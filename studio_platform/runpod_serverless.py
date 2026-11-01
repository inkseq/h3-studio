"""RunPod Serverless as a dstack-shaped backend for dial-out workers (#859).

The endpoint's start command runs deploy/runpod-serverless/handler.py, which
executes the run spec's own boot command with its own env, exactly as a dstack
container does. The worker then dials out, so readiness, dispatch, budget and
idle shutdown stay on the existing dstack capacity path. This client only maps
plan/apply/get/stop onto RunPod's /run, /status and /cancel.

One RunPod job is one run. The local journal (one private file per run name)
binds run name, dstack-style run id, job id and the exact applied spec; a
missing or unbound journal is never read as absence of a job.

Credentials: a protected JSON file named by the operator config's
`serverless_file`:
{"api_key", "endpoint_id", "gpu_name", "gpu_memory_gib", "cpus", "memory_gib",
 "disk_gib", "hourly_microusd", "state_dir"}.
"""

import json
import math
import os
from pathlib import Path
import re
import time
import uuid

import httpx

from .dstack_capacity import DstackError

BACKEND = "runpod-serverless"
API = "https://api.runpod.ai/v2/"
# RunPod job status → (dstack run status, dstack job submission status).
STATUSES = {
    "IN_QUEUE": ("provisioning", "provisioning"),
    "IN_PROGRESS": ("running", "running"),
    "COMPLETED": ("done", "done"),
    "FAILED": ("failed", "failed"),
    "TIMED_OUT": ("failed", "failed"),
    "CANCELLED": ("terminated", "terminated"),
}
LIVE = frozenset({"IN_QUEUE", "IN_PROGRESS"})
JOB_ID = re.compile(r"[A-Za-z0-9-]{1,128}")
RUN_NAME = re.compile(r"sixnine-[0-9a-f]{32}")
ENDPOINT_ID = re.compile(r"[a-z0-9]{1,64}")
BOOT_ENV = frozenset({"INKSEQ_BOOT_URL", "INKSEQ_INTENT", "INKSEQ_BOOT_TOKEN"})
MAX_EXECUTION_SECONDS = 7200
CONFIG_KEYS = frozenset(
    {
        "api_key",
        "endpoint_id",
        "gpu_name",
        "gpu_memory_gib",
        "cpus",
        "memory_gib",
        "disk_gib",
        "hourly_microusd",
        "state_dir",
    }
)


def _require(condition, code):
    if not condition:
        raise DstackError(code)


def validate_config(value):
    _require(
        isinstance(value, dict)
        and set(value) == CONFIG_KEYS
        and isinstance(value["api_key"], str)
        and 16 <= len(value["api_key"]) <= 4096
        and not any(c.isspace() for c in value["api_key"])
        and isinstance(value["endpoint_id"], str)
        and ENDPOINT_ID.fullmatch(value["endpoint_id"])
        and isinstance(value["gpu_name"], str)
        and re.fullmatch(r"[A-Za-z0-9 _-]{1,64}", value["gpu_name"])
        and all(
            type(value[k]) is int and 1 <= value[k] <= 100_000
            for k in ("gpu_memory_gib", "cpus", "memory_gib", "disk_gib")
        )
        and type(value["hourly_microusd"]) is int
        and 1 <= value["hourly_microusd"] <= 50_000_000
        and isinstance(value["state_dir"], str)
        and Path(value["state_dir"]).is_absolute(),
        "runpod_serverless_config_invalid",
    )
    return value


def boot_input(spec, backend=BACKEND):
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


class RunPodServerlessClient:
    """The dstack client subset the capacity path uses, for one endpoint.

    Subclasses (modal_dialout) reuse the journal and the dstack-shaped runs and
    replace only the provider calls: _launch, _status, _cancel and _live.
    """

    backend = BACKEND

    def __init__(self, config, project, *, http=None, clock=time.time):
        self.config = validate_config(config)
        self.project, self.clock = project, clock
        self.root = Path(config["state_dir"])
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.http = http or httpx.Client(timeout=30, follow_redirects=False)

    # ---- journal -------------------------------------------------------
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

    # ---- RunPod API ----------------------------------------------------
    def _call(self, method, path, body=None):
        url = API + self.config["endpoint_id"] + path
        headers = {"Authorization": "Bearer " + self.config["api_key"]}
        try:
            response = self.http.request(method, url, json=body, headers=headers)
        except httpx.HTTPError:
            raise DstackError("dstack_api_unavailable") from None
        if response.status_code == 429 or response.status_code >= 500:
            raise DstackError("dstack_api_unavailable")
        if response.status_code >= 400:
            raise DstackError("dstack_api_rejected")
        try:
            value = response.json()
        except ValueError:
            raise DstackError("dstack_response_invalid") from None
        _require(isinstance(value, dict), "dstack_response_invalid")
        return value

    def _status(self, record):
        """Live status; a terminal one is journaled once, since RunPod expires it."""
        final = record.get("final")
        if isinstance(final, dict):
            return final
        status = self._call("GET", "/status/" + record["job_id"])
        if status.get("status") in STATUSES and status["status"] not in LIVE:
            kept = {
                k: status[k]
                for k in ("status", "workerId", "executionTime")
                if k in status
            }
            record["final"] = kept
            self._write(record["run_name"], record, create=False)
        return status

    # ---- dstack client subset -----------------------------------------
    @property
    def instance_name(self):
        return self.config["endpoint_id"]

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
        value = self._launch(run_name, job, seconds)
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
            # Applied but the job id was never journaled: unknown, not absent.
            raise DstackError("dstack_apply_requires_reconciliation")
        return self._run(record, self._status(record))

    def stop(self, run_name):
        record = self._read(run_name)
        _require(record is not None and record.get("job_id"), "dstack_exact_run_absent")
        self._cancel(record["job_id"])
        return {}

    def _launch(self, run_name, job, seconds):
        return self._call(
            "POST",
            "/run",
            {
                "input": job,
                "policy": {
                    "executionTimeout": seconds * 1000,
                    "ttl": (seconds + 3600) * 1000,
                },
            },
        )

    def _cancel(self, job_id):
        self._call("POST", "/cancel/" + job_id)

    def _live(self):
        """The provider's own count of queued plus running jobs."""
        health = self._call("GET", "/health").get("jobs")
        _require(isinstance(health, dict), "dstack_provider_inventory_unconfirmed")
        return health.get("inQueue", -1) + health.get("inProgress", -1)

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
        if raw in {"FAILED", "TIMED_OUT", "CANCELLED"}:
            submission["termination_reason"] = raw.lower()
        run = {
            "id": record["run_id"],
            "project_name": self.project,
            "run_spec": record["spec"],
            "status": run_status,
            "latest_job_submission": submission,
        }
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

    def __init__(self, dstack, *lanes):
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
