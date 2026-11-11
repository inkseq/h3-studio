"""Controller simulator (#1034): the real controller on a fake clock and provider.

One world is a disposable SQLite ledger built by the existing test harnesses
(``LedgerCase``, ``DstackOperatorTests._setup_dstack`` and the demand-start
settings). ``DstackController.tick``, demand start, the health check, the
watchdog, ``WorkerControl`` claims and ``worker_admission`` run unchanged.
Only the edges are fake: the dstack/provider client, native readiness, the
DBOS slot process and the GPU worker loop. No network, no provider and no GPU.

A scenario scripts each machine (create reply, allocation, download, model
load, worker liveness, provider removal) and the outside world (jobs arriving,
app restarts, inventory reads, demand toggles). Invariants are checked after
every tick and at the end. See docs/CAPACITY-SIMULATOR.md.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import copy
from dataclasses import dataclass, field
import heapq
import itertools
import json
import logging
import random
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from sqlalchemy import insert, select, update

from studio_platform.control import WorkerSpec
from studio_platform.dstack_capacity import DstackError
from studio_platform.dstack_controller import DstackController
from studio_platform.dstack_demand_policy import policy_key
from studio_platform.dstack_operator import DstackOperator
from studio_platform.dstack_test_budget import ACCOUNT, demand_task_id
from studio_platform.fleet import SlotConfig
from studio_platform.inference.wangp_contract import HostReadiness
from studio_platform.operator_capacity import operator_nodes, operator_policy
from studio_platform.queue import TaskQueue
from studio_platform.repository import (
    Conflict,
    InvalidTransition,
    LeaseLost,
    attempts,
    instance_intents,
    jobs,
    outbox,
    registered_workers,
    request_hash,
)
from test_platform_repository import LedgerCase
import test_dstack_operator as operator_fixture

STAGING_POOL = "inkseq-staging-v8-fl"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "capacity_sim"
GIB = 1000**3
ARTIFACT = {
    "kind": "video",
    "object_key": "sim/results/output.mp4",
    "size_bytes": 100,
    "sha256": "a" * 64,
    "validated": True,
}


# --------------------------------------------------------------------------
# Scripts: what one machine and one scenario do. Times are sim seconds.
# --------------------------------------------------------------------------


ENGINE_FAILURE = "wangp_generation_tensor_shape_mismatch"
# The hosted slot simulated requests are routed to.
HOSTED_MODEL = "h3-max-turbo"


@dataclass
class MachineScript:
    """One rented machine, relative to its create call."""

    create: str = "ok"  # ok | reply_lost
    allocate_after: float | None = 60  # None: never allocates
    no_capacity: bool = False  # the run fails without an allocation
    running_after: float = 30  # provisioning -> running, after allocation
    download_seconds: float = 300
    download_stall_at: float | None = None  # fraction 0..1 where bytes stop
    download_bytes: int = 52 * GIB
    load_seconds: float = 60
    load_fails: bool = False
    terminal_at: float | None = None  # run turns terminal (failed) on its own
    removed_at: float | None = None  # provider deletes the machine
    worker_lost_at: float | None = None  # slot heartbeat and consumer stop
    tunnel_lost_on_restart: str | None = None  # None | "returns" | "never"
    # Every result collection on this machine fails after the run finished
    # (the host cannot deliver its output), like staging job 27b6213f.
    collect_fails: bool = False
    # The engine on this machine accepts work and is never heard from again.
    silent: bool = False
    stop_seconds: float = 60  # stop request -> machine gone
    price: float = 0.7  # USD/hour billed by the provider
    # Recorded provider fields laid over every run read once the machine is
    # allocated, e.g. a real termination_reason or job_provisioning_data.
    run_fields: dict | None = None
    # [[from, until, code]] seconds after create in which a run read raises
    # DstackError(code), as recorded by an exported incident.
    read_errors: list | None = None

    @classmethod
    def from_fixture(cls, name, **overrides):
        """A recorded machine from tests/fixtures/capacity_sim/<name>.json.

        ``machine`` holds MachineScript timings; ``run`` holds recorded dstack
        run fields (same shape as the API) laid over the scripted state.
        """
        path = (
            Path(name) if str(name).endswith(".json") else FIXTURES / (name + ".json")
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        fields = dict(value.get("machine", {}), run_fields=value.get("run"))
        return cls(**{**fields, **overrides})


@dataclass
class Host:
    """One physical host the provider keeps offering (#1198).

    ``rentals`` script its rentals in order; the last one repeats.
    """

    machine_id: str
    rentals: list


@dataclass
class JobBatch:
    at: float
    count: int
    mode: str = "fl"
    owner: str = "superdan"
    expected_runtime_s: int = 1800
    actual_runtime_s: float = 240
    spacing: float = 0
    # The engine publishes a failure a minute after it accepts the request
    # and keeps its generation thread alive, on every machine (#1214).
    engine_fails: bool = False
    # The request is accepted and then nothing is ever heard from the engine,
    # on every machine: no progress, no failure, no result (#1214).
    silent: bool = False
    # Hosted routing (Scenario.hosted): the billing tier, and whether the
    # hosted catalog has a model for this request.
    tier: str = "standard"
    hostable: bool = True


@dataclass
class Event:
    at: float
    action: str  # restart_app | demand_off | demand_on | offers_off | offers_on
    value: object = None


@dataclass
class Scenario:
    name: str
    description: str
    hours: float = 3.0
    tick_seconds: float = 60
    tick_jitter: tuple | None = None  # (min, max) seconds between ticks
    prepare_seconds: float = 5  # claim -> submission window check
    observation_fresh_seconds: int = 30
    jobs: list = field(default_factory=list)
    machines: list = field(default_factory=list)  # scripts in create order
    # A market of named hosts instead: every plan offers all of them, cheapest
    # first, and a create lands on the first one the frozen selector names.
    hosts: list = field(default_factory=list)
    expect_host_rentals: dict | None = None  # machine id -> most rentals allowed
    default_machine: MachineScript = field(default_factory=MachineScript)
    events: list = field(default_factory=list)
    max_machines: int = 2
    offers: bool = True
    inventory: str = "fresh"  # fresh | stale | failed | slow (alternating)
    history_removed: int = 0
    history_invoiced: bool = True  # history settled at its cost (else pending)
    test_guard: dict | None = None  # {"task_limit_usd", "max_runtime_seconds"}
    spent_fraction: float = 0.0  # pre-spent share of the test task cap
    owner_profiles: tuple = ("superdan",)
    lookahead: bool = False  # demand_start.lookahead_version 1
    check_owner_funding: bool = True  # a start on another owner's profile is recorded
    demand_enabled: bool = True
    expect_all_done: bool = True
    expect_failed: dict | None = None  # job error code -> jobs that end failed
    expect_capacity: bool = True  # capacity and budget allow work to start
    # Hosts the speed gate denies, and health alert codes raised (#903).
    expect_slow_denies: int | None = None
    expect_denies: dict | None = None  # automatic deny reason -> hosts denied
    # Hosts that end the run denied in effect, stored or derived from history.
    expect_effective_denies: int | None = None
    expect_alerts: tuple = ()
    expected_failure: str | None = None  # known controller bug, with reason
    # Policy idle_shutdown_seconds; lookahead starts use the start rules' boot.
    idle_shutdown_seconds: float = 600
    # Saved start rules (job_seconds, boot_seconds, minimum_jobs) for every
    # demand profile, as the admin start-rules API stores them; None: defaults.
    start_rules: dict | None = None
    expect_starts: int | None = None  # exact number of machines created
    # Every idle stop comes this many seconds after the machine's last job
    # (or readiness), at most one tick late, and at least one happens.
    expect_idle_stop_seconds: float | None = None
    health_window_seconds: float = 900
    max_queue_age_seconds: float = 2 * 3600
    idle_grace_seconds: float = 600 + 600
    # Seconds each provider read (run GET, inventory, plan, offer mapping)
    # takes. Above zero, workers run concurrently with the controller tick:
    # they act during provider waits unless the capacity lock is held (#1148).
    provider_latency: float = 0
    # The host's everyframe-routing.json; None: the credential is not installed.
    hosted: dict | None = None
    hosted_slots: int = 4
    hosted_seconds: float = 90
    expect_hosted: int | None = None  # jobs that finish on the hosted route
    # Every routed job was routed at most this long after it was submitted.
    expect_hosted_within: float | None = None


HISTORY = MachineScript(allocate_after=0, running_after=0)


def incident_scenario(path):
    """A Scenario from an exported incident fixture (capacity_incident_export).

    The recorded machine is the first create; later creates are healthy. The
    fixture may carry ``expected_failure`` while the bug it shows is open.
    """
    path = Path(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    spec = dict(value["scenario"])
    jobs = [JobBatch(**batch) for batch in spec.pop("jobs")]
    guard = spec.pop("test_guard", None)
    scenario = Scenario(
        spec.pop("name", path.stem),
        spec.pop("description", value.get("description", "Recorded incident.")),
        jobs=jobs,
        machines=[MachineScript.from_fixture(str(path))],
        test_guard=guard,
        expected_failure=value.get("expected_failure"),
        # A replay is about the recorded machine; whether the rest of the
        # queue finishes depends on machines the incident did not record.
        expect_all_done=spec.pop("expect_all_done", False),
        **spec,
    )
    scenario.recorded = value.get("recorded", {})
    return scenario


def incident_scenarios():
    """Every exported incident checked into tests/fixtures/capacity_sim."""
    return [
        incident_scenario(path) for path in sorted(FIXTURES.glob("incident_*.json"))
    ]


# --------------------------------------------------------------------------
# Fake provider: dstack run JSON + provider inventory, per scripted machine.
# --------------------------------------------------------------------------


class Machine:
    def __init__(self, script, *, intent_id, run_name, created_at, index):
        self.script, self.intent_id, self.run_name = script, intent_id, run_name
        self.created_at, self.index = created_at, index
        self.instance_id = str(4_300_000 + index)
        self.offer_id = str(10_000 + index)
        self.machine_id = str(19_000 + index)
        self.stop_requested_at = None
        self.max_duration = None
        self.gone_at = None
        self.busy_job = None
        self.restarted_at = None
        self.billed_seconds = 0.0
        self.idle_seconds = 0.0
        self.idle_with_work = 0.0
        self.idle_without_work = 0.0
        self.job_done_at = 0.0
        self.job_started_at = 0.0
        self.last_completed_at = None

    def incarnation(self):
        """The native runtime identity; one per machine in these scripts."""
        return request_hash({"instance": self.instance_id})[:32]

    def elapsed(self, now):
        return now - self.created_at

    def allocated_at(self):
        if self.script.allocate_after is None or self.script.no_capacity:
            return None
        return self.created_at + self.script.allocate_after

    def alive(self, now):
        """True while the provider bills this machine."""
        at = self.allocated_at()
        return (
            at is not None
            and at <= now
            and (self.gone_at is None or now < self.gone_at)
        )

    def advance(self, now):
        script = self.script
        if self.gone_at is None:
            ends = [
                t
                for t in (
                    script.removed_at is not None
                    and self.created_at + script.removed_at,
                    self.stop_requested_at is not None
                    and self.stop_requested_at + script.stop_seconds,
                    # dstack's own max_duration ends the run ("off" in production).
                    self.max_duration is not None
                    and self.created_at + self.max_duration,
                    # A failed run: dstack terminates its instance shortly after.
                    script.terminal_at is not None
                    and self.created_at + script.terminal_at + script.stop_seconds,
                )
                if t
            ]
            if ends and min(ends) <= now:
                self.gone_at = min(ends)

    def removed_first(self):
        """The provider or a failed run ended it, not our stop request."""
        script = self.script
        return any(
            at is not None and self.gone_at == self.created_at + at
            for at in (
                script.removed_at,
                script.terminal_at is not None
                and script.terminal_at + script.stop_seconds
                or None,
            )
        )

    def phase(self, now):
        """Native boot phase for an allocated, running machine."""
        script = self.script
        start = self.allocated_at() + script.running_after
        download_end = start + script.download_seconds
        if now < download_end or script.download_stall_at is not None:
            fraction = min(
                1.0, max(0.0, (now - start) / max(script.download_seconds, 1))
            )
            if script.download_stall_at is not None:
                fraction = min(fraction, script.download_stall_at)
            return "download", start, fraction
        if script.load_fails:
            return "load_failed", download_end, 1.0
        if now < download_end + script.load_seconds:
            return "load", download_end, 1.0
        return "ready", download_end + script.load_seconds, 1.0

    def lost_since(self, world):
        """When the dial-out worker stopped answering (only while it is lost)."""
        lost = []
        if self.script.worker_lost_at is not None:
            lost.append(self.created_at + self.script.worker_lost_at)
        if self.restarted_at is not None and self.script.tunnel_lost_on_restart:
            lost.append(self.restarted_at)
        return min(lost, default=world.now)

    def download_problem(self, now):
        """(since, what) for a stalled or below-floor download, else None."""
        from studio_platform.dstack_preparation import (
            DOWNLOAD_MEASURE_SECONDS,
            MIN_DOWNLOAD_BYTES_PER_SECOND,
        )

        if self.allocated_at() is None or now < self.allocated_at():
            return None
        phase, start, fraction = self.phase(now)
        if phase != "download":
            return None
        script = self.script
        if (
            script.download_stall_at is not None
            and fraction >= script.download_stall_at
        ):
            since = start + script.download_stall_at * max(script.download_seconds, 1)
            return since, "stalled at %d%%" % (script.download_stall_at * 100)
        rate = script.download_bytes / max(script.download_seconds, 1)
        if rate < MIN_DOWNLOAD_BYTES_PER_SECOND:
            return start + DOWNLOAD_MEASURE_SECONDS, "at %d MB/s" % (rate / 1e6)
        return None

    def worker_alive(self, world):
        now = world.now
        if self.script.worker_lost_at is not None and (
            now >= self.created_at + self.script.worker_lost_at
        ):
            return False
        if self.restarted_at is not None and self.script.tunnel_lost_on_restart:
            if self.script.tunnel_lost_on_restart == "never":
                return False
            return now >= self.restarted_at + 300  # the tunnel comes back
        return True


class SimProvider(operator_fixture.Client):
    """dstack-shaped runs and complete provider inventories; no HTTP."""

    def __init__(self, store, world):
        super().__init__(store)
        self.world = world
        self.machines = {}  # run_name -> Machine
        self.plans = 0
        self.inventory_reads = 0
        self.scripted = 0
        self.rented = Counter()  # machine id -> rentals, for scenario.hosts

    # Planning -------------------------------------------------------------
    def _next_script(self):
        if self.world._seeding:
            return HISTORY
        index = self.scripted
        scripts = self.world.scenario.machines
        return (
            scripts[index]
            if index < len(scripts)
            else self.world.scenario.default_machine
        )

    def _market(self):
        """(offer id, host, its next rental's script), cheapest first."""
        if self.world._seeding:
            return []
        rows = [
            (
                str(30_000 + index),
                host,
                host.rentals[min(self.rented[host.machine_id], len(host.rentals) - 1)],
            )
            for index, host in enumerate(self.world.scenario.hosts)
        ]
        return sorted(rows, key=lambda row: row[2].price)

    def plan(self, spec):
        self.world.provider_wait("plan")
        self.plans += 1
        value = super().plan(spec)
        if not self.world.offers:
            value["job_plans"][0]["offers"] = []
            return value
        offer = value["job_plans"][0]["offers"][0]
        market = self._market()
        if market:
            value["job_plans"][0]["offers"] = [
                dict(
                    copy.deepcopy(offer),
                    price=script.price,
                    instance=dict(copy.deepcopy(offer["instance"]), name=offer_id),
                )
                for offer_id, _, script in market
            ]
            return value
        offer["instance"]["name"] = str(10_000 + len(self.machines))
        offer["price"] = self._next_script().price
        return value

    def vast_offer_mapping(self, request, *, clock):
        self.world.provider_wait("vast_offer_mapping")
        index = len(self.machines)
        hosts = [
            (offer_id, host.machine_id) for offer_id, host, _ in self._market()
        ] or [(str(10_000 + index), str(19_000 + index))]
        return {
            "observed_at": clock(),
            "offers": [
                {
                    "offer_id": offer_id,
                    "machine_id": machine_id,
                    "country": "US",
                    "region": "test-region",
                    "advertised_download_mbps": 2000,
                }
                for offer_id, machine_id in hosts
            ],
        }

    def reusable_hosts_confirmed(self, approved):
        self.world.provider_wait("reusable_hosts")
        return super().reusable_hosts_confirmed(approved)

    def vast_instance_identity(self, identity):
        self.world.provider_wait("vast_instance_identity")
        machine = next(
            (m for m in self.machines.values() if m.instance_id == identity), None
        )
        return {
            "provider_instance_id": identity,
            "machine_id": machine.machine_id if machine else "19493",
            "reliability": 0.99,
        }

    # Create / read / stop ---------------------------------------------------
    def apply(self, spec):
        intent = spec["configuration"]["tags"]["sixnine_intent"]
        assert self.store.load(intent)["apply_started"] is True
        self.applied.append(intent)
        market = self._market()
        if market:
            # The native selector keeps the controller's order (dstack_preview).
            order = (spec.get("profile") or {}).get("instance_types") or []
            offer_id, host, script = next(
                (row for name in order for row in market if row[0] == name),
                market[0],
            )
            self.rented[host.machine_id] += 1
        else:
            script = self._next_script()
            if script is not HISTORY:
                self.scripted += 1
        machine = Machine(
            script,
            intent_id=intent,
            run_name=spec["run_name"],
            created_at=self.world.now,
            index=len(self.machines),
        )
        if market:
            machine.offer_id, machine.machine_id = offer_id, host.machine_id
        limit = spec["configuration"].get("max_duration")
        machine.max_duration = limit if type(limit) is int else None
        self.machines[spec["run_name"]] = machine
        value = {
            "id": str(uuid.uuid4()),
            "project_name": self.project,
            "run_spec": copy.deepcopy(spec),
            "status": "submitted",
            "cost": 0.0,
        }
        self.runs[spec["run_name"]] = value
        self.world.log("create", machine)
        if not self.world._seeding and script.no_capacity:
            self.world.evidence.create_failed(machine, "no_capacity")
        if script.create == "reply_lost":
            self.world.evidence.create_failed(machine, "dstack_api_unavailable")
            raise DstackError("dstack_api_unavailable")
        return copy.deepcopy(value)

    def _refresh(self, name):
        run, machine = self.runs.get(name), self.machines.get(name)
        if run is None or machine is None:
            return
        now = self.world.now
        machine.advance(now)
        script = machine.script
        allocated = machine.allocated_at()
        run["cost"] = round(machine.billed_seconds / 3600 * script.price, 6)
        if script.no_capacity and machine.elapsed(now) >= 60:
            from test_dstack_capacity import no_capacity_run

            self.runs[name] = no_capacity_run(run)
            return
        if allocated is None or now < allocated:
            run["status"] = (
                "submitted" if machine.stop_requested_at is None else "terminated"
            )
            return
        data = {
            "backend": "vastai",
            "instance_id": machine.instance_id,
            "region": "test-region",
            "price": script.price,
            "instance_type": {
                "name": machine.offer_id,
                "resources": {"gpus": [{"name": "RTX5090"}]},
            },
        }
        run["latest_job_submission"] = {"job_provisioning_data": data}
        if machine.gone_at is not None and now >= machine.gone_at:
            run["status"] = "failed" if machine.removed_first() else "terminated"
        elif machine.stop_requested_at is not None:
            run["status"] = "terminating"
        elif (
            script.terminal_at is not None
            and machine.elapsed(now) >= script.terminal_at
        ):
            run["status"] = "failed"
        elif now < allocated + script.running_after:
            run["status"] = "provisioning"
        else:
            run["status"] = "running"

    def get(self, *, run_name=None, run_id=None):
        self.world.provider_wait("run_get")
        for name, run in list(self.runs.items()):
            if run_name == name or run_id is not None and run.get("id") == run_id:
                self._refresh(name)
                self._overlay(name)
                machine = self.machines.get(name)
                for start, until, code in (
                    machine and machine.script.read_errors
                ) or ():
                    if start <= machine.elapsed(self.world.now) < until:
                        raise DstackError(code)
        return super().get(run_name=run_name, run_id=run_id)

    def _overlay(self, name):
        run, machine = self.runs.get(name), self.machines.get(name)
        fields = machine and machine.script.run_fields
        if not fields or machine.allocated_at() is None:
            return
        for key, value in fields.items():
            if isinstance(value, dict) and isinstance(run.get(key), dict):
                for inner, item in value.items():
                    if isinstance(item, dict) and isinstance(run[key].get(inner), dict):
                        run[key][inner].update(copy.deepcopy(item))
                    else:
                        run[key][inner] = copy.deepcopy(item)
            else:
                run[key] = copy.deepcopy(value)

    def stop(self, name):
        self.stopped.append(name)
        machine = self.machines.get(name)
        if machine is not None and machine.stop_requested_at is None:
            machine.stop_requested_at = self.world.now
            self.world.log("stop", machine)

    # Inventory -------------------------------------------------------------
    def provider_inventory(self, backend, *, clock=None):
        self.world.provider_wait("inventory")
        self.inventory_reads += 1
        mode = self.world.inventory_mode()
        if mode == "failed":
            raise DstackError("dstack_provider_inventory_unavailable")
        now = self.world.now
        ids = []
        for machine in self.machines.values():
            machine.advance(now)
            if machine.alive(now) or (
                machine.allocated_at() is not None
                and machine.allocated_at() <= now
                and machine.gone_at is None
            ):
                ids.append(machine.instance_id)
        return {
            "backend": backend,
            "complete": True,
            "observed_at": now - (300 if mode == "stale" else 0),
            "instance_ids": sorted(set(ids)),
            "aliases": {},
        }


# --------------------------------------------------------------------------
# Native edges: readiness callback, slot config, DBOS slot process.
# --------------------------------------------------------------------------


class SimRuntime:
    def __init__(self, world):
        self.world = world

    def close(self, intent_id=None):
        pass

    def readiness(self, binding, run):
        world = self.world
        machine = world.machine_for(binding["intent_id"])
        if machine is None:
            raise DstackError("dstack_runtime_preparing")
        now = world.now
        phase, started, fraction = machine.phase(now)
        script = machine.script
        if phase == "download":
            error = DstackError("dstack_runtime_downloading_weights")
            done = int(script.download_bytes * fraction)
            error.preparation = {
                "phase": "model_download",
                "started_at": started,
                "phase_started_at": started,
                "updated_at": now,
                "download": {
                    "bytes_complete": done,
                    "bytes_received": done,
                    "bytes_total": script.download_bytes,
                    "files_complete": int(11 * fraction),
                    "files_total": 11,
                },
            }
            raise error
        complete = {
            "bytes_complete": script.download_bytes,
            "bytes_received": script.download_bytes,
            "bytes_total": script.download_bytes,
            "files_complete": 11,
            "files_total": 11,
        }
        if phase == "load":
            error = DstackError("dstack_runtime_loading_model")
            error.preparation = {
                "phase": "model_load",
                "started_at": started,
                "phase_started_at": started,
                "updated_at": now,
                "download": complete,
                "model": {
                    "state": "loading",
                    "phase_started_at": started,
                    "updated_at": now,
                },
            }
            raise error
        if phase == "load_failed":
            # The one-shot launch ended: processes stopped, phase "failed".
            error = DstackError("dstack_runtime_bootstrap_failed")
            error.preparation = {
                "phase": "failed",
                "failure_phase": "model_load",
                "reason_code": "SubprocessFailed",
                "started_at": started,
                "phase_started_at": started,
                "updated_at": now,
                "download": complete,
            }
            raise error
        if not machine.worker_alive(world):
            # The native host answers, but its dial-out worker is gone.
            raise DstackError("dstack_runtime_bootstrap_reconciliation_required")
        return HostReadiness(
            binding["manifest_digest"],
            binding["intent_id"],
            machine.incarnation(),
            machine.busy_job is None,
        )

    def slot(self, intent_id):
        world = self.world
        binding = world.store.load(intent_id)
        machine = world.machine_for(intent_id)
        # Every simulated profile shares one pool, recipe and model.
        profile = world.service.profiles[world.profile_ids[0]]
        from studio_platform.runtime_catalog import profile_output_delivery

        spec = WorkerSpec(
            world.worker_id(intent_id),
            profile["pool"],
            "vast",
            machine.instance_id,
            ("GPU-" + machine.instance_id,),
            (profile["recipe_id"],),
            binding["model_id"],
            binding["configuration_id"],
            "wangp-worker",
            binding["manifest_digest"],
            dispatch_backend="hatchet-v1",
            output_delivery=profile_output_delivery(operator_fixture.PROFILE),
        )
        return SlotConfig(
            spec,
            True,
            "http://127.0.0.1:8199",
            ("http://127.0.0.1:8199",),
            "",
            True,
            runtime_config_file=str(Path(world.temp.name) / "native.json"),
        )


class SimSlots:
    """Stand-in for DurableSlots: which consumers the controller keeps open."""

    def __init__(self, world):
        self.world = world
        self.children = {}

    def ensure(self, intent_id, slot):
        self.children[intent_id] = slot

    def stop(self, intent_id):
        self.children.pop(intent_id, None)


# --------------------------------------------------------------------------
# The world: ledger, service, controller, workers, clock, invariants.
# --------------------------------------------------------------------------


class World(LedgerCase):
    """Reuses the existing ledger and dstack fixtures; never runs as a test."""

    preview = operator_fixture.DstackOperatorTests.preview

    tracing = False

    def runTest(self):  # pragma: no cover - required by TestCase()
        pass

    def _trace_tick(self, result):
        minute = int((self.now - self.start) // 60)
        with self.repo.engine.connect() as connection:
            queued = connection.execute(select(jobs.c.status)).scalars().all()
            workers = {
                row["id"][7:15]: (row["state"], row["current_job_id"] is not None)
                for row in connection.execute(select(registered_workers)).mappings()
            }
        nodes = []
        with self.repo.engine.connect() as connection:
            for node in connection.execute(select(operator_nodes)).mappings():
                if (
                    node["intent_id"] in self._stop_seen
                    and node["runtime_state"] == "stopped"
                ):
                    continue
                binding = node["payload"].get("dstack", {})
                nodes.append(
                    "%s:%s/%s/%s"
                    % (
                        node["intent_id"][:8],
                        node["desired_state"],
                        binding.get("state"),
                        binding.get("reason_code"),
                    )
                )
        print(
            "t=%dm demand=%s/%s nodes=%s workers=%s jobs=%s"
            % (
                minute,
                result["demand_start"].get("state"),
                result["demand_start"].get("reason_code"),
                " ".join(nodes),
                workers,
                dict(Counter(queued)),
            )
        )
        for node in result["nodes"]:
            if node.get("code"):
                print("    node", node)

    def __init__(self, scenario, *, seed=0):
        super().__init__()
        self.scenario, self.seed = scenario, seed
        self.events, self.violations = [], []
        self.reasons = Counter()
        self.stats = Counter()
        self.offers = scenario.offers
        self.random = random.Random(seed)
        self._preparing = []
        self._seeding = False
        self._inventory_calls = 0
        self.trace = []
        self.metrics = Counter()
        self._history_ids = []
        # Concurrency model for provider latency (#1148): worker turns due at
        # a time, whether a tick runs, and who holds the capacity lock.
        self._due, self._seq = [], itertools.count()
        self._in_tick = self._in_workers = False
        self._depth, self._locked, self._open = 0, False, 0
        self._claimed_at = 0.0
        # Ground truth: job id -> (first failed collection, machine).
        self.collect_failing = {}
        from capacity_sim_evidence import Evidence

        self.evidence = Evidence(self)

    # Setup -----------------------------------------------------------------
    def build(self):
        self.setUp()
        if self.repo.engine.dialect.name == "sqlite":
            # A throwaway ledger needs no fsync; this only makes ticks faster.
            from sqlalchemy import event

            def fast(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA synchronous=OFF")
                cursor.execute("PRAGMA journal_mode=MEMORY")
                cursor.close()

            event.listen(self.repo.engine, "connect", fast)
            self.repo.engine.dispose()
        operator_fixture.DstackOperatorTests._setup_dstack(self)
        scenario = self.scenario
        self.client = SimProvider(self.store, self)
        self.capacity.client = self.client
        self.config["project"] = "sixnine"
        self.config["policy"].update(
            lifecycle_version=2,
            reservation_seconds=3600,
            max_instances=scenario.max_machines,
            max_physical_gpus=scenario.max_machines,
            expires_at=None,
            max_ttl_seconds=7200,
            idle_shutdown_seconds=scenario.idle_shutdown_seconds,
            observation_fresh_seconds=scenario.observation_fresh_seconds,
        )
        guard = scenario.test_guard
        pool = STAGING_POOL if guard else "dstack-test"
        self.config["policy"]["max_hourly_cost_microusd"] = 1_000_000 * (
            scenario.max_machines + 1
        )
        for name in {pool, "dstack-test"}:
            self.repo.configure_pool(
                name,
                max_instances=scenario.max_machines,
                max_physical_gpus=scenario.max_machines,
            )
        self.repo.configure_capacity(
            max_instances=scenario.max_machines, max_physical_gpus=scenario.max_machines
        )
        if guard:
            # The staging shape of DailyTestBudgetTests, with the task guard
            # template giving the admin defaults (cap and runtime window).
            self.settings.public_origin = "https://staging.inkseq.com"
            self.config["project"] = self.client.project = "sixnine-staging"
            self.config["policy"]["test_gpu_guard"] = {
                "version": 1,
                "timezone": "Australia/Sydney",
                "daily_limit_microusd": guard["task_limit_microusd"],
                "budget_account_id": ACCOUNT,
                "max_runtime_seconds": guard["max_runtime_seconds"],
                "stop_margin_seconds": 300,
            }
            self.repo.configure_budget(
                ACCOUNT, tenant_id=self.scope.tenant_id, limit_microusd=10_000_000_000
            )
            self._task_limit = guard["task_limit_microusd"]
        self.profile_ids = []
        base = dict(
            self.config["profiles"][0],
            max_ttl_seconds=7200,
            pool=pool,
            extra_reservation_microusd=1_000_000 if guard else 0,
        )
        profiles = []
        for owner in scenario.owner_profiles:
            account = "owner-budget" if owner == "superdan" else owner + "-budget"
            self.repo.configure_budget(
                account,
                tenant_id=self.scope.tenant_id,
                owner_id=owner,
                limit_microusd=10_000_000_000,
            )
            profile = dict(copy.deepcopy(base), id="fl-" + owner, owner_id=owner)
            profile["budget_account_ids"] = [account]
            if guard:
                profile["project_id"] = "project-inkseq-staging-" + owner
                profile["budget_account_ids"] = [ACCOUNT, account]
            profiles.append(profile)
            self.profile_ids.append(profile["id"])
        self.config["profiles"] = profiles
        demand = {"profile_ids": list(self.profile_ids)}
        if guard:
            demand["test_task_id"] = guard.get("task_id", "sim-task")
        if scenario.lookahead:
            demand["lookahead_version"] = 1
        self.config["policy"]["demand_start"] = demand
        # Job admission reads the protected operator file, like production.
        self.config_file = Path(self.temp.name) / "operator.json"
        environment = patch.dict(
            "os.environ", {"DSTACK_OPERATOR_CONFIG": str(self.config_file)}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self._load_service()
        if scenario.start_rules:
            self._save_start_rules(scenario.start_rules)
        profile = self.service.profiles[self.profile_ids[0]]
        from studio_platform.runtime_catalog import (
            engine_manifest,
            profile_output_delivery,
        )

        manifest = engine_manifest(operator_fixture.PROFILE, "fl")
        self.manifest = manifest
        self.policy = dict(
            enabled=True,
            backend="wangp-worker",
            dispatch_backend="hatchet-v1",
            pool=profile["pool"],
            configuration_id=profile["configuration_id"],
            model_id=profile["model_id"],
            engine_manifest_digest=manifest.digest,
            output_delivery=profile_output_delivery(operator_fixture.PROFILE),
            qualification=dict(status="accepted", verified_at=1.0, expires_at=None),
            reservation=dict(expected_runtime_s=1800, expires_at=None),
            envelope=dict(max_duration_seconds=4.0),
        )
        for target, value in (
            ("studio_platform.execution_profiles.selected_policy", self.policy),
            (
                "studio_platform.execution_policy.ExecutionPolicies.submission_allowed",
                True,
            ),
            # Media delivery evidence belongs to the collector tests, not
            # to capacity: completion uses one fixed validated artifact.
            ("studio_platform.inference.outputs.validate_delivery_evidence", None),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self._watch_controller_log()
        self._track_locks()
        self.runtime = SimRuntime(self)
        self.capacity.readiness = self.runtime.readiness
        self.slots = SimSlots(self)
        self.queue = TaskQueue(self.repo)
        self.set_demand(scenario.demand_enabled)
        self.demand_switch = scenario.demand_enabled
        self.new_controller()
        self.inflight = {}  # worker_id -> (lease, job_id, done_at)
        self.jobs_created = []
        self.job_actual = {}
        self.engine_failing = set()
        self.silent_jobs = set()
        if scenario.hosted is not None:
            self._build_hosted()
        if scenario.history_removed:
            self._seed_history(
                scenario.history_removed,
                actual=1_000_000 if scenario.history_invoiced else None,
            )
        if guard and scenario.spent_fraction:
            spent = int(guard["task_limit_microusd"] * scenario.spent_fraction)
            # Spent from the task window the simulation starts in (#1140).
            task = demand_task_id(demand, self.now + 86400)
            self._seed_history(1, task=task, actual=spent)
            self._prespent = spent
        # History predates the audited capacity policy (and its tenant daily
        # cap on unsettled exposure); the simulation starts a day later.
        self._adopt_settings()
        self.now += 86400

    def _save_start_rules(self, value):
        """The row the admin start-rules API writes, per demand profile."""
        from studio_platform.capacity_start_rules import DEFAULTS, StartRules
        from studio_platform.dstack_workspace import DstackWorkspace

        rules = StartRules(DstackWorkspace(self.service))
        with self.repo.transaction() as connection:
            for profile_id in self.profile_ids:
                identity, binding = rules._target(profile_id)
                payload = {
                    "tenant_id": self.scope.tenant_id,
                    "binding": binding,
                    "value": {**DEFAULTS, **value},
                }
                connection.execute(
                    insert(operator_policy).values(
                        id=identity,
                        version=1,
                        payload=payload,
                        actor="simulator",
                        updated_at=self.now,
                    )
                )

    def _seed_history(self, count, *, task=None, actual=None):
        """Historical runs through the real start path, then provider removal."""
        self._seeding = True
        try:
            for index in range(count):
                body = {"profile_id": self.profile_ids[0]}
                if task is not None:
                    body["test_task_id"] = task
                preview = self.service.preview(self.owner, body)
                node_id = self.service.start(
                    self.owner,
                    {"preview_id": preview["preview_id"]},
                    "history-%d" % index,
                )["node_id"]
                self._history_ids.append(node_id)
                machine = self.machine_for(node_id)
                self._stop_seen.add(node_id)
                # Spread over days, as on staging, under the tenant daily cap.
                self.now += 3 * 3600
                machine.stop_requested_at = machine.gone_at = self.now
                run = self.client.runs[machine.run_name]
                self.client._refresh(machine.run_name)
                binding = self.store.load(node_id)
                proof = {
                    "version": 1,
                    "backend": binding["backend"],
                    "run_id": run["id"],
                    "provider_instance_id": machine.instance_id,
                    "observed_at": self.now,
                    "inventory_observed_at": self.now,
                    "complete": True,
                    "absent": True,
                    "inventory_sha256": "a" * 64,
                }
                with self.repo.transaction() as connection:
                    node = (
                        connection.execute(
                            select(operator_nodes).where(
                                operator_nodes.c.intent_id == node_id
                            )
                        )
                        .mappings()
                        .one()
                    )
                    payload = copy.deepcopy(node["payload"])
                    payload["dstack"].update(
                        state="stopped",
                        dstack_status="terminated",
                        run_id=run["id"],
                        provider_instance_id=machine.instance_id,
                        estimated_cost_microusd=actual or 0,
                        observed_at=self.now,
                        provider_removal_proof=proof,
                    )
                    connection.execute(
                        update(operator_nodes)
                        .where(operator_nodes.c.intent_id == node_id)
                        .values(
                            payload=payload,
                            desired_state="stopped",
                            runtime_state="stopped",
                        )
                    )
                    connection.execute(
                        update(instance_intents)
                        .where(instance_intents.c.id == node_id)
                        .values(
                            state="destroyed", provider_instance_id=machine.instance_id
                        )
                    )
                    if actual is not None:
                        self.repo._settle(connection, "instance", node_id, actual)
        finally:
            self._seeding = False

    def _watch_controller_log(self):
        """A swallowed exception inside a controller tick is a bug, not a reason.

        A test-budget row the projection could not read is a reported reason
        (#1148 item 4): the warning names the row, never the money.
        """
        world = self

        class Handler(logging.Handler):
            def emit(self, record):
                message = record.getMessage()
                if message.startswith("dstack_controller_observation_failed"):
                    kind = message.rsplit("error_type=", 1)[-1]
                    world.reasons["controller_exception:" + kind] += 1
                    if kind not in world._exceptions:
                        world._exceptions.add(kind)
                        world.violate("controller_exception", kind)
                elif message.startswith("dstack_test_budget_row_unconfirmed"):
                    world.reasons["budget_row_unconfirmed"] += 1

        self._exceptions = set()
        for name in (
            "studio_platform.dstack_controller",
            "studio_platform.dstack_test_budget",
        ):
            logger = logging.getLogger(name)
            handler, level = Handler(logging.WARNING), logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.WARNING)

            def restore(logger=logger, handler=handler, level=level):
                logger.removeHandler(handler)
                logger.setLevel(level)

            self.addCleanup(restore)

    def _track_locks(self):
        """Know when the global capacity lock or any connection is held (#1148).

        Only the provider-latency model reads this: a worker's consumer write
        needs the capacity lock, so it waits while the controller holds it.
        """
        from sqlalchemy import event

        world, repo = self, self.repo
        transaction, lock = repo.transaction, repo._lock_capacity

        @contextmanager
        def tracked():
            world._depth += 1
            try:
                with transaction() as connection:
                    yield connection
            finally:
                world._depth -= 1
                if not world._depth:
                    world._locked = False

        def locking(connection):
            world._locked = True
            return lock(connection)

        def checkout(*_args):
            world._open += 1

        def checkin(*_args):
            world._open -= 1

        repo.transaction, repo._lock_capacity = tracked, locking
        event.listen(repo.engine, "checkout", checkout)
        event.listen(repo.engine, "checkin", checkin)

    def provider_wait(self, kind):
        """One provider read takes ``provider_latency`` simulated seconds.

        Workers keep running during the wait, unless the controller holds the
        global capacity lock (their consumer write needs it) or SQLite holds
        an open connection (then they run when it closes).
        """
        latency = self.scenario.provider_latency
        if not latency or self._seeding or self._in_workers or not self._in_tick:
            return
        if self._locked:
            self.metrics["provider_seconds_under_capacity_lock"] += latency
            self.reasons["provider_read_under_capacity_lock:" + kind] += 1
            if ("locked_read", kind) not in self._flagged:
                self._flagged.add(("locked_read", kind))
                self.violate("provider_read_under_capacity_lock", kind)
        elif self._open:
            self.reasons["provider_read_with_open_connection:" + kind] += 1
        end = self.now + latency
        if not self._open:
            self._run_workers(end)
        self.now = max(self.now, end)

    def _metric_max(self, name, value):
        self.metrics[name] = max(self.metrics[name], round(value, 1))

    def _run_workers(self, until):
        """Worker turns due by ``until``, each at its time or once it may run."""
        while self._due and self._due[0][0] <= until:
            at, _, phase = heapq.heappop(self._due)
            self.now = max(self.now, at)
            self._metric_max("max_worker_lock_wait_s", self.now - at)
            self._in_workers = True
            try:
                if phase == "claim":
                    self._claimed_at = self.now
                    self.work()
                    if self._preparing:
                        heapq.heappush(
                            self._due,
                            (
                                self.now + self.scenario.prepare_seconds,
                                next(self._seq),
                                "submit",
                            ),
                        )
                else:
                    self._metric_max(
                        "max_claim_to_submit_s", self.now - self._claimed_at
                    )
                    self.submit()
            finally:
                self._in_workers = False

    def _build_hosted(self):
        """The hosted route: its switch file, live slots and an unknown balance.

        The catalog and tier of a simulated request are scripted per batch;
        the routing rules under test are the real ones.
        """
        from studio_platform.control import WorkerSpec
        from studio_platform.providers import hosted_models
        from studio_platform.scheduling import rebind

        self.settings.data_dir = str(Path(self.temp.name) / "data")
        folder = Path(self.settings.data_dir) / "runtime-secrets"
        folder.mkdir(parents=True)
        (folder / "everyframe-routing.json").write_text(
            json.dumps(self.scenario.hosted), encoding="utf-8"
        )
        self.settings.everyframe_enabled = True
        # No vendor read: the balance stays unknown, which leaves hosted open.
        before = dict(rebind._balance)
        rebind._balance.update(usd=None, read_at=float("inf"))
        self.addCleanup(rebind._balance.update, before)
        model = hosted_models.MODELS[HOSTED_MODEL]
        for target, value in (
            (
                "studio_platform.scheduling.rebind.chain",
                lambda request: (model.id,) if request.get("sim_hostable") else (),
            ),
            (
                "studio_platform.scheduling.rebind.tier_of",
                lambda job: job["request"].get("sim_tier"),
            ),
        ):
            patcher = patch(target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hosted_workers, self.hosted_inflight = [], {}

    def hosted_alive(self):
        """Real hosted slots heartbeat every few seconds: live at every tick."""
        if self.scenario.hosted is None:
            return
        from studio_platform.control import WorkerSpec
        from studio_platform.providers import hosted_models

        control = self.controller.control
        if self.hosted_workers:
            for worker in self.hosted_workers:
                if worker not in self.hosted_inflight:
                    try:
                        control.heartbeat(
                            worker, control.get(worker)["fence"], lease_seconds=900
                        )
                    except Conflict:
                        self.reasons["hosted_slot_conflict"] += 1
            return
        model = hosted_models.MODELS[HOSTED_MODEL]
        recipes = tuple(
            dict.fromkeys(
                (
                    *hosted_models.RECIPES,
                    self.manifest.document["generation_recipe_id"],
                )
            )
        )
        for index in range(self.scenario.hosted_slots):
            worker = "dbos-everyframe-sim-%d" % index
            self.controller.control.register(
                WorkerSpec(
                    worker,
                    hosted_models.POOL,
                    hosted_models.PROVIDER,
                    worker,
                    (),
                    recipes,
                    model.id,
                    model.configuration_id,
                    hosted_models.BACKEND,
                )
            )
            self.controller.control.mark_ready(worker, upstream_idle_confirmed=True)
            self.hosted_workers.append(worker)

    def hosted_work(self):
        """The hosted slots: claim a routed job, render, deliver."""
        if self.scenario.hosted is None:
            return
        from studio_platform.providers.hosted_models import POOL

        control, queue = self.controller.control, self.queue
        for worker in self.hosted_workers:
            try:
                current = self.hosted_inflight.get(worker)
                fence = control.get(worker)["fence"]
                if current is None:
                    claim = control.claim(worker, POOL, lease_seconds=900)
                    if claim is None:
                        continue
                    queue.begin_submission(claim.lease)
                    queue.record_submitted(
                        claim.lease, "render-" + claim.lease.attempt_id[:12]
                    )
                    # A queue the hosted route is draining is not blocked.
                    self._blocked_since = None
                    self.hosted_inflight[worker] = (
                        claim.lease,
                        claim.job["id"],
                        self.now + self.scenario.hosted_seconds,
                    )
                    continue
                lease, job_id, done_at = current
                queue.heartbeat(lease, lease_seconds=900)
                control.heartbeat(worker, fence, lease_seconds=900)
                if self.now < done_at:
                    continue
                queue.begin_collection(lease)
                queue.complete(lease, [ARTIFACT], actual_cost_microusd=0)
                self.hosted_inflight.pop(worker)
                control.observe(worker, job_id)
            except (LeaseLost, Conflict, InvalidTransition) as error:
                self.reasons["hosted_lease_lost:" + type(error).__name__] += 1
                self.hosted_inflight.pop(worker, None)

    def _check_hosted(self):
        from studio_platform.providers.hosted_models import POOL

        scenario = self.scenario
        if scenario.hosted is None:
            return
        with self.repo.engine.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    select(jobs).where(jobs.c.pool == POOL)
                ).mappings()
            ]
        done = sum(row["status"] == "succeeded" for row in rows)
        self.metrics["hosted_jobs"] = done
        waits = [row["execution_plan"]["routed_at"] - row["created_at"] for row in rows]
        if waits:
            self.metrics["hosted_wait_max_s"] = int(max(waits))
        if scenario.expect_hosted is not None and done != scenario.expect_hosted:
            self.violate(
                "hosted_count_mismatch",
                "%d jobs finished hosted, expected %d" % (done, scenario.expect_hosted),
            )
        limit = scenario.expect_hosted_within
        if limit is not None and waits and max(waits) > limit:
            self.violate(
                "hosted_route_late",
                "a job waited %d s for the hosted route, at most %d expected"
                % (max(waits), limit),
            )

    def _load_service(self):
        self.config_file.write_text(json.dumps(self.config), encoding="utf-8")
        # Like the production file: not readable by others (_protected_read).
        self.config_file.chmod(0o600)
        self.service = DstackOperator(
            self.repo, self.settings, self.capacity, self.config
        )
        self.service.provider_inventory = self.client.provider_inventory

    def _adopt_settings(self):
        """The audited admin capacity policy, as staging and production hold it."""
        from studio_platform.capacity_settings import DEFAULTS
        from studio_platform.repository import spending_settings
        from studio_platform.spending_limits import ADMIN_OWNER

        scenario = self.scenario
        value = dict(DEFAULTS, max_machines=scenario.max_machines)
        if scenario.test_guard:
            value.update(
                test_task_limit_microusd=scenario.test_guard["task_limit_microusd"],
                max_runtime_seconds=scenario.test_guard["max_runtime_seconds"],
            )
        with self.repo.transaction() as connection:
            connection.execute(
                insert(spending_settings).values(
                    tenant_id=self.scope.tenant_id,
                    owner_id=ADMIN_OWNER,
                    version=1,
                    value=value,
                    actor_id="simulator",
                    updated_at=self.now,
                )
            )

    def new_controller(self):
        self.controller = DstackController(
            self.service, self.runtime, worker_process=self.slots
        )

    def set_demand(self, value):
        with self.repo.transaction() as connection:
            row = (
                connection.execute(
                    select(operator_policy).where(
                        operator_policy.c.id == policy_key(self.scope.tenant_id)
                    )
                )
                .mappings()
                .first()
            )
            values = dict(
                payload={"tenant_id": self.scope.tenant_id, "enabled": value},
                actor="simulator",
                updated_at=self.now,
            )
            if row:
                connection.execute(
                    update(operator_policy)
                    .where(operator_policy.c.id == row["id"])
                    .values(**values, version=row["version"] + 1)
                )
            else:
                connection.execute(
                    insert(operator_policy).values(
                        id=policy_key(self.scope.tenant_id), version=1, **values
                    )
                )

    # Helpers ----------------------------------------------------------------
    def log(self, kind, machine):
        self.trace.append((self.now, kind, machine.intent_id[:8]))

    def worker_id(self, intent_id):
        return "dstack-" + intent_id.replace("-", "")

    def machine_for(self, intent_id):
        return next(
            (m for m in self.client.machines.values() if m.intent_id == intent_id), None
        )

    def inventory_mode(self):
        mode = self.scenario.inventory
        self._inventory_calls += 1
        if mode == "slow":
            return "stale" if self._inventory_calls % 2 else "fresh"
        return mode

    def add_job(self, batch, index):
        from studio_platform.repository import Scope

        from studio_platform.dstack_demand import demand_admission

        scope = Scope(self.scope.tenant_id, batch.owner, "project-1")
        profile = self.service.profiles[self.profile_ids[0]]
        compiled = dict(
            recipe_id=self.manifest.document["generation_recipe_id"],
            deployment_profile_id=operator_fixture.PROFILE,
            request=dict(model=profile["model_id"], mode=batch.mode),
        )
        if self.scenario.hosted is not None:
            compiled.update(sim_tier=batch.tier, sim_hostable=batch.hostable)
        execution = dict(
            self.policy,
            policy_hash=request_hash(self.policy),
            expected_runtime_s=batch.expected_runtime_s,
        )
        marker = demand_admission(
            self.settings, self.repo, scope, compiled, self.policy
        )
        if marker is not None:
            execution.update(demand_start=marker, admission_state="queued")
        else:
            self.reasons["admission:no_demand_marker"] += 1
        plan = self.repo.create_plan(
            scope,
            compiled,
            execution,
            expires_at=self.now + 10 * 86400,
            estimated_cost_microusd=0,
        )
        from studio_platform.repository import BudgetExceeded

        try:
            created = self.repo.create_job(
                scope, plan["id"], "sim-%d-%s" % (index, plan["id"])
            )
        except (BudgetExceeded, Conflict) as error:
            # The user is told no: count it, it is a refusal with a reason.
            code = error.args[0] if error.args else type(error).__name__
            self.reasons["admission_refused:" + str(code)] += 1
            if self.scenario.expect_capacity:
                self.violate("job_admission_refused", str(code))
            return None
        self.jobs_created.append(created["id"])
        self.job_actual[created["id"]] = batch.actual_runtime_s
        if batch.engine_fails:
            self.engine_failing.add(created["id"])
        if batch.silent:
            self.silent_jobs.add(created["id"])
        return created

    # One tick -----------------------------------------------------------------
    def apply_events(self):
        for event in self.scenario.events:
            if id(event) in self._done_events or self.now < self.start + event.at:
                continue
            self._done_events.add(id(event))
            if event.action == "restart_app":
                # A deploy: the controller process and its DBOS slots restart.
                # Registered workers, nodes and jobs stay in the database.
                for machine in self.client.machines.values():
                    machine.restarted_at = self.now
                self.slots = SimSlots(self)
                self.new_controller()
            elif event.action == "demand_off":
                self.set_demand(False)
                self.demand_switch = False
            elif event.action == "demand_on":
                self.set_demand(True)
                self.demand_switch = True
            elif event.action == "offers_off":
                self.offers = False
            elif event.action == "offers_on":
                self.offers = True
            elif event.action == "corrupt_reservation":
                # One historical test-task reservation becomes unreadable, e.g.
                # a pool name the projection does not know (#1148 item 4).
                with self.repo.transaction() as connection:
                    connection.execute(
                        update(instance_intents)
                        .where(instance_intents.c.id == self._history_ids[-1])
                        .values(**event.value)
                    )
            elif event.action == "edit_demand_config":
                # An operator edits operator.json and restarts the controller.
                self._edit_demand_config(event.value)
            else:
                raise ValueError("unknown_event:" + event.action)
            self.reasons["event:" + event.action] += 1

    def _edit_demand_config(self, value):
        demand = self.config["policy"]["demand_start"]
        value = dict(value)
        # {profile id: owner} for newly listed profiles owned by someone else
        # than the first profile's owner (#1159).
        owners = value.pop("profile_owners", {})
        known = {profile["id"] for profile in self.config["profiles"]}
        for profile_id in value.get("profile_ids", ()):
            if profile_id not in known:
                # A newly listed profile: same machine type, another id.
                profile = dict(copy.deepcopy(self.config["profiles"][0]), id=profile_id)
                owner = owners.get(profile_id)
                if owner is not None and owner != profile["owner_id"]:
                    account = owner + "-budget"
                    self.repo.configure_budget(
                        account,
                        tenant_id=self.scope.tenant_id,
                        owner_id=owner,
                        limit_microusd=10_000_000_000,
                    )
                    profile["owner_id"] = owner
                    accounts = [account]
                    if self.scenario.test_guard:
                        profile["project_id"] = "project-inkseq-staging-" + owner
                        accounts = [ACCOUNT, account]
                    profile["budget_account_ids"] = accounts
                self.config["profiles"].append(profile)
        if value.get("test_task_id", demand.get("test_task_id")) != demand.get(
            "test_task_id"
        ):
            # A fresh task cap: spend before the rotation was the old task's.
            self._prespent = -self._billed_microusd()
        demand.update(value)
        self._load_service()
        self.new_controller()

    def arrive(self):
        for number, batch in enumerate(self.scenario.jobs):
            for index in range(batch.count):
                key = (number, index)
                if key in self._arrived:
                    continue
                if self.now < self.start + batch.at + index * batch.spacing:
                    continue
                self._arrived.add(key)
                self.add_job(batch, len(self._arrived))

    def work(self):
        """The dial-out worker loop of every slot the controller keeps open."""
        control, queue = self.controller.control, self.queue
        for intent_id, slot in list(self.slots.children.items()):
            machine = self.machine_for(intent_id)
            if machine is None or not machine.alive(self.now):
                continue
            if not machine.worker_alive(self):
                continue
            worker_id = slot.spec.worker_id
            try:
                worker = control.get(worker_id)
            except Exception:
                continue
            self._consume(machine, worker_id)
            current = self.inflight.get(worker_id)
            if current is not None:
                self._progress(machine, worker_id, current)
                continue
            if worker["expires_at"] > self.now and worker["state"] not in (
                "unknown",
                "retired",
            ):
                try:
                    control.heartbeat(worker_id, worker["fence"])
                except Conflict:
                    continue
            control.recover_expired()
            queue.recover_expired(summary=True)
            claim, purpose = None, None
            for phase in ("collect", "reconcile", "generate"):
                claim = control.claim(
                    worker_id, slot.spec.pool, purpose=phase, lease_seconds=900
                )
                if claim:
                    purpose = phase
                    break
            if claim is None:
                continue
            # PostgresSlotRunner republishes the consumer at the new fence.
            self._consume(machine, worker_id)
            job, lease = claim.job, claim.lease
            slot_fence = control.get(worker_id)["fence"]
            if purpose == "generate":
                # Input transfer happens between the claim and the POST.
                self._preparing.append((machine, worker_id, job, lease, slot_fence))
                continue
            if machine.busy_job != job["id"]:
                # The machine has no record of this task: it was lost with
                # an earlier host. Leave reconciliation to the real watchdog.
                self.reasons["reconcile:task_unknown"] += 1
            current = (lease, job["id"], slot_fence)
            self.inflight[worker_id] = current
            self._progress(machine, worker_id, current)

    def _consume(self, machine, worker_id):
        from studio_platform.postgres_dispatch import record_consumer

        def live_readiness():
            # The slot runner's own native readiness read (WanGPBackend).
            return self.runtime.readiness(self.store.load(machine.intent_id), None)

        try:
            record_consumer(
                self.repo,
                self.controller.control,
                SimpleNamespace(
                    kind="wangp-worker",
                    slot_key=machine.intent_id,
                    expected_incarnation=machine.incarnation(),
                    live_readiness=live_readiness,
                ),
                worker_id,
                probe=True,
            )
        except (ValueError, Conflict):
            self.reasons["consumer:binding_changed"] += 1

    def submit(self):
        """The worker's last window check right before the paid POST."""
        from studio_platform.capacity_reasons import submission_open

        control, queue = self.controller.control, self.queue
        preparing, self._preparing = self._preparing, []
        for machine, worker_id, job, lease, slot_fence in preparing:
            try:
                self._consume(machine, worker_id)
                why = control.submission_block_reason(job)
                # The real worker's check: it counts the exact reason by worker.
                if not submission_open(control, job):
                    # The job row stores only worker_window_closed (#1140 Q1).
                    self.reasons["deferred:" + str(why or "worker_state")] += 1
                    self.evidence.deferred(machine, str(why or "window_closed"))
                    queue.defer_unsubmitted(
                        lease, retry_after_s=30, error_code="worker_window_closed"
                    )
                    control.observe(worker_id, job["id"])
                    continue
                queue.begin_submission(lease)
            except (LeaseLost, Conflict, InvalidTransition) as error:
                self.reasons["worker_lease_lost:" + type(error).__name__] += 1
                continue
            if machine.busy_job not in (None, job["id"]):
                self.violate("machine_double_booked", job["id"][:8])
            machine.busy_job = job["id"]
            machine.job_started_at = self.now
            machine.job_done_at = self.now + self.job_actual.get(job["id"], 240)
            queue.record_submitted(lease, "task-" + lease.attempt_id[:12])
            current = (lease, job["id"], slot_fence)
            self.inflight[worker_id] = current

    def _progress(self, machine, worker_id, current):
        from studio_platform.capacity_watchdog import record_engine_progress

        control, queue = self.controller.control, self.queue
        lease, job_id, slot_fence = current
        try:
            queue.heartbeat(lease, lease_seconds=900)
            control.heartbeat(worker_id, slot_fence, lease_seconds=900)
            if machine.busy_job != job_id:
                return  # Held for reconciliation; nothing runs on this host.
            if job_id in self.engine_failing:
                # The worker's own path (WorkerRunner): the host answers unknown
                # with the failure class, which is written on the job once.
                if self.now >= machine.job_started_at + 60:
                    from studio_platform.capacity_watchdog import record_engine_failure

                    record_engine_failure(self.repo, queue, lease, ENGINE_FAILURE)
                return
            if job_id in self.silent_jobs or machine.script.silent:
                return  # Leases renew; the engine never says anything.
            if self.now < machine.job_done_at:
                record_engine_progress(self.repo, queue, lease)
                return
            job = self.repo.get_job(self._scope(job_id), job_id)
            if job["status"] in ("submitting", "running"):
                queue.begin_collection(lease)
            if machine.script.collect_fails:
                # The worker's own failure path (WorkerRunner._run_once):
                # count the failure, release the lease, keep the binding.
                self.reasons["collect_failed"] += 1
                self.collect_failing.setdefault(job_id, (self.now, machine))
                queue.collection_failed(lease)
                self.inflight.pop(worker_id, None)
                control.observe(worker_id, job_id)
                return
            queue.complete(lease, [ARTIFACT], actual_cost_microusd=0)
            machine.last_completed_at = self.now
            machine.busy_job = None
            self.inflight.pop(worker_id, None)
            control.observe(worker_id, job_id)
        except (LeaseLost, Conflict, InvalidTransition) as error:
            self.reasons["worker_lease_lost:" + type(error).__name__] += 1
            self.inflight.pop(worker_id, None)
            if machine.busy_job == job_id:
                machine.busy_job = None

    def _scope(self, job_id):
        from studio_platform.repository import Scope

        with self.repo.engine.connect() as connection:
            row = (
                connection.execute(select(jobs).where(jobs.c.id == job_id))
                .mappings()
                .one()
            )
        return Scope(row["tenant_id"], row["owner_id"], row["project_id"])

    def tick(self):
        """One controller pass, then the always-polling workers react to it."""
        scenario, began = self.scenario, self.now
        self._tick_at = began
        step = scenario.tick_seconds
        if scenario.tick_jitter:
            step = self.random.uniform(*scenario.tick_jitter)
        self.apply_events()
        self.arrive()
        self.hosted_alive()
        if scenario.provider_latency:
            # Workers poll every second; one claims 2 s into the tick and
            # submits after preparing, concurrently with the controller.
            heapq.heappush(self._due, (began + 2, next(self._seq), "claim"))
            self._in_tick = True
            try:
                result = self.controller.tick()
            finally:
                self._in_tick = False
            self.observe_result(result)
            self._run_workers(float("inf"))
            self.hosted_work()
            self.now = max(self.now, began + max(step, 2 + scenario.prepare_seconds))
            self.check_tick(self.now - began)
            self.account(self.now - began)
            return
        result = self.controller.tick()
        self.observe_result(result)
        self.now += 2  # Workers poll every second; one claims soon after.
        self.work()
        self.hosted_work()
        self.now += scenario.prepare_seconds
        self.submit()
        self.now = began + max(step, 2 + scenario.prepare_seconds)
        self.check_tick(self.now - began)
        self.account(self.now - began)

    # Observations and invariants -------------------------------------------
    def violate(self, code, detail=""):
        if not self.violations:
            self._first_violation_at = self.now
        self.violations.append(
            "t=%dm %s %s" % ((self.now - self.start) // 60, code, detail)
        )

    def observe_result(self, result):
        from studio_platform.dstack_demand_policy import read_runtime

        with self.repo.engine.connect() as connection:
            row = (
                connection.execute(
                    select(operator_policy).where(
                        operator_policy.c.id == policy_key(self.scope.tenant_id)
                    )
                )
                .mappings()
                .one()
            )
            enabled = read_runtime(connection, self.scope.tenant_id)["enabled"]
        if not enabled and row["actor"] != "simulator" and not self._paused:
            # Someone other than the scenario switched demand start off.
            self._paused = True
            self.reasons["demand_switched_off_by:" + row["actor"]] += 1
        elif enabled:
            self._paused = False
        if self.tracing:
            self._trace_tick(result)
        demand = result.get("demand_start", {})
        state, reason = demand.get("state"), demand.get("reason_code")
        if state == "blocked":
            self.reasons["demand:" + str(reason)] += 1
            self._demand_reason = reason
            self.evidence.demand_refused(reason, demand.get("detail_code"))
            if not reason:
                self.violate("refusal_without_reason", "demand_start blocked")
        elif state in ("accepted", "disabled"):
            self.reasons["demand:" + state] += 1
            self._demand_reason = state
        if state == "accepted":
            self._check_start_owner(demand["node_id"])
        for node in result.get("nodes", []):
            if node.get("state") == "reconciliation_required":
                self.reasons["node:" + str(node.get("code"))] += 1
                if not node.get("code"):
                    self.violate("refusal_without_reason", node.get("node_id", "")[:8])
            elif node.get("code"):
                self.reasons["node:" + node["code"]] += 1

    def _check_start_owner(self, node_id):
        """Whose GPU account pays for a start, against whose job asked for it."""
        from studio_platform.operator_capacity import operator_commands

        with self.repo.engine.connect() as connection:
            node = (
                connection.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == node_id)
                )
                .mappings()
                .one()
            )
            journals = connection.execute(
                select(operator_commands.c.payload).where(
                    operator_commands.c.kind == "dstack_demand",
                    operator_commands.c.updated_at == self.now,
                )
            ).scalars()
            owners = set()
            for payload in journals:
                job = connection.execute(
                    select(jobs.c.owner_id).where(jobs.c.id == payload.get("job_id"))
                ).scalar_one_or_none()
                if job:
                    owners.add(job)
        payer = node["payload"].get("owner_id")
        if owners and payer not in owners:
            self.reasons["start:cross_owner_profile"] += 1
            recorded = any(
                key in node["payload"] or key in node["payload"].get("selection", {})
                for key in ("cross_owner_fallback", "demand_job_owner_id")
            )
            if self.scenario.check_owner_funding and not recorded:
                self.violate(
                    "cross_owner_funding_unrecorded",
                    "%s job of %s started on %s's profile"
                    % (node_id[:8], "/".join(sorted(owners)), payer),
                )

    def _queued(self):
        with self.repo.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(jobs).where(jobs.c.status == "queued")
                ).mappings()
            ]

    def _deadline(self, machine):
        """Latest second a machine may still finish work (independent arithmetic)."""
        with self.repo.engine.connect() as connection:
            node = (
                connection.execute(
                    select(operator_nodes).where(
                        operator_nodes.c.intent_id == machine.intent_id
                    )
                )
                .mappings()
                .one()
            )
            intent = (
                connection.execute(
                    select(instance_intents).where(
                        instance_intents.c.id == machine.intent_id
                    )
                )
                .mappings()
                .one()
            )
        payload = node["payload"]
        guard = payload.get("lifecycle", {}).get("test_gpu_guard")
        if guard:
            return (
                intent["created_at"]
                + guard["max_runtime_seconds"]
                - guard["stop_margin_seconds"]
            )
        deadline = payload.get("hard_deadline")
        return deadline if isinstance(deadline, (int, float)) else float("inf")

    def check_tick(self, step):
        now, scenario = self.now, self.scenario
        for machine in self.client.machines.values():
            machine.advance(now)
        live = [m for m in self.client.machines.values() if m.alive(now)]
        if len(live) > scenario.max_machines:
            self.violate(
                "machine_count_exceeded", "%d>%d" % (len(live), scenario.max_machines)
            )
        queued = self._queued()
        for machine in live:
            booted = machine.phase(now)[0] in ("ready", "load_failed")
            serving = machine.busy_job is not None
            deadline = self._deadline(machine)
            admittable = [
                job
                for job in queued
                if now + job["expected_runtime_s"] + 300 < deadline
            ]
            machine.idle_with_work = (
                machine.idle_with_work + step
                if booted and not serving and admittable
                else 0
            )
            machine.idle_without_work = (
                machine.idle_without_work + step
                if not serving and not admittable
                else 0
            )
            key = machine.intent_id[:8]
            if (
                machine.idle_with_work > scenario.health_window_seconds
                and ("idle_queue", key) not in self._flagged
            ):
                self._flagged.add(("idle_queue", key))
                self.violate(
                    "paid_idle_with_admittable_queue",
                    "%s idle %dm while %d jobs wait"
                    % (key, machine.idle_with_work // 60, len(admittable)),
                )
            if (
                machine.idle_without_work > scenario.idle_grace_seconds
                and ("idle_empty", key) not in self._flagged
            ):
                self._flagged.add(("idle_empty", key))
                self.violate(
                    "kept_on_without_admittable_work",
                    "%s for %dm" % (key, machine.idle_without_work // 60),
                )
        pending = [
            m
            for m in self.client.machines.values()
            if m.gone_at is None and not m.script.no_capacity
        ]
        if queued and not pending and scenario.expect_capacity and self.demand_switch:
            self._blocked_since = self._blocked_since or now
            if now - self._blocked_since > 1800 and not self._flagged_block:
                self._flagged_block = True
                self.violate(
                    "queue_blocked_with_no_machine",
                    "%d queued, demand says %s" % (len(queued), self._demand_reason),
                )
        else:
            self._blocked_since = None
        self._check_stuck_jobs(queued)
        self._check_stop_reasons()
        self._check_spend()
        self.evidence.detect(queued, live)
        self.evidence.check()

    def _orphans(self):
        """(job_id, machine) for active jobs whose machine the provider removed."""
        with self.repo.engine.connect() as connection:
            active = list(
                connection.execute(
                    select(jobs.c.id, attempts.c.worker_id)
                    .select_from(
                        jobs.join(attempts, attempts.c.id == jobs.c.current_attempt_id)
                    )
                    .where(
                        jobs.c.status.in_(
                            (
                                "submitting",
                                "running",
                                "collecting",
                                "submission_unknown",
                            )
                        )
                    )
                )
            )
        hosts = {self.worker_id(m.intent_id): m for m in self.client.machines.values()}
        for job_id, worker_id in active:
            machine = hosts.get(worker_id)
            if (
                machine is not None
                and machine.gone_at is not None
                and machine.removed_first()
            ):
                yield job_id, machine

    def _check_stuck_jobs(self, queued):
        """Queued work eventually starts; running work never outlives its host."""
        now, scenario = self.now, self.scenario
        billing = sum(
            m.gone_at is None and m.allocated_at() is not None
            for m in self.client.machines.values()
        )
        # A full fleet may legitimately leave work queued; spare room may not.
        if (
            scenario.expect_capacity
            and self.demand_switch
            and billing < scenario.max_machines
        ):
            for job in queued:
                key = ("queue_age", job["id"])
                if (
                    now - job["created_at"] > scenario.max_queue_age_seconds
                    and key not in self._flagged
                ):
                    self._flagged.add(key)
                    self.violate(
                        "queued_too_long",
                        "%s queued %dm (%s)"
                        % (
                            job["id"][:8],
                            (now - job["created_at"]) // 60,
                            job.get("error_code") or self._demand_reason,
                        ),
                    )
        with self.repo.engine.connect() as connection:
            active = list(
                connection.execute(
                    select(jobs.c.id, jobs.c.lease_worker_id, attempts.c.worker_id)
                    .select_from(
                        jobs.join(attempts, attempts.c.id == jobs.c.current_attempt_id)
                    )
                    .where(
                        jobs.c.status.in_(
                            (
                                "submitting",
                                "running",
                                "collecting",
                                "submission_unknown",
                            )
                        )
                    )
                )
            )
        hosts = {self.worker_id(m.intent_id): m for m in self.client.machines.values()}
        for job_id, _, worker_id in active:
            machine = hosts.get(worker_id)
            key = ("orphan", job_id)
            if (
                machine is not None
                and machine.gone_at is not None
                and now - machine.gone_at > 2700
                and key not in self._flagged
            ):
                self._flagged.add(key)
                self.violate(
                    "job_held_by_removed_machine",
                    "%s still active %dm after its machine left"
                    % (job_id[:8], (now - machine.gone_at) // 60),
                )

    def _check_stop_reasons(self):
        with self.repo.engine.connect() as connection:
            for node in connection.execute(select(operator_nodes)).mappings():
                if node["intent_id"] in self._stop_seen:
                    continue
                if node["desired_state"] != "stopped":
                    continue
                self._stop_seen.add(node["intent_id"])
                payload = node["payload"]
                binding = payload.get("dstack", {})
                reason = (
                    payload.get("stop_reason_code")
                    or payload.get("capacity_watchdog", {}).get("reason")
                    # The provider ended the run itself; the observation says so.
                    or binding.get("state") == "stopped"
                    and binding.get("reason_code")
                )
                self.reasons["stop:" + str(reason)] += 1
                if not reason:
                    self.violate("stop_without_reason", node["intent_id"][:8])
                self._check_window_used(node, reason)
                self._check_idle_stop(node, reason)

    def _check_idle_stop(self, node, reason):
        """An idle stop keeps the machine for exactly its idle rule (#688 rent-or-buy).

        Measured from the machine's last finished job (or its readiness), to
        the controller tick that asked for the stop: not earlier than the rule
        and at most one tick later.
        """
        expected = self.scenario.expect_idle_stop_seconds
        if expected is None or reason != "dstack_idle_expired":
            return
        machine = self.machine_for(node["intent_id"])
        if machine is None or machine.allocated_at() is None:
            return
        phase, ready_at, _ = machine.phase(self._tick_at)
        if phase != "ready":
            return
        self._idle_stops += 1
        idle = self._tick_at - max(machine.last_completed_at or 0, ready_at)
        if not expected <= idle <= expected + self.scenario.tick_seconds:
            self.violate(
                "idle_stop_off_rule",
                "%s stopped after %ds idle, rule %ds"
                % (node["intent_id"][:8], idle, expected),
            )

    def _check_window_used(self, node, reason):
        """A test machine is not let go while it could still run waiting work.

        Independent arithmetic with the true job runtimes: a machine whose test
        runtime window still fits two of the queued jobs back to back (each
        with the 120 s completion margin) must not stop for idleness or for
        its runtime window (#1148 item 3).
        """
        guard = node["payload"].get("lifecycle", {}).get("test_gpu_guard")
        if not guard or reason not in (
            "dstack_idle_expired",
            "dstack_test_runtime_limit_reached",
        ):
            return
        machine = self.machine_for(node["intent_id"])
        if machine is None or machine.phase(self.now)[0] != "ready":
            return
        deadline = self._deadline(machine)
        fits = [
            job["id"]
            for job in self._queued()
            if self.now + 2 * (self.job_actual.get(job["id"], 240) + 120) < deadline
        ]
        if fits:
            self.violate(
                "test_window_unused",
                "%s stopped (%s) with %dm of window left and %d jobs waiting"
                % (
                    node["intent_id"][:8],
                    reason,
                    (deadline - self.now) // 60,
                    len(fits),
                ),
            )

    def _check_spend(self):
        guard = self.scenario.test_guard
        if not guard:
            return
        billed = self._billed_microusd()
        window = demand_task_id(self.config["policy"]["demand_start"], self.now)
        if window != self._spend_window:
            if self._spend_window is not None:
                # A new Sydney day is a fresh task cap (#1140).
                self._prespent = -billed
            self._spend_window = window
        limit = self._task_limit
        if billed + self._prespent > limit and not self._flagged_spend:
            self._flagged_spend = True
            self.violate("test_cap_exceeded", "billed past the task cap")

    def _check_host_policy(self):
        """Count speed-gate denies and health alerts; check what the scenario expects."""
        with self.repo.engine.connect() as connection:
            denies = connection.execute(
                select(outbox.c.payload).where(
                    outbox.c.event_type == "operator.host_policy_updated"
                )
            ).scalars()
            by_reason = Counter(
                row.get("reason_code")
                for row in denies
                if str(row.get("reason_code")).startswith("automatic_")
            )
            denies = by_reason["automatic_slow_download"]
            alerts = Counter(
                row.get("code")
                for row in connection.execute(
                    select(outbox.c.payload).where(
                        outbox.c.event_type == "capacity.health_alert"
                    )
                ).scalars()
            )
        for reason, count in by_reason.items():
            self.reasons["host_deny:" + reason] = count
        for reason, count in (self.scenario.expect_denies or {}).items():
            if by_reason[reason] != count:
                self.violate(
                    "host_denies",
                    "%s: %d denied, expected %d" % (reason, by_reason[reason], count),
                )
        for code, count in alerts.items():
            self.reasons["alert:" + str(code)] = count
        expected = self.scenario.expect_slow_denies
        if expected is not None and denies != expected:
            self.violate("slow_denies", "%d denied, expected %d" % (denies, expected))
        for code in self.scenario.expect_alerts:
            if not alerts.get(code):
                self.violate("alert_missing", code)
        expected = self.scenario.expect_effective_denies
        if expected is not None:
            from studio_platform.host_policy import HostPolicyStore, original_nodes

            tenant = self.settings.tenant_id
            store = HostPolicyStore(self.repo, tenant)
            with self.repo.engine.connect() as connection:
                nodes = original_nodes(connection, tenant)
                denied = sorted(
                    machine
                    for machine in self.client.rented
                    if store.read(connection, machine, nodes=nodes, now=self.now)[0][
                        "effective"
                    ]
                    == "deny"
                )
            self.reasons["effective_denies"] = len(denied)
            if len(denied) != expected:
                self.violate(
                    "effective_denies",
                    "%s denied in effect, expected %d"
                    % (", ".join(denied) or "none", expected),
                )

    def _billed_microusd(self):
        return sum(
            m.billed_seconds / 3600 * m.script.price * 1_000_000
            for m in self.client.machines.values()
        )

    def account(self, step):
        for machine in self.client.machines.values():
            if machine.alive(self.now):
                machine.billed_seconds += step
                if machine.busy_job is None:
                    machine.idle_seconds += step

    def finish(self):
        self.evidence.check(final=True)
        with self.repo.engine.connect() as connection:
            rows = [dict(r) for r in connection.execute(select(jobs)).mappings()]
            tries = Counter(
                row.job_id
                for row in connection.execute(
                    select(attempts.c.job_id).where(
                        attempts.c.submission_started_at.is_not(None)
                    )
                )
            )
            lost = Counter(
                row.job_id
                for row in connection.execute(
                    select(attempts.c.job_id).where(attempts.c.status == "attempt_lost")
                )
            )
        statuses = Counter(row["status"] for row in rows)
        expected_failed = self.scenario.expect_failed or {}
        failed = Counter(row["error_code"] for row in rows if row["status"] == "failed")
        for row in rows:
            if tries[row["id"]] > 1 + lost[row["id"]]:
                self.violate("job_run_twice", row["id"][:8])
            if row["status"] in ("recovery_hold", "submission_unknown") or (
                row["status"] == "failed" and row["error_code"] not in expected_failed
            ):
                self.violate("job_lost", "%s %s" % (row["id"][:8], row["status"]))
        for code, count in expected_failed.items():
            if failed[code] != count:
                self.violate(
                    "jobs_failed",
                    "%s: %d failed, expected %d" % (code, failed[code], count),
                )
            self.reasons["job_failed:" + code] = failed[code]
        if self.scenario.expect_all_done and statuses.get("succeeded", 0) != len(
            rows
        ) - sum(expected_failed.values()):
            self.violate(
                "jobs_not_done",
                ", ".join("%s=%d" % item for item in sorted(statuses.items())),
            )
        self._check_host_policy()
        machines = [m for m in self.client.machines.values() if m.script is not HISTORY]
        self._check_expected_counts(machines)
        self._check_hosted()
        return {
            "scenario": self.scenario.name,
            "simulated_hours": round((self.now - self.start) / 3600, 2),
            "jobs": len(rows),
            "jobs_done": statuses.get("succeeded", 0),
            "machines": len(machines),
            "machine_hours": round(sum(m.billed_seconds for m in machines) / 3600, 2),
            "idle_machine_hours": round(
                sum(m.idle_seconds for m in machines) / 3600, 2
            ),
            "reasons": dict(sorted(self.reasons.items())),
            "metrics": dict(sorted(self.metrics.items())),
            "violations": list(self.violations),
            "observability_gaps": list(self.evidence.gaps),
        }

    def _check_expected_counts(self, machines):
        """Exact start count and idle-stop presence; lookahead reasons reported."""
        from studio_platform.repository import outbox

        scenario = self.scenario
        with self.repo.engine.connect() as connection:
            for payload in connection.execute(
                select(outbox.c.payload).where(
                    outbox.c.event_type == "capacity.lookahead_decision"
                )
            ).scalars():
                self.reasons[
                    "lookahead:%s:%s"
                    % (payload["phase"], payload["decision"]["reason"])
                ] += 1
        if (
            scenario.expect_starts is not None
            and len(machines) != scenario.expect_starts
        ):
            self.violate(
                "start_count_mismatch",
                "%d machines started, expected %d"
                % (len(machines), scenario.expect_starts),
            )
        for machine_id, most in (scenario.expect_host_rentals or {}).items():
            if self.client.rented[machine_id] > most:
                self.violate(
                    "host_rented_again",
                    "host %s rented %d times, at most %d expected"
                    % (machine_id, self.client.rented[machine_id], most),
                )
        if scenario.expect_idle_stop_seconds is not None and not self._idle_stops:
            self.violate("idle_stop_missing", "no machine stopped for idleness")

    def _settled(self):
        """Stop early once nothing can change, or 30 min after a violation."""
        if self.violations and self.now - self._first_violation_at >= 1800:
            return True
        arrivals = sum(batch.count for batch in self.scenario.jobs)
        if len(self._arrived) < arrivals or any(
            id(event) not in self._done_events for event in self.scenario.events
        ):
            return False
        if any(
            m.alive(self.now) or m.gone_at is None and m.allocated_at() is not None
            for m in self.client.machines.values()
        ):
            return False
        with self.repo.engine.connect() as connection:
            open_jobs = connection.execute(
                select(jobs.c.id).where(
                    jobs.c.status.not_in(("succeeded", "failed", "cancelled"))
                )
            ).first()
        return open_jobs is None

    def outcomes(self):
        """Per scripted machine, in create order: stop reason and claims."""
        result = []
        with self.repo.engine.connect() as connection:
            for machine in self.client.machines.values():
                if machine.script is HISTORY:
                    continue
                node = (
                    connection.execute(
                        select(operator_nodes).where(
                            operator_nodes.c.intent_id == machine.intent_id
                        )
                    )
                    .mappings()
                    .first()
                )
                rows = list(
                    connection.execute(
                        select(attempts.c.submission_started_at).where(
                            attempts.c.worker_id == self.worker_id(machine.intent_id)
                        )
                    ).scalars()
                )
                payload = node["payload"] if node else {}
                reason = (
                    payload.get("stop_reason_code")
                    or payload.get("capacity_watchdog", {}).get("reason")
                    or payload.get("dstack", {}).get("reason_code")
                    if node and node["desired_state"] == "stopped"
                    else None
                )
                result.append(
                    {
                        "intent_id": machine.intent_id,
                        "stop_reason": reason,
                        "claims": len(rows),
                        "submitted": sum(row is not None for row in rows),
                    }
                )
        return result

    def run(self, inspect=None):
        self._done_events, self._arrived = set(), set()
        self._flagged, self._stop_seen = set(), set()
        self._paused = False
        self._first_violation_at = None
        self._blocked_since, self._flagged_block, self._flagged_spend = (
            None,
            False,
            False,
        )
        self._prespent, self._task_limit, self._demand_reason = 0, 0, None
        self._spend_window = None
        self._tick_at, self._idle_stops = None, 0
        try:
            self.build()
            self.start = self.now
            end = self.start + self.scenario.hours * 3600
            while self.now < end and not self._settled():
                self.tick()
            report = self.finish()
            report["outcomes"] = self.outcomes()
            if inspect is not None:
                # Before teardown: the throwaway ledger is still readable.
                report["inspect"] = inspect(self)
            return report
        finally:
            self.doCleanups()
            self.tearDown()


def _offline(*args, **kwargs):
    raise OSError("capacity_simulator_network_disabled")


def run_scenario(scenario, *, seed=0, inspect=None):
    """Run one scenario offline; returns the report dict with a ``status``.

    ``inspect(world)`` runs after the last tick, before the ledger is thrown
    away (for example to export a machine's recorded timeline).
    """
    logging.getLogger("studio_platform").setLevel(logging.CRITICAL)
    with (
        patch.object(socket.socket, "connect", _offline),
        patch.object(socket, "getaddrinfo", _offline),
    ):
        report = World(scenario, seed=seed).run(inspect)
    failed = bool(report["violations"])
    if scenario.expected_failure:
        report["status"] = "expected_fail" if failed else "unexpected_pass"
        report["expected_failure"] = scenario.expected_failure
    else:
        report["status"] = "fail" if failed else "pass"
    recorded = getattr(scenario, "recorded", None)
    if recorded:
        report["recorded_outcome"] = recorded.get("outcome")
    return report


def format_report(report):
    """A few readable lines per scenario: outcome, money-time, reasons seen."""
    lines = [
        "%-28s %-15s %5.1f h  jobs %d/%d  machines %d  billed %.2f mh  idle %.2f mh"
        % (
            report["scenario"],
            report["status"].upper(),
            report["simulated_hours"],
            report["jobs_done"],
            report["jobs"],
            report["machines"],
            report["machine_hours"],
            report["idle_machine_hours"],
        )
    ]
    if "wall_seconds" in report:
        lines[0] += "  (%.0f s)" % report["wall_seconds"]
    if report.get("expected_failure"):
        lines.append("    known bug: " + report["expected_failure"])
    recorded = report.get("recorded_outcome")
    if recorded:
        replay = (report.get("outcomes") or [{}])[0]
        lines.append(
            "    recorded machine: stop %s, claims %s | replay: stop %s, claims %s"
            % (
                recorded.get("stop_reason"),
                recorded.get("claims"),
                replay.get("stop_reason"),
                replay.get("claims"),
            )
        )
    lines.extend("    violation: " + item for item in report["violations"])
    if report.get("metrics"):
        lines.append(
            "    metrics: "
            + ", ".join("%s=%s" % item for item in report["metrics"].items())
        )
    reasons = ", ".join("%s=%d" % item for item in report["reasons"].items())
    lines.append("    reasons: " + (reasons or "none"))
    return "\n".join(lines)


def format_summary(reports):
    counts = Counter(report["status"] for report in reports)
    return "summary: " + ", ".join(
        "%s %d" % (status, counts[status])
        for status in ("pass", "expected_fail", "fail", "unexpected_pass")
        if counts[status]
    )
