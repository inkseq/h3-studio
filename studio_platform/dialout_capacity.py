"""Shared create-once capacity lifecycle for native dial-out worker hosts.

Provider adapters supply launch/status/cancel/inventory calls. This module keeps
existing journal bytes and dstack-shaped compatibility observations unchanged;
it owns neither the rental ledger nor provider selection. An unresolved original
journal never permits another create or routing to a different backend.
"""

import json
import logging
import math
import os
from pathlib import Path
import re
import time
import uuid

from .dstack_capacity import DstackError
from .capacity_interface import CapacityTransport
from .providers.registry import RUNPOD_SERVERLESS

logger = logging.getLogger(__name__)
# Retained adapter status vocabulary, not a new persisted state machine.
STATUSES = {
    "IN_QUEUE": ("provisioning", "provisioning"),
    "IN_PROGRESS": ("running", "running"),
    "COMPLETED": ("done", "done"),
    "FAILED": ("failed", "failed"),
    "TIMED_OUT": ("failed", "failed"),
    "CANCELLED": ("terminated", "terminated"),
    # Journals whose provider call never returned a job id (#1268): refused
    # outright, or old enough that no job of it can still exist.
    "REFUSED": ("failed", "failed"),
    "UNBOUND_EXPIRED": ("failed", "failed"),
    # The provider's own count showed no job at all after the grace (#1272).
    "UNBOUND_ABSENT": ("failed", "failed"),
    # Queued while the provider had no worker at all, then cancelled (#1272).
    "NO_CAPACITY": ("failed", "failed"),
}
UNBOUND = frozenset({"REFUSED", "UNBOUND_EXPIRED", "UNBOUND_ABSENT"})
LIVE = frozenset({"IN_QUEUE", "IN_PROGRESS"})
JOB_ID = re.compile(r"[A-Za-z0-9-]{1,128}")
RUN_NAME = re.compile(r"sixnine-[0-9a-f]{32}")
BOOT_ENV = frozenset({"INKSEQ_BOOT_URL", "INKSEQ_INTENT", "INKSEQ_BOOT_TOKEN"})
MAX_EXECUTION_SECONDS = 7200


def _require(condition, code):
    if not condition:
        raise DstackError(code)


def boot_input(spec, backend=RUNPOD_SERVERLESS.id):
    """The job input: the run spec's single boot command and its boot env."""
    configuration = spec.get("configuration") if isinstance(spec, dict) else None
    _require(isinstance(configuration, dict), "runpod_serverless_spec_invalid")
    commands, env = configuration.get("commands"), configuration.get("env")
    _require(
        configuration.get("backends") == [backend]
        and isinstance(commands, list)
        and len(commands) == 1
        and isinstance(commands[0], str)
        and commands[0].startswith("exec python3 - <<'INKSEQ_BOOT_SCRIPT'\n")
        and isinstance(env, dict)
        and set(env) == BOOT_ENV
        and all(isinstance(v, str) for v in env.values()),
        "runpod_serverless_dialout_required",
    )
    return {"command": commands[0], "env": dict(env)}


class DialoutCapacityClient:
    """Shared journal/compatibility projection; subclasses own only vendor calls."""

    # A rejection by this lane proves nothing was created (a 4xx answer before
    # any job exists). Lanes whose create errors mix refusals with lost answers
    # keep False and wait for the expiry below.
    DEFINITE_REJECTION = False
    # Past this age an unbound journal's job, if the provider created one, has
    # ended: a RunPod job lives its execution limit plus a queue TTL of one hour
    # (_launch), a Modal sandbox at most its timeout. Ten minutes of margin.
    UNBOUND_EXPIRY_SECONDS = MAX_EXECUTION_SECONDS + 3600 + 600
    # A lane that can count its own jobs may end an unbound journal this soon,
    # once that count proves no job exists (_unbound_absent).
    UNBOUND_GRACE_SECONDS = 120

    def __init__(self, config, project, *, clock=time.time):
        self.config = config
        self.project, self.clock = project, clock
        self.root = Path(config["state_dir"])
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _provider_error(self, operation, code, error):
        # Exception messages may contain credentials or provider/customer payloads.
        logger.warning(
            "capacity_provider_failure backend=%s operation=%s code=%s error_type=%s",
            self.backend,
            operation,
            code,
            type(error).__name__,
        )
        return DstackError(code)

    def _path(self, run_name):
        _require(
            isinstance(run_name, str) and RUN_NAME.fullmatch(run_name),
            "dstack_run_name_invalid",
        )
        return self.root / (run_name + ".json")

    def _write(self, run_name, value, *, create):
        path = self._path(run_name)
        temporary = path.with_name(path.name + ".tmp")
        with open(temporary, "w") as out:
            json.dump(value, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        if create:
            # Create-only like dstack apply: an existing journal is a collision.
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise DstackError(
                    "dstack_existing_run_requires_reconciliation"
                ) from None
            finally:
                temporary.unlink()
        else:
            os.replace(temporary, path)

    def _read(self, run_name):
        try:
            return json.loads(self._path(run_name).read_text())
        except FileNotFoundError:
            return None

    def _by_id(self, run_id):
        for path in self.root.glob("sixnine-*.json"):
            value = json.loads(path.read_text())
            if value.get("run_id") == run_id:
                return value
        return None

    def configured_backends(self):
        return frozenset({self.backend})

    def plan(self, spec):
        configuration = spec["configuration"]
        names = configuration.get("resources", {}).get("gpu", {}).get("name") or []
        price = self.config["hourly_microusd"] / 1_000_000
        offers = []
        if (
            configuration.get("backends") == [self.backend]
            and (not names or self.config["gpu_name"] in names)
            and price <= configuration.get("max_price", price)
        ):
            offers.append(self._offer(price))
        return {"run_spec": spec, "job_plans": [{"offers": offers}]}

    def _offer(self, price):
        return {
            "backend": self.backend,
            "region": self.backend,
            "price": price,
            "availability": "available",
            "instance": {"name": self.instance_name, "resources": self._resources()},
        }

    def _resources(self):
        c = self.config
        return {
            "cpus": c["cpus"],
            "memory_mib": c["memory_gib"] * 1024,
            "disk": {"size_mib": c["disk_gib"] * 1024},
            "gpus": [{"name": c["gpu_name"], "memory_mib": c["gpu_memory_gib"] * 1024}],
        }

    def apply(self, spec):
        run_name = spec.get("run_name")
        job = boot_input(spec, self.backend)
        duration = spec["configuration"].get("max_duration")
        seconds = duration if type(duration) is int and duration > 0 else None
        seconds = min(seconds or MAX_EXECUTION_SECONDS, MAX_EXECUTION_SECONDS)
        record = {
            "run_name": run_name,
            "run_id": str(uuid.uuid4()),
            "job_id": None,
            "spec": spec,
            "created_at": self.clock(),
        }
        # Journal before the provider call; a lost answer stays unbound.
        self._write(run_name, record, create=True)
        try:
            value = self._launch(run_name, job, seconds)
        except DstackError as error:
            if self.DEFINITE_REJECTION and str(error) == "dstack_api_rejected":
                # Nothing exists to observe or stop: the start fails at once and
                # the next lane is tried, instead of waiting out the expiry.
                record["final"] = {"status": "REFUSED"}
            else:
                # The grace counts from the failed answer: a slow timeout must
                # not use it up before the provider could show a late job.
                record["failed_at"] = self.clock()
            self._write(run_name, record, create=False)
            raise
        job_id = value.get("id")
        _require(
            isinstance(job_id, str) and JOB_ID.fullmatch(job_id),
            "dstack_response_invalid",
        )
        record["job_id"] = job_id
        self._write(run_name, record, create=False)
        return self._run(record, value)

    def get(self, *, run_name=None, run_id=None):
        _require(bool(run_name) != bool(run_id), "dstack_exact_identity_required")
        record = self._read(run_name) if run_name else self._by_id(run_id)
        if record is None:
            return None
        if record.get("job_id") is None:
            final = self._unbound_final(record)
            if final is None:
                # Applied but the job id was never journaled: unknown, not absent.
                raise DstackError("dstack_apply_requires_reconciliation")
            return self._run(record, final)
        return self._run(record, self._status(record))

    def _unbound_final(self, record):
        """The terminal status of a journal without a job id, else None (#1268).

        Refused journals are final at once. Others become final only past the
        expiry, when no job of theirs can still run. Neither claims zero cost:
        no execution time is reported, so the run's cost stays unknown.
        """
        final = record.get("final")
        if isinstance(final, dict) and final.get("status") in UNBOUND:
            return final
        created = record.get("created_at")
        if type(created) not in (int, float):
            return None
        now = self.clock()
        failed = record.get("failed_at")
        failed = failed if type(failed) in (int, float) else created
        if now - created >= self.UNBOUND_EXPIRY_SECONDS:
            status = "UNBOUND_EXPIRED"
        elif now - failed >= self.UNBOUND_GRACE_SECONDS and self._unbound_absent():
            # Waiting out the expiry blocked every start for hours (#1272).
            status = "UNBOUND_ABSENT"
        else:
            return None
        record["final"] = {"status": status}
        self._write(record["run_name"], record, create=False)
        return record["final"]

    def _unbound_absent(self):
        """Whether the provider shows no job of ours queued or running; lanes override.

        This proves nothing live, not that nothing ran: the run's cost stays
        unknown, as for the expiry.
        """
        return False

    def stop(self, run_name):
        record = self._read(run_name)
        _require(record is not None, "dstack_exact_run_absent")
        if record.get("job_id") is None and self._unbound_final(record) is not None:
            return {}  # Nothing was created, or it has long ended: nothing to stop.
        _require(record.get("job_id"), "dstack_exact_run_absent")
        self._cancel(record["job_id"])
        return {}

    def _run(self, record, status):
        raw = status.get("status")
        _require(raw in STATUSES, "dstack_run_status_invalid")
        run_status, job_status = STATUSES[raw]
        submission = {"id": record["run_id"], "status": job_status}
        executed = status.get("executionTime")
        if raw != "IN_QUEUE":
            worker = status.get("workerId")
            instance = (
                worker
                if isinstance(worker, str) and JOB_ID.fullmatch(worker)
                # A journal without a job id still names its own allocation, the
                # same way "job-<id>" names a job whose worker is unknown. The
                # provider's complete live inventory never lists it, which is
                # what proves it gone, releases its slot and cools the lane.
                else "unbound-" + record["run_id"]
                if raw in UNBOUND
                else "job-" + record["job_id"]
            )
            submission["job_provisioning_data"] = {
                "backend": self.backend,
                "instance_id": instance,
                "region": self.backend,
                "price": self.config["hourly_microusd"] / 1_000_000,
                "dockerized": True,
                "instance_type": {
                    "name": self.instance_name,
                    "resources": self._resources(),
                },
            }
        if raw in {"FAILED", "TIMED_OUT", "CANCELLED", "NO_CAPACITY", *UNBOUND}:
            submission["termination_reason"] = raw.lower()
        run = {
            "id": record["run_id"],
            "project_name": self.project,
            "run_spec": record["spec"],
            "status": run_status,
            "latest_job_submission": submission,
        }
        if status.get("no_worker") is True:
            # Queued with no worker the provider could start (#1272).
            run["lane_no_worker"] = True
        if type(executed) in (int, float) and math.isfinite(executed) and executed >= 0:
            run["cost"] = executed / 1000 * self.config["hourly_microusd"] / 3.6e9
        return run

    def provider_inventory(self, backend, *, clock=None):
        """Live jobs of this dedicated endpoint, cross-checked with /health."""
        _require(backend == self.backend, "dstack_backend_unsupported")
        observed_at = (clock or self.clock)()
        live = []
        for path in sorted(self.root.glob("sixnine-*.json")):
            record = json.loads(path.read_text())
            if record.get("job_id") is None and self._unbound_final(record):
                continue  # Refused or expired: cannot be live (#1268).
            _require(record.get("job_id"), "dstack_provider_inventory_unconfirmed")
            if isinstance(record.get("final"), dict):
                continue  # Settled; never queried again.
            status = self._status(record)
            _require(
                status.get("status") in STATUSES,
                "dstack_provider_inventory_unconfirmed",
            )
            if status["status"] in LIVE:
                live.append(self._run(record, status))
        _require(self._live() == len(live), "dstack_provider_inventory_unconfirmed")
        ids = [
            run["latest_job_submission"]["job_provisioning_data"]["instance_id"]
            for run in live
            if "job_provisioning_data" in run["latest_job_submission"]
        ]
        return {
            "backend": self.backend,
            "complete": True,
            "observed_at": observed_at,
            "instance_ids": ids,
        }


class RoutedClient:
    """dstack for dstack backends; each serverless lane for its own backend."""

    def __init__(self, dstack: CapacityTransport, *lanes: DialoutCapacityClient):
        self.dstack = dstack
        self.lanes = {lane.backend: lane for lane in lanes}

    def __getattr__(self, name):
        return getattr(self.dstack, name)

    def _for_spec(self, spec):
        backends = spec.get("configuration", {}).get("backends")
        if isinstance(backends, list) and len(backends) == 1:
            return self.lanes.get(backends[0], self.dstack)
        return self.dstack

    def _for_run(self, run_name):
        for lane in self.lanes.values():
            if lane._read(run_name) is not None:
                return lane
        return self.dstack

    def configured_backends(self):
        return frozenset(self.dstack.configured_backends()) | set(self.lanes)

    def lane_price(self, backend):
        """A serverless lane's own configured rate (micro-USD per hour), else None.

        Serverless bills per second only while it works, so a rented machine's
        hourly ceiling is not its measure (owner 2026-11-17, #1243).
        """
        lane = self.lanes.get(backend)
        return None if lane is None else lane.config["hourly_microusd"]

    def plan(self, spec):
        return self._for_spec(spec).plan(spec)

    def apply(self, spec):
        return self._for_spec(spec).apply(spec)

    def get(self, *, run_name=None, run_id=None):
        for lane in self.lanes.values():
            found = lane.get(run_name=run_name, run_id=run_id)
            if found is not None:
                return found
        return self.dstack.get(run_name=run_name, run_id=run_id)

    def stop(self, run_name):
        return self._for_run(run_name).stop(run_name)

    def provider_inventory(self, backend, **kwargs):
        lane = self.lanes.get(backend, self.dstack)
        return lane.provider_inventory(backend, **kwargs)
