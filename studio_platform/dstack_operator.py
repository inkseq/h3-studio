"""dstack application commands backed exclusively by the original capacity ledger.

Constructing the service does not contact providers, initialize budgets or change
capacity ceilings. HTTP callers select trusted, pinned deployments, not images,
credentials or arbitrary resources. The existing CPU controller supplies native
readiness; a dstack RUNNING task alone never authorizes generation.
"""

from __future__ import annotations

from dataclasses import asdict, replace
import copy
import json
import math
import os
from pathlib import Path
import stat
import uuid

from sqlalchemy import insert, select, update

from .dstack_capacity import (
    CapacityRequest,
    request_from_document,
    DstackCapacity,
    DstackClient,
    DstackError,
    control_plane,
    live_capacity_usage,
)
from .capacity_policy import window_open, cold_unadmitted_runtime
from .capacity_memory import configured_ram, require_preview_ram, catalog_resources

# Compatibility names for retained callers; all current call sites use the contract.
_window_open = window_open
_cold_unadmitted_runtime = cold_unadmitted_runtime
_request = request_from_document

from .operator_capacity import (
    OperatorError,
    advance_command_revision,
    command_revision,
    operator_commands,
    operator_nodes,
    operator_previews,
    require,
    safe_id,
)
from .repository import (
    Scope,
    BudgetExceeded,
    attempts,
    budget_accounts,
    budget_reservations,
    instance_intents,
    jobs,
    manually_reviewed_inactive,
    registered_workers,
    registered_devices,
    request_hash,
    scaler_actions,
    scaler_receipts,
)
from .dstack_inventory import fallbacks_valid, profile_backends
from .runtime_catalog import engine_manifest, get_profile

BACKEND_MARKER = "dstack-v1"
MAX_HOST_CANDIDATES = 10
PREFERRED_DOWNLOAD_COUNTRIES = frozenset(
    'us at be bg hr cy cz dk ee fi fr de gr hu ie it lv lt lu mt nl pl pt ro sk si es se'.split()
) | frozenset(
    (
        'United States|United States of America|Austria|Belgium|Bulgaria|Croatia|Cyprus|'
        'Czechia|Czech Republic|Denmark|Estonia|Finland|France|Germany|Greece|Hungary|'
        'Ireland|Italy|Latvia|Lithuania|Luxembourg|Malta|Netherlands|Poland|Portugal|'
        'Romania|Slovakia|Slovenia|Spain|Sweden'
    )
    .lower()
    .split('|')
)


def preferred_download_offer(offer):
    country = offer.get('country')
    return (
        isinstance(country, str)
        and country.strip().lower() in PREFERRED_DOWNLOAD_COUNTRIES
    )


PROVIDERS = {
    "vastai": "vast",
    "runpod": "runpod",
    "lium": "lium",
    "cloudrift": "cloudrift",
}
# Pinned dstack/gpuhunt scheduler labels, not native CUDA device identities.
# The native worker still checks the full physical name, capability and VRAM.
SCHEDULER_GPU_ALIASES = {
    "RTXPRO6000": "RTX PRO 6000 Blackwell",
    "RTXPRO6000S": "RTX PRO 6000 Blackwell Server Edition",
    "RTXPRO6000WS": "RTX PRO 6000 Blackwell Workstation Edition",
}
TERMINAL_JOBS = {"succeeded", "failed", "cancelled"}
OBSERVATION_KEYS = {
    "run_id",
    "run_name",
    "state",
    "ready",
    "reason_code",
    "observed_at",
    "provider_instance_id",
    "runtime_incarnation",
    "dstack_status",
    "model_load_state",
    "billing_state",
    "estimated_cost_microusd",
    "bootstrap_phase",
    "preparation",
    "host_observation",
    "host_failure",
    "no_allocation_proof",
    "provider_removal_proof",
}
IDENTITY_KEYS = {"run_id", "provider_instance_id", "runtime_incarnation"}
REGISTRATION_REASON_CODES = frozenset({"global_capacity_exceeded"})


def _actor(tenant, owner):
    return "dstack:" + request_hash({"tenant_id": tenant, "owner_id": owner})


def _integer(value, minimum=1, maximum=9_000_000_000_000):
    return type(value) is int and minimum <= value <= maximum


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def plan_offers(plan, request):
    """Project pinned 0.22.5 offers; no backend_data, SSH or user/job payload."""
    require(
        isinstance(plan, dict)
        and isinstance(plan.get("job_plans"), list)
        and len(plan["job_plans"]) == 1,
        "dstack_plan_unconfirmed",
    )
    offered = plan["job_plans"][0].get("offers")
    require(isinstance(offered, list), "dstack_plan_unconfirmed")
    normalize = lambda text: "".join(c.lower() for c in text if c.isalnum())
    result = []
    for item in offered:
        if not isinstance(item, dict) or item.get("backend") != request.backend:
            continue
        instance = item.get("instance", {})
        resources = instance.get("resources", {})
        gpus = resources.get("gpus")
        price = item.get("price")
        memory = resources.get("memory_mib")
        disk = resources.get("disk", {}).get("size_mib")
        cpus = resources.get("cpus")
        availability = item.get("availability")
        if (
            not isinstance(gpus, list)
            or len(gpus) != 1
            or not isinstance(gpus[0], dict)
            or not isinstance(gpus[0].get("name"), str)
            or normalize(gpus[0]["name"])
            not in {normalize(name) for name in request.gpu_names}
            or not all(
                _finite(v) and v > 0
                for v in (memory, disk, cpus, gpus[0].get("memory_mib"))
            )
            or memory < request.memory_gib * 1024
            or disk < request.disk_gib * 1024
            or cpus < request.cpu_count
            or gpus[0]["memory_mib"] < request.gpu_memory_gib * 1024
            or not _finite(price)
            or price <= 0
            or math.ceil(price * 1_000_000) > request.max_price_microusd
            or availability not in {"available", "idle", "unknown"}
        ):
            continue
        result.append(
            {
                "backend": request.backend,
                "region": item.get("region"),
                "offer_id": instance.get("name"),
                "gpu_type": gpus[0]["name"],
                "gpu_count": 1,
                "memory_gib": memory / 1024,
                "disk_gib": disk / 1024,
                "gpu_memory_gib": gpus[0]["memory_mib"] / 1024,
                "cpu_count": cpus,
                "hourly_cost_microusd": math.ceil(price * 1_000_000),
                "availability": availability,
                "inventory_confirmed": availability in {"available", "idle"},
            }
        )
    return sorted(
        result,
        key=lambda row: (not row["inventory_confirmed"], row["hourly_cost_microusd"]),
    )[:100]


def _protected_read(filename, *, maximum=262144):
    path = Path(filename)
    require(path.is_absolute(), "dstack_config_path_invalid", 422)
    try:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            require(
                stat.S_ISREG(info.st_mode)
                and (
                    os.name == "nt" or not info.st_mode & (stat.S_IRWXO | stat.S_IWGRP)
                ),
                "dstack_config_permissions_invalid",
                422,
            )
            raw = stream.read(maximum + 1)
        require(len(raw) <= maximum, "dstack_config_invalid", 422)
        return raw.decode("utf-8")
    except (OSError, UnicodeError):
        raise OperatorError("dstack_config_unavailable", 422) from None


class LedgerDstackStore:
    """Same-row atomic journals; no rental, task or reservation side database."""

    def __init__(self, repo, tenant_id, *, observation_fresh_seconds=30):
        self.repo, self.tenant_id = repo, tenant_id
        self.observation_fresh_seconds = observation_fresh_seconds
        self.host_client = None

    def set_host_client(self, client):
        self.host_client = client

    def _node(self, connection, intent_id):
        node = self.repo._locked(
            connection,
            select(operator_nodes).where(operator_nodes.c.intent_id == intent_id),
        )
        require(
            node is not None
            and node["payload"].get("capacity_backend") == BACKEND_MARKER
            and node["payload"].get("tenant_id") == self.tenant_id,
            "dstack_node_not_found",
            404,
        )
        intent = self.repo._locked(
            connection,
            select(instance_intents).where(instance_intents.c.id == intent_id),
        )
        require(
            intent is not None and intent["provider"] in PROVIDERS.values(),
            "dstack_intent_binding_invalid",
        )
        return dict(node), dict(intent)

    def load(self, intent_id):
        with self.repo.transaction() as connection:
            node, _ = self._node(connection, intent_id)
            return copy.deepcopy(node["payload"]["dstack"])

    def begin_apply(self, intent_id, binding):
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self._node(connection, intent_id)
            payload = copy.deepcopy(node["payload"])
            stored = payload["dstack"]
            require(
                stored.get("spec_digest") == binding["spec_digest"]
                and all(stored.get(key) == value for key, value in binding.items()),
                "dstack_intent_binding_changed",
            )
            action = self.repo._locked(
                connection,
                select(scaler_actions).where(scaler_actions.c.intent_id == intent_id),
            )
            if (
                stored.get("apply_started")
                or node["desired_state"] != "running"
                or action is None
                or action["create_started_at"] is not None
            ):
                return False
            require(
                intent["state"] == "reserved"
                and window_open(intent, node, self.repo.clock()),
                "dstack_start_window_expired",
            )
            if stored.get("host_selection") is not None:
                from .host_policy import HostPolicyStore, selection

                selected = selection(stored["host_selection"], now=self.repo.clock())
                require(
                    HostPolicyStore(self.repo, self.tenant_id).admission_snapshot(
                        connection, selected["offers"]
                    )
                    == selected["policy_snapshot"],
                    "host_policy_changed",
                )
                require(self.host_client is not None, "host_reuse_unconfirmed")
                self.host_client.reusable_hosts_confirmed(
                    {r["offer_id"]: r["machine_id"] for r in selected["offers"]}
                )
                selection(selected, now=self.repo.clock())
            reservations = list(
                connection.execute(
                    select(budget_reservations).where(
                        budget_reservations.c.reference_type == "instance",
                        budget_reservations.c.reference_id == intent_id,
                    )
                ).mappings()
            )
            require(
                reservations
                and all(
                    row["state"] == "reserved"
                    and row["amount_microusd"] == intent["reserved_cost_microusd"]
                    for row in reservations
                ),
                "dstack_budget_reservation_missing",
            )
            now = self.repo.clock()
            from .dstack_test_budget import budget_reason

            reason = budget_reason(connection, intent, node, now)
            require(reason is None, reason or "dstack_test_budget_unconfirmed")
            stored.update(apply_started=True, apply_started_at=now)
            payload["dstack"] = stored
            self.repo.update_instance(intent_id, "creating", connection=connection)
            connection.execute(
                update(scaler_actions)
                .where(scaler_actions.c.intent_id == intent_id)
                .values(create_started_at=now)
            )
            self._save(connection, node, payload, runtime_state="creating")
            self._receipt(
                connection,
                intent_id,
                "create",
                {"state": "journaled", "run_name": binding["run_name"]},
            )
            return True

    def _save(self, connection, node, payload, *, runtime_state=None):
        values = {"payload": payload, "updated_at": self.repo.clock()}
        if runtime_state is not None:
            values["runtime_state"] = runtime_state
        connection.execute(
            update(operator_nodes)
            .where(operator_nodes.c.intent_id == node["intent_id"])
            .values(**values)
        )

    def _receipt(self, connection, intent_id, operation, facts):
        connection.execute(
            insert(scaler_receipts).values(
                id=str(uuid.uuid4()),
                intent_id=intent_id,
                operation=operation,
                observed_at=self.repo.clock(),
                facts=facts,
            )
        )

    def _advance(self, connection, intent, observation):
        """Use the original state machine; uncertainty never settles invoices."""
        identity = observation.get("provider_instance_id") or intent.get(
            "provider_instance_id"
        )
        current = intent["state"]
        if identity and not intent.get("provider_instance_id"):
            connection.execute(
                update(instance_intents)
                .where(instance_intents.c.id == intent["id"])
                .values(provider_instance_id=identity, updated_at=self.repo.clock())
            )
        wanted = observation.get("state")
        path = []
        if wanted in {"starting", "runtime_unconfirmed", "ready", "busy"} and identity:
            if current in {"creating", "creation_unknown"}:
                path.append("starting")
                current = "starting"
            if wanted in {"ready", "busy"} and current == "starting":
                path.append("ready")
                current = "ready"
            if wanted == "busy" and current == "ready":
                path.append("busy")
            elif wanted == "ready" and current == "busy":
                path.append("ready")
        elif wanted == "creation_unknown" and current == "creating":
            path.append("creation_unknown")
        elif wanted in {"stopping", "stopped", "removal_unknown"}:
            if current == "creating":
                path.append("creation_unknown")
                current = "creation_unknown"
            if current in {"starting", "ready", "busy"}:
                path.append("draining")
                current = "draining"
            if current in {"creation_unknown", "draining"}:
                path.append("destroying")
        for state in path:
            self.repo.update_instance(
                intent["id"],
                state,
                provider_instance_id=identity,
                connection=connection,
            )

    def record(self, intent_id, observation):
        require(
            isinstance(observation, dict) and not set(observation) - OBSERVATION_KEYS,
            "dstack_observation_invalid",
            422,
        )
        require(
            _finite(observation.get("observed_at"))
            and 0 <= observation["observed_at"] <= self.repo.clock(),
            "dstack_observation_time_invalid",
            422,
        )
        require(
            "bootstrap_phase" not in observation
            or observation["bootstrap_phase"]
            in {
                "provisioning",
                "model_download",
                "runtime_start",
                "model_load",
                "runtime_ready",
            },
            "dstack_observation_phase_invalid",
            422,
        )
        allocation_event, cold_event = None, None
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self._node(connection, intent_id)
            from .settlement_recovery import reviewed_instance_fenced

            if reviewed_instance_fenced(connection, intent, node):
                return  # A late observation cannot settle or revive reviewed history.
            payload = copy.deepcopy(node["payload"])
            binding = payload["dstack"]
            if observation["observed_at"] < binding.get("observed_at", 0):
                return
            for key in IDENTITY_KEYS:
                require(
                    not observation.get(key)
                    or not binding.get(key)
                    or observation[key] == binding[key],
                    "dstack_observation_identity_changed",
                )
            if observation.get("run_name"):
                require(
                    observation["run_name"] == binding["run_name"],
                    "dstack_observation_identity_changed",
                )
            value = copy.deepcopy(observation)
            removal = value.pop("provider_removal_proof", None)
            if removal is not None:
                require(
                    isinstance(removal, dict)
                    and set(removal)
                    == {
                        "version",
                        "backend",
                        "run_id",
                        "provider_instance_id",
                        "observed_at",
                        "inventory_observed_at",
                        "complete",
                        "absent",
                        "inventory_sha256",
                    }
                    and type(removal["version"]) is int
                    and removal["version"] == 1
                    and removal["complete"] is True
                    and removal["absent"] is True
                    and removal["backend"] == binding["backend"]
                    and removal["run_id"] == binding.get("run_id")
                    and removal["provider_instance_id"]
                    == intent.get("provider_instance_id")
                    and bool(intent.get("provider_instance_id"))
                    and value.get("state") == "stopped"
                    and value.get("dstack_status")
                    in {"done", "failed", "terminated", "aborted"}
                    and _finite(removal["observed_at"])
                    and _finite(removal["inventory_observed_at"])
                    and 0 <= self.repo.clock() - removal["observed_at"] <= 30
                    and 0
                    <= removal["observed_at"] - removal["inventory_observed_at"]
                    <= 30
                    and 0 <= self.repo.clock() - removal["inventory_observed_at"] <= 30
                    and isinstance(removal["inventory_sha256"], str)
                    and len(removal["inventory_sha256"]) == 64
                    and all(
                        c in "0123456789abcdef" for c in removal["inventory_sha256"]
                    ),
                    "dstack_provider_removal_proof_invalid",
                )
            no_allocation = value.pop("no_allocation_proof", None)
            if no_allocation is not None:
                from .dstack_allocation_proof import validate_proof

                no_allocation = validate_proof(no_allocation, binding)
                require(
                    no_allocation is not None
                    and value.get("state") == "stopped"
                    and value.get("dstack_status") == "failed"
                    and not intent.get("provider_instance_id")
                    and value.get("provider_instance_id") is None,
                    "dstack_no_allocation_proof_invalid",
                )
            if binding.get("billing_state") == "no_charge_confirmed" and (
                binding.get("no_allocation_proof")
                or binding.get("no_allocation_review")
            ):
                require(
                    value.get("state") == "stopped"
                    and value.get("dstack_status") == "failed"
                    and value.get("provider_instance_id") is None
                    and value.get("runtime_incarnation") is None
                    and "host_observation" not in value
                    and "host_failure" not in value,
                    "dstack_no_allocation_history_conflict",
                )
                value["billing_state"] = "no_charge_confirmed"
                if value.get("state") == "stopped":
                    value["reason_code"] = "dstack_never_allocated"
            if "host_observation" in value:
                from .host_policy import observation as physical_observation

                physical = physical_observation(
                    value.pop("host_observation"), now=self.repo.clock()
                )
                require(
                    binding["backend"] == "vastai"
                    and physical["provider_instance_id"]
                    == value.get(
                        "provider_instance_id", binding.get("provider_instance_id")
                    ),
                    "host_observation_binding_invalid",
                )
                prior = payload.get("host_observation")
                require(
                    prior is None
                    or all(
                        prior[k] == physical[k]
                        for k in ("provider_instance_id", "machine_id", "offer_id")
                    ),
                    "host_observation_identity_changed",
                )
                payload.setdefault("host_observation", physical)
                binding.setdefault("host_observation", physical)
                if prior is None:
                    allocation_event = {
                        **physical,
                        "node_id": intent_id,
                        "dstack_run": binding["run_name"],
                        "provider": "vast",
                        "profile_id": binding["profile_id"],
                        "mode": binding["mode"],
                        "simulation": False,
                    }
            if "host_failure" in value:
                from .host_policy import failure

                failed = failure(value.pop("host_failure"), now=self.repo.clock())
                # A user stop, model failure or already-ready runtime is never
                # retrospectively converted into a provider startup failure.
                if (
                    payload.get("host_observation") is not None
                    and node["desired_state"] == "running"
                    and not payload.get("stop_requested_at")
                    and not binding.get("stop_started")
                    and not binding.get("first_runtime_ready_at")
                ):
                    payload.setdefault("host_failure", failed)
            # A stale error, stop intent or expired window cannot leave a ready projection.
            value["ready"] = bool(
                value.get("ready") is True
                and value.get("state") == "ready"
                and node["desired_state"] == "running"
                and window_open(intent, node, self.repo.clock())
            )
            if value.get("runtime_incarnation") and value.get("state") in {
                "ready",
                "busy",
            }:
                started = binding.get("apply_started_at")
                physical = payload.get("host_observation")
                if (
                    not binding.get("first_runtime_ready_at")
                    and physical is not None
                    and _finite(started)
                    and 0 < started < value["observed_at"]
                ):
                    cold_event = (
                        {
                            **physical,
                            "node_id": intent_id,
                            "dstack_run": binding["run_name"],
                            "provider": "vast",
                            "profile_id": binding["profile_id"],
                            "mode": binding["mode"],
                            "warmth": "cold",
                            "simulation": False,
                        },
                        started,
                        value["observed_at"] - started,
                    )
                binding.setdefault("first_runtime_ready_at", value["observed_at"])
            if value.get("state") == "stopped":
                # Preserve the first terminal observation in the original row;
                # later polls must not extend ended runtime/cost estimates.
                binding.setdefault("first_stopped_at", value["observed_at"])
            if "preparation" in value:
                from .dstack_preparation import record_preparation

                record_preparation(
                    binding,
                    value.pop("preparation"),
                    value["observed_at"],
                    validation_at=self.repo.clock(),
                )
            binding.update(value)
            payload["dstack"] = binding
            payload["dstack_observation"] = {
                "run_id": binding.get("run_id"),
                "instance_id": binding.get("provider_instance_id"),
                "observed_at": value["observed_at"],
                "status": value.get("dstack_status", "unknown"),
                "ready": value["ready"],
                "runtime_incarnation": binding.get("runtime_incarnation"),
                "reason_code": value.get("reason_code"),
                "manifest_digest": binding["manifest_digest"],
            }
            self._advance(connection, intent, value)
            if removal is not None:
                # Record physical closure independently from invoices. Unknown
                # actual cost stays unknown and retains its billing obligation.
                self.repo.update_instance(
                    intent_id,
                    "destroyed",
                    destruction_confirmed=True,
                    connection=connection,
                )
                from .settlement_recovery import release_removed_instance

                release_removed_instance(self.repo, connection, intent_id)
                if not binding.get("provider_removal_proof"):
                    binding["provider_removal_proof"] = removal
                    self._receipt(
                        connection,
                        intent_id,
                        "provider_removal",
                        {"state": "destroyed", "proof": removal},
                    )
                    self.repo._emit(
                        connection,
                        "dstack.provider_removal_confirmed",
                        intent_id,
                        {
                            "intent_id": intent_id,
                            "provider_instance_id": removal["provider_instance_id"],
                            "inventory_sha256": removal["inventory_sha256"],
                            "billing_status": "pending",
                        },
                    )
                connection.execute(
                    update(operator_nodes)
                    .where(operator_nodes.c.intent_id == intent_id)
                    .values(desired_state="stopped")
                )
            if no_allocation is not None:
                # The complete typed native rejection list proves every PUT
                # returned no contract. Settle this original reservation once;
                # terminal/JPD-null history without that evidence stays pending.
                self.repo.update_instance(
                    intent_id,
                    "destroyed",
                    destruction_confirmed=True,
                    actual_cost_microusd=0,
                    connection=connection,
                )
                previous_proof = binding.get("no_allocation_proof")
                binding["no_allocation_proof"] = no_allocation
                binding.update(
                    billing_state="no_charge_confirmed",
                    reason_code="dstack_never_allocated",
                )
                connection.execute(
                    update(operator_nodes)
                    .where(operator_nodes.c.intent_id == intent_id)
                    .values(desired_state="stopped")
                )
                if previous_proof is None:
                    self._receipt(
                        connection,
                        intent_id,
                        "no_allocation",
                        {
                            "state": "not_created",
                            "actual_cost_microusd": 0,
                            "proof": no_allocation,
                        },
                    )
                    self.repo._emit(
                        connection,
                        "dstack.no_allocation_confirmed",
                        intent_id,
                        {
                            "intent_id": intent_id,
                            "run_id": no_allocation["run_id"],
                            "evidence_sha256": no_allocation["evidence_sha256"],
                        },
                    )
            if intent["state"] == "destroyed" and binding.get("provider_removal_proof"):
                from .settlement_recovery import release_removed_instance

                release_removed_instance(self.repo, connection, intent_id)
            if (removal is not None or no_allocation is not None) and node[
                "desired_state"
            ] != "stopped":
                payload = advance_command_revision(payload)
            self._save(
                connection,
                node,
                payload,
                runtime_state=value.get("state", "observation_unknown"),
            )
            connection.execute(
                update(scaler_actions)
                .where(scaler_actions.c.intent_id == intent_id)
                .values(last_observation=value, last_observed_at=value["observed_at"])
            )
        # Original SQL evidence commits first. Export failure cannot affect
        # authority, roll back the rental journal or advance its timestamps.
        telemetry = getattr(self, "telemetry", None)
        try:
            if allocation_event is not None and telemetry is not None:
                telemetry.host_event(allocation_event)
            if cold_event is not None and telemetry is not None:
                context, started, duration = cold_event
                telemetry.start(
                    "capacity_provision", context, started_at=started
                ).finish(
                    "success",
                    duration_seconds=duration,
                    timing_basis="durable_cpu_interval",
                )
        except Exception:
            pass

    def record_registration(self, intent_id, worker_id, reason_code=None):
        """Safe control-plane outcome, separate from native/runtime readiness.

        A registration receipt cannot settle bills, confirm provider removal,
        mark a model ready or make a worker transport eligible.
        """
        require(
            safe_id(worker_id)
            and (reason_code is None or reason_code in REGISTRATION_REASON_CODES),
            "dstack_registration_observation_invalid",
        )
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self._node(connection, intent_id)
            payload = copy.deepcopy(node["payload"])
            if reason_code is None:
                worker = (
                    connection.execute(
                        select(registered_workers).where(
                            registered_workers.c.id == worker_id
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                require(
                    worker is not None
                    and worker["provider"] == intent["provider"]
                    and worker["instance_id"] == intent["provider_instance_id"]
                    and worker["spec"].get("dispatch_backend") == "hatchet-v1"
                    and worker["spec"].get("engine_manifest_digest")
                    == payload["dstack"]["manifest_digest"]
                    and worker["spec"].get("configuration_id")
                    == payload["dstack"]["configuration_id"],
                    "dstack_registration_binding_invalid",
                )
            previous = payload["dstack"].get("registration_observation", {})
            observation = {
                "worker_id": worker_id,
                "reason_code": reason_code,
                "registered": reason_code is None,
                "observed_at": self.repo.clock(),
            }
            payload["dstack"]["registration_observation"] = observation
            self._save(connection, node, payload)
            if any(
                previous.get(key) != observation[key]
                for key in ("worker_id", "reason_code", "registered")
            ):
                self._receipt(connection, intent_id, "registration", observation)

    def begin_stop(self, intent_id, run_id, *, retry=False):
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self._node(connection, intent_id)
            payload = copy.deepcopy(node["payload"])
            binding = payload["dstack"]
            require(binding.get("run_id") == run_id, "dstack_stop_identity_unconfirmed")
            if node["desired_state"] != "stopped":
                return False
            now = self.repo.clock()
            if retry:
                observed = binding.get("observed_at")
                if (
                    not binding.get("stop_started")
                    or binding.get("stop_attempt_count", 0) >= 5
                    or now < binding.get("stop_next_retry_at", float("inf"))
                ):
                    return False
                if (
                    binding.get("dstack_status") != "running"
                    or not _finite(observed)
                    or not 0 <= now - observed <= self.observation_fresh_seconds
                    or binding.get("provider_instance_id")
                    != intent.get("provider_instance_id")
                ):
                    return False
            elif binding.get("stop_started"):
                return False
            workers = (
                list(
                    connection.execute(
                        select(registered_workers).where(
                            registered_workers.c.provider == intent["provider"],
                            registered_workers.c.instance_id
                            == intent["provider_instance_id"],
                        )
                    ).mappings()
                )
                if intent.get("provider_instance_id")
                else []
            )
            worker_ids = [worker["id"] for worker in workers]
            from .capacity_watchdog import fenced_node_stop

            watchdog_stop = fenced_node_stop(connection, node, intent)
            if (
                workers
                and any(
                    worker["state"] != "retired" or worker["current_job_id"]
                    for worker in workers
                )
                and not watchdog_stop
            ):
                return False
            if worker_ids and not watchdog_stop:
                from .control import WorkerControl

                relevant = jobs.outerjoin(attempts, jobs.c.id == attempts.c.job_id)
                for row in connection.execute(
                    select(
                        jobs.c.id,
                        jobs.c.status,
                        jobs.c.current_attempt_id,
                        jobs.c.lease_worker_id,
                        jobs.c.lease_expires_at,
                        attempts.c.submission_started_at,
                        attempts.c.upstream_task_id,
                        attempts.c.upstream_stopped,
                    )
                    .select_from(relevant)
                    .where(
                        (jobs.c.lease_worker_id.in_(worker_ids))
                        | (attempts.c.worker_id.in_(worker_ids))
                    )
                ).mappings():
                    if (
                        (
                            row["status"] not in TERMINAL_JOBS
                            and not WorkerControl._proven_unsubmitted_queue(
                                connection, row, expired_lease_only=True
                            )
                        )
                        or (
                            row["submission_started_at"] is not None
                            or row["upstream_task_id"] is not None
                        )
                        and row["upstream_stopped"] != 1
                    ):
                        return False
            # An owned cold runtime which has never admitted a worker cannot
            # have business inference to drain. Model download may already be
            # running; waiting for ready/idle would make that preparation
            # impossible to cancel. A previously qualified runtime still needs
            # exact fresh native idle proof, even if its worker is retired.
            launch_started = binding.get(
                "bootstrap_launch_started", binding.get("bootstrap_started")
            )
            cold_unadmitted = cold_unadmitted_runtime(
                binding,
                workers,
                intent.get("provider_instance_id"),
                now,
                self.observation_fresh_seconds,
            )
            if (
                not retry
                and not watchdog_stop
                and not cold_unadmitted
                and (
                    launch_started
                    or workers
                    or binding.get("runtime_incarnation") is not None
                    or binding.get("first_runtime_ready_at") is not None
                )
            ):
                observed = binding.get("observed_at")
                if not (
                    binding.get("state") == "ready"
                    and binding.get("runtime_incarnation")
                    and _finite(observed)
                    and 0
                    <= self.repo.clock() - observed
                    <= self.observation_fresh_seconds
                ):
                    return False
            count = binding.get("stop_attempt_count", 0) + 1
            binding.update(
                stop_started=True,
                stop_attempt_count=count,
                stop_last_attempt_at=now,
                stop_next_retry_at=now + min(30 * 2 ** (count - 1), 300),
            )
            binding.setdefault("stop_started_at", now)
            payload["dstack"] = binding
            connection.execute(
                update(scaler_actions)
                .where(scaler_actions.c.intent_id == intent_id)
                .values(destroy_started_at=now)
            )
            self._save(connection, node, payload, runtime_state="stopping")
            self._receipt(
                connection,
                intent_id,
                "destroy",
                {
                    "state": "retry_journaled" if retry else "journaled",
                    "run_id": run_id,
                    "attempt_count": count,
                },
            )
            return True

    def authorize_bootstrap_launch(self, intent_id, run_id, instance_id):
        """Recheck authority for immutable uploads which have not launched yet."""
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self._node(connection, intent_id)
            binding = node["payload"]["dstack"]
            require(
                binding.get("bootstrap_started")
                and binding.get("run_id") == run_id
                and binding.get("provider_instance_id") == instance_id,
                "dstack_bootstrap_identity_unconfirmed",
            )
            require(
                node["desired_state"] == "running"
                and window_open(intent, node, self.repo.clock()),
                "dstack_bootstrap_window_expired",
            )
            return True

    def begin_runtime_launch(self, intent_id, run_id, instance_id):
        """Durable launch journal; a response gap cannot run setup twice."""
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self._node(connection, intent_id)
            payload = copy.deepcopy(node["payload"])
            binding = payload["dstack"]
            require(
                binding.get("bootstrap_started")
                and binding.get("run_id") == run_id
                and binding.get("provider_instance_id") == instance_id,
                "dstack_bootstrap_identity_unconfirmed",
            )
            require(
                node["desired_state"] == "running"
                and window_open(intent, node, self.repo.clock()),
                "dstack_bootstrap_window_expired",
            )
            if binding.get("bootstrap_launch_started", True):
                return False
            binding.update(
                bootstrap_launch_started=True,
                bootstrap_launch_started_at=self.repo.clock(),
            )
            self._save(connection, node, payload)
            self._receipt(
                connection,
                intent_id,
                "bootstrap",
                {"state": "launch_journaled", "run_id": run_id},
            )
            return True

    def begin_bootstrap(self, intent_id, run_id, instance_id):
        """CPU must commit this before the existing one-shot remote bootstrap."""
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self._node(connection, intent_id)
            payload = copy.deepcopy(node["payload"])
            binding = payload["dstack"]
            require(
                binding.get("apply_started")
                and binding.get("run_id") == run_id
                and binding.get("provider_instance_id") == instance_id
                and instance_id,
                "dstack_bootstrap_identity_unconfirmed",
            )
            if binding.get("bootstrap_started"):
                return False
            require(
                node["desired_state"] == "running"
                and window_open(intent, node, self.repo.clock()),
                "dstack_bootstrap_window_expired",
            )
            binding.update(
                bootstrap_started=True,
                bootstrap_started_at=self.repo.clock(),
                bootstrap_launch_started=False,
            )
            payload["dstack"] = binding
            self._save(connection, node, payload)
            self._receipt(
                connection,
                intent_id,
                "bootstrap",
                {"state": "journaled", "run_id": run_id, "instance_id": instance_id},
            )
            return True

    def cancel_unapplied(self, intent_id):
        """Release only an original intent atomically proven never sent to dstack."""
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self._node(connection, intent_id)
            action = self.repo._locked(
                connection,
                select(scaler_actions).where(scaler_actions.c.intent_id == intent_id),
            )
            binding = node["payload"]["dstack"]
            if (
                node["desired_state"] != "stopped"
                or intent["state"] != "reserved"
                or binding.get("apply_started")
                or action is None
                or action["create_started_at"] is not None
            ):
                return False
            self.repo.update_instance(
                intent_id,
                "destroyed",
                destruction_confirmed=True,
                actual_cost_microusd=0,
                connection=connection,
            )
            payload = copy.deepcopy(node["payload"])
            payload["dstack"].update(
                state="stopped",
                ready=False,
                billing_state="no_charge_confirmed",
                reason_code="dstack_never_applied",
                observed_at=self.repo.clock(),
            )
            self._save(connection, node, payload, runtime_state="stopped")
            self._receipt(
                connection,
                intent_id,
                "cancel",
                {"state": "never_applied", "actual_cost_microusd": 0},
            )
            return True

    def review_unallocated_start(self, intent_id, review):
        """Commit an explicit trusted owner review through the original ledger."""
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self._node(connection, intent_id)
            payload = copy.deepcopy(node["payload"])
            binding = payload["dstack"]
            require(
                isinstance(review, dict)
                and review.get("version") == 1
                and review.get("owner_confirmation") == "no_machine_allocated"
                and review.get("run_id") == binding.get("run_id")
                and binding.get("dstack_status") == "failed"
                and binding.get("state") == "stopped"
                and review.get("provider") == binding.get("backend") == "vastai"
                and review.get("complete_account_empty") is True
                and _finite(review.get("observed_at"))
                and 0 <= self.repo.clock() - review["observed_at"] <= 30
                and _finite(review.get("inventory_observed_at"))
                and 0 <= self.repo.clock() - review["inventory_observed_at"] <= 30
                and not intent.get("provider_instance_id")
                and all(
                    binding.get(key) is None
                    for key in (
                        "provider_instance_id",
                        "runtime_incarnation",
                        "first_runtime_ready_at",
                    )
                )
                and all(
                    binding.get(key) in (None, False)
                    for key in (
                        "bootstrap_started",
                        "bootstrap_launch_started",
                        "runtime_launch_started",
                    )
                ),
                "dstack_no_allocation_review_unconfirmed",
            )
            if binding.get("no_allocation_review"):
                return {
                    "node_id": intent_id,
                    "state": "not_created",
                    "actual_cost_microusd": 0,
                    "already_reviewed": True,
                }
            self.repo.update_instance(
                intent_id,
                "destroyed",
                destruction_confirmed=True,
                actual_cost_microusd=0,
                connection=connection,
            )
            binding.update(
                no_allocation_review=copy.deepcopy(review),
                billing_state="no_charge_confirmed",
                reason_code="dstack_never_allocated",
                ready=False,
            )
            payload = advance_command_revision(payload)
            self._save(connection, node, payload, runtime_state="stopped")
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == intent_id)
                .values(desired_state="stopped")
            )
            self._receipt(
                connection,
                intent_id,
                "no_allocation_review",
                {"state": "not_created", "actual_cost_microusd": 0, "review": review},
            )
            self.repo._emit(
                connection,
                "dstack.no_allocation_confirmed",
                intent_id,
                {
                    "intent_id": intent_id,
                    "run_id": review["run_id"],
                    "source": review["source"],
                },
            )
            return {
                "node_id": intent_id,
                "state": "not_created",
                "actual_cost_microusd": 0,
                "already_reviewed": False,
            }


class DstackOperator:
    """Small owner-scoped application service; mount alongside existing operator API."""

    def __init__(
        self,
        repo,
        settings,
        capacity=None,
        profile_config=None,
        *,
        provider_inventory=None,
    ):
        self.repo, self.settings, self.capacity = repo, settings, capacity
        self.provider_inventory = provider_inventory or getattr(
            getattr(capacity, "client", None), "provider_inventory", None
        )
        self.config = copy.deepcopy(
            profile_config
            or {"version": 1, "policy": {"enabled": False}, "profiles": []}
        )
        require(self.config.get("version") == 1, "dstack_config_version_invalid", 422)
        self.policy = self.config.get("policy", {})
        self.profiles = {}
        if self.policy.get("enabled"):
            require(
                capacity is not None and type(self.policy["enabled"]) is bool,
                "dstack_adapter_unavailable",
            )
            for name, maximum in (
                ("max_instances", 128),
                ("max_physical_gpus", 128),
                ("max_hourly_cost_microusd", 9_000_000_000_000),
                ("max_ttl_seconds", 86400),
                ("idle_shutdown_seconds", 86400),
                ("observation_fresh_seconds", 120),
            ):
                require(
                    _integer(self.policy.get(name), maximum=maximum),
                    "dstack_policy_limits_invalid",
                    422,
                )
            require(
                "expires_at" in self.policy
                and (
                    _finite(self.policy["expires_at"])
                    or self.policy["expires_at"] is None
                    and self.policy.get("lifecycle_version") == 2
                ),
                "dstack_policy_window_invalid",
                422,
            )
            require(
                type(self.policy.get("lifecycle_version", 1)) is int
                and self.policy.get("lifecycle_version", 1) in {1, 2},
                "dstack_lifecycle_version_invalid",
                422,
            )
            if self.policy.get("lifecycle_version") == 2:
                require(
                    _integer(self.policy.get("reservation_seconds"), 120, 86400),
                    "dstack_reservation_window_invalid",
                    422,
                )
        for value in self.config.get("profiles", []):
            profile = self._validate_profile(value)
            require(profile["id"] not in self.profiles, "dstack_profile_duplicate", 422)
            self.profiles[profile["id"]] = profile
        if "test_gpu_guard" in self.policy:
            from .dstack_test_budget import ACCOUNT, POOLS, PROJECTS, validate_guard

            try:
                validate_guard(self.policy["test_gpu_guard"])
            except ValueError:
                raise OperatorError("dstack_test_guard_invalid", 422) from None
            require(
                getattr(settings, "public_origin", None) == "https://staging.inkseq.com"
                # Local server project, or the dstack Sky staging project.
                and self.config.get("project") in {"sixnine-staging", "inkseq-staging"}
                and self.policy.get("lifecycle_version") == 2
                and _integer(self.policy.get("max_instances"), 1, 10)
                and _integer(self.policy.get("max_physical_gpus"), 1, 10)
                and self.policy["max_instances"] == self.policy["max_physical_gpus"]
                and 1 <= self.policy["idle_shutdown_seconds"] <= 86400
                and all(
                    profile["pool"] in POOLS
                    and profile["project_id"] in PROJECTS
                    and ACCOUNT in profile["budget_account_ids"]
                    and profile["extra_reservation_microusd"] > 0
                    for profile in self.profiles.values()
                ),
                "dstack_test_guard_binding_invalid",
                422,
            )
        self.fingerprint = request_hash(self.config)

    def _validate_profile(self, value):
        require(isinstance(value, dict), "dstack_profile_invalid", 422)
        profile = copy.deepcopy(value)
        required = {
            "id",
            "owner_id",
            "project_id",
            "pool",
            "backend",
            "runtime_profile_id",
            "mode",
            "configuration_id",
            "image",
            "gpu_names",
            "memory_gib",
            "disk_gib",
            "gpu_memory_gib",
            "cpu_count",
            "max_price_microusd",
            "max_ttl_seconds",
            "extra_reservation_microusd",
            "budget_account_ids",
        }
        require(
            required <= set(profile)
            and not set(profile)
            - required
            - {"regions", "min_reliability", "fallback_backends"},
            "dstack_profile_fields_invalid",
            422,
        )
        require(
            all(
                safe_id(profile[key])
                for key in ("id", "owner_id", "project_id", "pool", "configuration_id")
            )
            and self._capacity_owner_known(profile["owner_id"])
            and isinstance(profile["budget_account_ids"], list)
            and profile["budget_account_ids"]
            and len(profile["budget_account_ids"])
            == len(set(profile["budget_account_ids"]))
            and all(safe_id(item) for item in profile["budget_account_ids"]),
            "dstack_profile_scope_invalid",
            422,
        )
        try:
            native = get_profile(profile["runtime_profile_id"])
            manifest = engine_manifest(profile["runtime_profile_id"], profile["mode"])
        except (KeyError, ValueError):
            raise OperatorError("dstack_native_profile_invalid", 422) from None
        floors = {
            "memory_gib": max(
                native.get("minimum_ram_bytes", 0),
                native["runtime"].get("minimum_available_ram_bytes", 0),
                native["runtime"].get("minimum_total_ram_bytes", 0),
            ),
            "disk_gib": native.get("minimum_disk_bytes", 0),
            "gpu_memory_gib": max(
                native.get("minimum_free_vram_bytes", 0),
                native["runtime"].get("minimum_free_vram_bytes", 0),
                native.get("hardware_admission", {}).get("minimum_total_vram_bytes", 0),
            ),
        }
        require(
            all(
                _integer(profile.get(key)) and profile[key] * 2**30 >= floor
                for key, floor in floors.items()
            ),
            "dstack_native_resource_floor_invalid",
            422,
        )
        normalize = lambda name: "".join(
            char.lower() for char in name if char.isalnum()
        )
        allowed = {
            normalize(name)
            for name in native.get("hardware_admission", {}).get(
                "gpu_models", native["gpu_models"]
            )
        }
        require(
            isinstance(profile["gpu_names"], list)
            and profile["gpu_names"]
            and all(
                isinstance(name, str)
                and normalize(SCHEDULER_GPU_ALIASES.get(name, name)) in allowed
                for name in profile["gpu_names"]
            ),
            "dstack_native_gpu_binding_invalid",
            422,
        )
        require(
            fallbacks_valid(profile, PROVIDERS), "dstack_profile_fields_invalid", 422
        )
        require(
            _integer(profile["extra_reservation_microusd"], 0)
            and _integer(profile["max_ttl_seconds"], 120, 86400),
            "dstack_profile_limits_invalid",
            422,
        )
        profile.update(
            model_id=native["model_id"],
            manifest_digest=manifest.digest,
            recipe_id=manifest.document["generation_recipe_id"],
        )
        # Exercise exact transport validation without constructing any provider request.
        self._capacity_request(profile, str(uuid.uuid4()), 1.0, 121.0, 120)
        return profile

    def authorize(self, principal):
        from .identity import require_admin

        return require_admin(
            principal,
            self.repo,
            self.settings,
            legacy_capacity=True,
            legacy_code="operator_forbidden",
        )

    def _capacity_owner_known(self, owner):
        from .identity import capacity_owner_known

        return capacity_owner_known(self.repo, self.settings, owner)

    def authorize_admin(self, principal):
        from .identity import require_admin

        self.authorize(principal)
        return require_admin(
            principal,
            self.repo,
            self.settings,
            legacy_code="operator_admin_forbidden",
        )

    def _enabled(self):
        require(
            self.policy.get("enabled") is True and self.capacity is not None,
            "dstack_capacity_disabled",
        )
        require(
            self.policy["expires_at"] is None
            or self.repo.clock() < self.policy["expires_at"],
            "dstack_authority_expired",
        )

    def demand_start_once(self):
        from .dstack_demand_start import start_once

        return start_once(self)

    def demand_start_summary(self, connection, now):
        from .dstack_demand import summary

        return summary(self, connection, now)

    def record_download_timebox(self, connection, current, intent, now):
        """Journal an owned cold stop on the original job and host history.

        The controller rechecks its cold/no-execution fence under this same
        capacity lock. This callback never starts/stops a provider or settles
        money; the original stop path owns those operations.
        """
        from .dstack_demand import profile_hash
        from .dstack_demand_start import (
            DOWNLOAD_TIMEBOX,
            MAX_DOWNLOAD_REPLACEMENTS,
            _start_command,
            _start_key,
            _write,
            download_journal,
        )
        from .host_policy import bound_observation, failure

        payload = copy.deepcopy(current['payload'])
        demand = payload.get('demand_start')
        if not isinstance(demand, dict) or not isinstance(demand.get('job_id'), str):
            return None
        profile = self.profiles.get(demand.get('profile_id'))
        physical = bound_observation(current, now=now)
        if (
            profile is None
            or physical is None
            or physical['provider_instance_id'] != intent['provider_instance_id']
        ):
            return None
        job = (
            connection.execute(
                select(jobs).where(
                    jobs.c.id == demand['job_id'],
                    jobs.c.tenant_id == self.settings.tenant_id,
                )
            )
            .mappings()
            .first()
        )
        if (
            job is None
            or job['status'] != 'queued'
            or any(
                (
                    demand.get('plan_id') != job['plan_id'],
                    demand.get('request_hash') != job['request_hash'],
                    demand.get('execution_hash') != request_hash(job['execution_plan']),
                    demand.get('profile_id') != profile['id'],
                    demand.get('profile_hash') != profile_hash(self.config, profile),
                )
            )
        ):
            return None
        row = download_journal(connection, self.settings.tenant_id, job['id'])
        if row is None:
            return None
        identity = request_hash(
            {
                'tenant': self.settings.tenant_id,
                'job_id': job['id'],
                'profile_hash': demand['profile_hash'],
            }
        )
        journal = copy.deepcopy(row['payload'])
        if row['request_hash'] != identity or type(journal.get('cycle')) is not int:
            return None
        original = _start_command(
            connection, self, profile, _start_key(identity, journal['cycle'])
        )
        if original is None or original['payload']['node_id'] != current['intent_id']:
            return None
        stops = journal.get('download_stops', [])
        existing = next(
            (s for s in stops if s['node_id'] == current['intent_id']), None
        )
        if existing is None:
            if len(stops) >= MAX_DOWNLOAD_REPLACEMENTS + 1:
                return None
            stops.append(
                {
                    'node_id': current['intent_id'],
                    'machine_id': physical['machine_id'],
                    'at': now,
                }
            )
            journal['download_stops'] = stops
            _write(connection, row, journal, now)
        payload['host_failure'] = failure(
            {
                'version': 1,
                'stage': 'model_download',
                'reason_code': DOWNLOAD_TIMEBOX,
                'at': existing['at'] if existing else now,
                'qualified_start_failure': False,
            },
            now=now,
        )
        payload['download_timebox_event'] = {
            'version': 1,
            'job_id': job['id'],
            'machine_id': physical['machine_id'],
            'at': existing['at'] if existing else now,
            'reason_code': DOWNLOAD_TIMEBOX,
        }
        self.repo._emit(
            connection,
            'dstack.download_timebox_stop_requested',
            current['intent_id'],
            {
                'job_id': job['id'],
                'machine_id': physical['machine_id'],
                'reason_code': DOWNLOAD_TIMEBOX,
            },
        )
        return payload

    def _capacity_request(
        self,
        profile,
        intent_id,
        created_at,
        deadline,
        duration,
        *,
        lifecycle_version=1,
        test_max_duration=None,
        test_task_id=None,
    ):
        return CapacityRequest(
            intent_id=intent_id,
            backend=profile["backend"],
            profile_id=profile["runtime_profile_id"],
            mode=profile["mode"],
            model_id=profile["model_id"],
            configuration_id=profile["configuration_id"],
            manifest_digest=profile["manifest_digest"],
            image=profile["image"],
            gpu_names=tuple(profile["gpu_names"]),
            memory_gib=configured_ram(profile),
            disk_gib=profile["disk_gib"],
            gpu_memory_gib=profile["gpu_memory_gib"],
            cpu_count=profile["cpu_count"],
            max_price_microusd=profile["max_price_microusd"],
            created_at=created_at,
            hard_deadline=deadline,
            max_duration_s=duration,
            regions=tuple(profile.get("regions", [])),
            min_reliability=profile.get("min_reliability", 0.9),
            lifecycle_version=lifecycle_version,
            test_task_id=test_task_id,
            test_max_duration_s=(
                test_max_duration
                or self.policy.get("test_gpu_guard", {}).get("max_runtime_seconds")
                if lifecycle_version == 2
                else None
            ),
        )

    def _execution_check(self, profile, now=None):
        """One read-only policy check shared by catalog and original admission."""
        from .execution_profiles import selected_policy
        from .execution_policy import authority_current, reservation_for_duration

        try:
            policy = selected_policy(
                self.settings,
                profile_id=profile["runtime_profile_id"],
                recipe_id=profile["recipe_id"],
            )
            now = self.repo.clock() if now is None else now
            if policy is None:
                return None, "execution_policy_missing"
            if policy["enabled"] is not True:
                return None, "execution_policy_disabled"
            if not (
                policy["backend"] == "wangp-worker"
                and policy.get("dispatch_backend") == "hatchet-v1"
                and policy["pool"] == profile["pool"]
                and policy["configuration_id"] == profile["configuration_id"]
                and policy["model_id"] == profile["model_id"]
                and policy["engine_manifest_digest"] == profile["manifest_digest"]
            ):
                return None, "execution_policy_binding_mismatch"
            if policy["qualification"]["status"] not in {
                "accepted",
                "runtime_required",
            }:
                return None, "execution_policy_unqualified"
            if not authority_current(policy, now):
                return None, (
                    "execution_policy_not_yet_valid"
                    if policy["qualification"]["verified_at"] > now
                    else "execution_policy_expired"
                )
            duration = reservation_for_duration(
                policy, policy["envelope"]["max_duration_seconds"]
            )["expected_runtime_s"]
            if not (_finite(duration) and 0 < duration <= 86400):
                return None, "execution_runtime_unconfirmed"
            return duration, None
        except (KeyError, TypeError, ValueError):
            return None, "execution_policy_invalid"

    def _expected_runtime(self, profile):
        """Use the original protected execution quote, never an HTTP estimate."""
        duration, reason = self._execution_check(profile)
        require(
            reason is None,
            "dstack_execution_runtime_unconfirmed"
            if reason == "execution_runtime_unconfirmed"
            else "dstack_execution_policy_unconfirmed",
        )
        return duration

    def _admin_limits(self, connection=None):
        from .spending_limits import admin_defaults
        from .capacity_settings import read_policy

        if connection is None:
            with self.repo.engine.connect() as connection:
                return self._admin_limits(connection)
        return read_policy(
            connection,
            self.settings.tenant_id,
            admin_defaults=admin_defaults(self),
        )

    def _lifecycle(
        self, body, now, expected_runtime, *, limits=None, original_guard=None
    ):
        limits = limits or self._admin_limits()["value"]
        keep = body.get("keep_on", "until" if body.get("hold_seconds", 0) else "idle")
        hold = body.get("hold_seconds", 0)
        idle = body.get("idle_shutdown_seconds", limits["idle_shutdown_seconds"])
        require(
            isinstance(keep, str)
            and keep in {"idle", "until", "until_stopped"}
            and _integer(hold, 0, 86400)
            and _integer(idle, 1, 86400)
            and (keep == "until" and hold > 0 or keep != "until" and hold == 0),
            "dstack_lifecycle_policy_invalid",
            422,
        )
        guard = self.policy.get("test_gpu_guard")
        require(
            guard is None or keep == "idle" and idle <= limits["idle_shutdown_seconds"],
            "dstack_test_idle_policy_required",
            422,
        )
        result = {
            "version": 2,
            "keep_on": keep,
            "hold_until": now + hold if hold else 0,
            "idle_shutdown_seconds": idle,
            "reservation_seconds": self.policy["reservation_seconds"],
            "expected_runtime_s": expected_runtime,
        }
        if guard is not None:
            from .dstack_test_budget import task_guard, valid_task_id, validate_guard

            require(
                original_guard is not None or valid_task_id(body.get("test_task_id")),
                "dstack_test_task_required",
                422,
            )
            require(
                limits["max_runtime_seconds"]
                > expected_runtime + guard["stop_margin_seconds"],
                "dstack_test_runtime_limit_reached",
                422,
            )
            result["test_gpu_guard"] = (
                copy.deepcopy(validate_guard(original_guard))
                if original_guard is not None
                else task_guard(
                    guard,
                    body["test_task_id"],
                    limit=limits["test_task_limit_microusd"],
                    max_runtime=limits["max_runtime_seconds"],
                )
            )
        else:
            require("test_task_id" not in body, "dstack_test_task_invalid", 422)
        return result

    def fund_window(self, connection, intent, node, now):
        """Fund preparation, elapsed paid time and a full safe execution runway."""
        from .spending_limits import ADMIN_OWNER, lock_owner

        self.repo._lock_capacity(connection)
        lock_owner(connection, self.settings.tenant_id, ADMIN_OWNER)
        from .worker_admission import COMPLETION_MARGIN_SECONDS

        payload, lifecycle = node["payload"], node["payload"]["lifecycle"]
        from .dstack_test_budget import budget_reason

        reason = budget_reason(connection, intent, node, now)
        if reason:
            raise BudgetExceeded(reason)
        require(window_open(intent, node, now), "dstack_continuing_policy_unconfirmed")
        started = connection.execute(
            select(scaler_actions.c.create_started_at).where(
                scaler_actions.c.intent_id == intent["id"]
            )
        ).scalar_one_or_none()
        started = intent["created_at"] if started is None else started
        expected = lifecycle.get("expected_runtime_s")
        hourly, extra = (
            payload.get("hourly_cost_microusd"),
            payload.get("extra_reservation_microusd", 0),
        )
        require(
            _finite(started)
            and 0 <= now - started
            and _finite(expected)
            and 0 < expected <= 86400
            and _integer(hourly)
            and _integer(extra, 0),
            "dstack_funding_basis_unconfirmed",
        )
        chunk = lifecycle["reservation_seconds"]
        paid = (
            now
            - started
            + expected
            + max(lifecycle["idle_shutdown_seconds"], COMPLETION_MARGIN_SECONDS)
        )
        # Admission requires strictly more funding than the complete runway.
        seconds = math.ceil((paid + 1) / chunk) * chunk + 300
        minimum = math.ceil(hourly * seconds / 3600) + extra
        reason = budget_reason(connection, intent, node, now, minimum_reserved=minimum)
        if reason:
            raise BudgetExceeded(reason)
        return self.repo.fund_instance_window(
            connection, intent["id"], minimum, lifecycle["budget_account_ids"]
        )

    def catalog(self, principal):
        owner = self.authorize(principal)
        fields = (
            "id",
            "runtime_profile_id",
            "configuration_id",
            "recipe_id",
            "model_id",
            "mode",
            "pool",
            "backend",
            "gpu_names",
            "memory_gib",
            "disk_gib",
            "gpu_memory_gib",
            "cpu_count",
            "max_price_microusd",
            "max_ttl_seconds",
        )
        from .operator_prices import OperatorPrices

        with self.repo.engine.connect() as connection:
            prices = OperatorPrices(self.repo, self.settings)
            profiles = []
            observed_at = self.repo.clock()
            for profile in self.profiles.values():
                if profile["owner_id"] != owner:
                    continue
                price, _, _ = prices.read(connection, profile)
                reason = None
                if self.policy.get("lifecycle_version") == 2:
                    _, reason = self._execution_check(profile, observed_at)
                profiles.append(
                    {
                        **catalog_resources(profile, fields),
                        "max_price_microusd": price["max_price_microusd"],
                        "price_ceiling_microusd": price["price_ceiling_microusd"],
                        "price_policy_snapshot": price["snapshot"],
                        "execution_eligibility": {
                            "status": "confirmed" if reason is None else "blocked",
                            "reason_code": reason,
                            "observed_at": observed_at,
                        },
                    }
                )
        # New starts follow the configured authoring default; retained profiles
        # remain explicit choices and existing nodes are never rebound.
        default = getattr(self.settings, "default_deployment_profile_id", None)
        profiles.sort(key=lambda item: item["runtime_profile_id"] != default)
        return {
            "capacity_backend": BACKEND_MARKER,
            "enabled": self.policy.get("enabled") is True,
            "lifecycle_version": self.policy.get("lifecycle_version", 1),
            "test_task_id_required": self.policy.get("test_gpu_guard") is not None,
            "operator": {"account": owner},
            "profiles": profiles,
        }

    def admin_prices(self, principal):
        owner = self.authorize_admin(principal)
        from .operator_prices import OperatorPrices

        prices = OperatorPrices(self.repo, self.settings)
        with self.repo.engine.connect() as connection:
            return {
                "operator": {"account": owner, "admin": True},
                "expected_settings_version": self._admin_limits(connection)["version"],
                "profiles": [
                    prices.read(connection, profile)[0]
                    for profile in self.profiles.values()
                ],
                "audits": prices.audits(connection),
            }

    def update_price(self, principal, profile_id, body):
        self.authorize_admin(principal)
        profile = self.profiles.get(profile_id)
        require(profile is not None, "dstack_profile_not_found", 404)
        from .operator_prices import OperatorPrices
        from .spending_limits import admin_defaults

        return OperatorPrices(
            self.repo, self.settings, admin_defaults=admin_defaults(self)
        ).update(profile, body, principal)

    def preview_price(self, principal, profile_id, body):
        self.authorize_admin(principal)
        profile = self.profiles.get(profile_id)
        require(profile is not None, "dstack_profile_not_found", 404)
        from .operator_prices import OperatorPrices
        from .spending_limits import admin_defaults

        return OperatorPrices(
            self.repo, self.settings, admin_defaults=admin_defaults(self)
        ).preview(profile, body)

    def preview(self, principal, body):
        owner = self.authorize(principal)
        return self._preview_for_owner(owner, body)

    def _vast_host_selection(self, request, offers, demand_job_id):
        """Filter planned Vast offers by host policy/download speed; freeze the selector."""
        from .host_policy import FRESH_SECONDS, HostPolicyStore, selection

        mapper = getattr(self.capacity.client, "vast_offer_mapping", None)
        require(callable(mapper), "host_identity_unconfirmed")
        mapping = mapper(request, clock=self.repo.clock)
        require(
            isinstance(mapping, dict) and isinstance(mapping.get("offers"), list),
            "host_identity_unconfirmed",
        )
        by_offer = {r["offer_id"]: r for r in mapping["offers"]}
        store = HostPolicyStore(self.repo, self.settings.tenant_id)
        with self.repo._budget_read() as connection:
            from .host_policy import original_nodes

            nodes = original_nodes(connection, self.settings.tenant_id)
            excluded = set()
            if demand_job_id is not None:
                from .dstack_demand_start import excluded_download_machines

                excluded = excluded_download_machines(
                    connection, self.settings.tenant_id, demand_job_id
                )
            allowed = [
                r
                for r in offers
                if r.get("offer_id") in by_offer
                and by_offer[r['offer_id']]['machine_id'] not in excluded
                and type(by_offer[r['offer_id']].get('advertised_download_mbps'))
                in (int, float)
                and math.isfinite(by_offer[r['offer_id']]['advertised_download_mbps'])
                and by_offer[r['offer_id']]['advertised_download_mbps'] >= 1000
                and store.read(
                    connection, by_offer[r["offer_id"]]["machine_id"], nodes=nodes
                )[0]["effective"]
                != "deny"
            ]
            # Preference must not discard qualified ordinary fallback.
            # instance_types freezes membership; the reviewed native Vast
            # patch also preserves the explicit preferred-first order.
            offers = sorted(
                allowed,
                key=lambda r: (
                    not preferred_download_offer(by_offer[r['offer_id']]),
                    store.read(
                        connection,
                        by_offer[r["offer_id"]]["machine_id"],
                        nodes=nodes,
                    )[0]["effective"]
                    != "prefer",
                ),
            )[:MAX_HOST_CANDIDATES]
            require(offers, "dstack_no_compatible_host_offers")
            approved = [copy.deepcopy(by_offer[r["offer_id"]]) for r in offers]
            # Plan/native assignment uses bundle IDs, not a display-only
            # filtered offers list. The same selector is frozen below.
            selected = selection(
                {
                    "version": 1,
                    "observed_at": mapping["observed_at"],
                    "expires_at": mapping["observed_at"] + FRESH_SECONDS,
                    "policy_snapshot": store.admission_snapshot(connection, approved),
                    "offers": approved,
                },
                now=self.repo.clock(),
            )
        return replace(request, host_selection=selected), offers, store

    def _preview_for_owner(self, owner, body, *, demand_job_id=None):
        self._enabled()
        continuing = self.policy.get("lifecycle_version") == 2
        allowed = (
            {
                "profile_id",
                "ttl_seconds",
                "keep_on",
                "hold_seconds",
                "idle_shutdown_seconds",
                "group_id",
            }
            if continuing
            else {"profile_id", "ttl_seconds", "group_id"}
        )
        allowed.add("max_price_microusd")
        if self.policy.get("test_gpu_guard") is not None:
            allowed.add("test_task_id")
        require(
            isinstance(body, dict)
            and "profile_id" in body
            and not set(body) - allowed
            and (continuing or {"profile_id", "ttl_seconds"} <= set(body)),
            "dstack_preview_invalid",
            422,
        )
        require(safe_id(body["profile_id"]), "dstack_preview_invalid", 422)
        profile = self.profiles.get(body["profile_id"])
        require(
            profile is not None and profile["owner_id"] == owner,
            "dstack_profile_not_found",
            404,
        )
        from .operator_prices import (
            OperatorPrices,
            mode_price_ceiling,
            enforce_price_limit,
        )

        prices = OperatorPrices(self.repo, self.settings)
        with self.repo.engine.connect() as connection:
            price, _, _ = prices.read(connection, profile)
            admin_limits = self._admin_limits(connection)
        chosen_price = body.get("max_price_microusd", price["max_price_microusd"])
        require(
            _integer(chosen_price),
            "dstack_price_ceiling_exceeded",
            422,
        )
        enforce_price_limit(
            chosen_price,
            price["price_ceiling_microusd"],
            "profile",
            "dstack_price_ceiling_exceeded",
        )
        if admin_limits["adopted"] or self.policy.get("demand_start"):
            enforce_price_limit(
                chosen_price,
                mode_price_ceiling(profile, admin_limits),
                "mode",
                "dstack_price_ceiling_exceeded",
            )
        enforce_price_limit(
            chosen_price,
            admin_limits["value"]["max_hourly_price_microusd"],
            "admin",
            "admin_hourly_price_limit_reached",
        )
        profile = copy.deepcopy(profile)
        profile["max_price_microusd"] = chosen_price
        ttl = body.get("ttl_seconds", self.policy.get("reservation_seconds"))
        require(
            _integer(
                ttl,
                120,
                86400
                if continuing
                else min(profile["max_ttl_seconds"], self.policy["max_ttl_seconds"]),
            ),
            "dstack_runtime_limit_invalid",
            422,
        )
        now = self.repo.clock()
        if not continuing:
            require(
                self.policy["expires_at"] is None
                or now + ttl <= self.policy["expires_at"],
                "dstack_authority_window_exceeded",
            )
        lifecycle = (
            self._lifecycle(
                body, now, self._expected_runtime(profile), limits=admin_limits["value"]
            )
            if continuing
            else None
        )
        if continuing:
            lifecycle["budget_account_ids"] = copy.deepcopy(
                profile["budget_account_ids"]
            )
        preview_id = str(uuid.uuid4())
        request = self._capacity_request(
            profile,
            preview_id,
            now,
            None if continuing else now + ttl,
            None if continuing else ttl,
            lifecycle_version=2 if continuing else 1,
            test_max_duration=lifecycle.get("test_gpu_guard", {}).get(
                "max_runtime_seconds"
            )
            if lifecycle
            else None,
            test_task_id=lifecycle.get("test_gpu_guard", {}).get("task_id")
            if lifecycle
            else None,
        )
        # One intent always rents from exactly one backend. Each backend in
        # order (primary, then configured fallbacks) is planned and fully
        # filtered; the first with eligible offers binds the preview and start.
        backends = [request.backend, *profile.get("fallback_backends", [])]
        store, offers = None, []
        for index, backend in enumerate(backends):
            candidate = (
                request if index == 0 else replace(request, backend=backend, regions=())
            )
            offers = plan_offers(
                self.capacity.plan(candidate, self.config["ssh_public_key"]),
                candidate,
            )
            if offers and backend == "vastai":
                try:
                    candidate, offers, store = self._vast_host_selection(
                        candidate, offers, demand_job_id
                    )
                except OperatorError as error:
                    if (
                        error.code != "dstack_no_compatible_host_offers"
                        or index == len(backends) - 1
                    ):
                        raise
                    offers = []
            if offers:
                request = candidate
                if index:
                    profile["backend"] = backend
                    profile.pop("regions", None)
                break
        require(bool(offers), "dstack_no_compatible_offers")
        # Reserve preparation and a complete job from the first paid request,
        # plus shutdown grace; continuing funding has no absolute lifetime.
        initial_seconds = lifecycle["reservation_seconds"] if continuing else ttl
        if continuing:
            from .worker_admission import COMPLETION_MARGIN_SECONDS

            initial_seconds = (
                math.ceil(
                    (
                        lifecycle["expected_runtime_s"]
                        + max(
                            lifecycle["idle_shutdown_seconds"],
                            COMPLETION_MARGIN_SECONDS,
                        )
                        + 1
                    )
                    / initial_seconds
                )
                * initial_seconds
            )
        cost = (
            math.ceil(profile["max_price_microusd"] * (initial_seconds + 300) / 3600)
            + profile["extra_reservation_microusd"]
        )
        payload = {
            "capacity_backend": BACKEND_MARKER,
            "tenant_id": self.settings.tenant_id,
            "owner_id": owner,
            "profile_id": profile["id"],
            "profile": profile,
            "config_fingerprint": self.fingerprint,
            "admin_limits_snapshot": request_hash(admin_limits),
            "request": asdict(request),
            "reservation_microusd": cost,
            "price_policy_snapshot": price["snapshot"],
        }
        if continuing:
            payload["lifecycle"] = lifecycle
        expires = min(
            bound
            for bound in (now + 120, self.policy["expires_at"], request.hard_deadline)
            if bound is not None
        )
        if request.host_selection is not None:
            expires = min(expires, request.host_selection["expires_at"])
        with self.repo.transaction() as connection:
            if demand_job_id is not None:
                from .identity import lock_account_gate

                lock_account_gate(connection, self.settings.tenant_id)
            self.repo._lock_capacity(connection)
            if demand_job_id is not None:
                from .dstack_demand import validate_start

                payload["demand_start"] = validate_start(
                    self, connection, demand_job_id, profile["id"]
                )
            require(
                prices.read(connection, self.profiles[profile["id"]])[0]["snapshot"]
                == price["snapshot"],
                "dstack_price_policy_changed",
            )
            if request.host_selection is not None:
                from .host_policy import selection

                require(
                    store.admission_snapshot(
                        connection, request.host_selection["offers"]
                    )
                    == request.host_selection["policy_snapshot"],
                    "host_policy_changed",
                )
                from .host_scorecard import OperatorHosts
                from .stage_metrics import configured_stage_reader

                stage_reader = configured_stage_reader(
                    getattr(self.settings, "stage_query_config_file", None)
                )
                histories = OperatorHosts(
                    self.repo, self.settings, stage_reader
                ).histories(
                    connection,
                    owner,
                    request.host_selection["offers"],
                    profile_id=profile["id"],
                    observed_at=request.host_selection["observed_at"],
                    expires_at=expires,
                )
                for offer in offers:
                    offer["host_history"] = histories[offer["offer_id"]]
                selection(request.host_selection, now=self.repo.clock())
            from .dstack_workspace import owned_group

            group_id = body.get("group_id")
            if group_id is not None:
                owned_group(connection, self.settings, owner, group_id)
            payload["group_id"] = group_id
            connection.execute(
                insert(operator_previews).values(
                    id=preview_id,
                    actor=_actor(self.settings.tenant_id, owner),
                    payload=payload,
                    created_at=now,
                    expires_at=expires,
                )
            )
        return {
            "preview_id": preview_id,
            "expires_at": expires,
            "hard_deadline": request.hard_deadline,
            "reservation_microusd": cost,
            "hourly_cost_microusd": profile["max_price_microusd"],
            "price_basis": "approved_ceiling",
            "max_price_microusd": profile["max_price_microusd"],
            "offer_status": "advisory",
            "offers": offers,
            "can_start": True,
            "capacity_backend": BACKEND_MARKER,
            **(
                {
                    "lifecycle": copy.deepcopy(lifecycle),
                    "initial_reservation_seconds": initial_seconds,
                }
                if continuing
                else {}
            ),
        }

    def _existing_command(self, connection, actor, key, digest):
        require(safe_id(key), "operator_idempotency_key_invalid", 422)
        command = self.repo._locked(
            connection,
            select(operator_commands).where(
                operator_commands.c.actor == actor,
                operator_commands.c.idempotency_key == key,
            ),
        )
        if command:
            require(command["request_hash"] == digest, "operator_idempotency_conflict")
        return dict(command) if command else None

    def _live_capacity_usage(self, connection):
        # Legacy invoices/intents remain in the ledger, but are not current
        # dstack capacity. Only exact owned new-path allocations count here.
        nodes = [
            dict(node)
            for node in connection.execute(select(operator_nodes)).mappings()
            if node["payload"].get("capacity_backend") == BACKEND_MARKER
        ]
        return live_capacity_usage(
            self.capacity.client,
            nodes,
            provider_inventory=self.provider_inventory,
            clock=self.repo.clock,
            fresh_seconds=self.policy["observation_fresh_seconds"],
        )

    def machine_limits(self, connection, *, for_demand=False):
        from .capacity_settings import LEGACY_DEMAND_MAX_MACHINES

        settings = self._admin_limits(connection)
        if settings["adopted"]:
            maximum = settings["value"]["max_machines"]
            # A production policy adoption does not widen the independently
            # qualified staging test guard or its explicitly protected allowance.
            if "test_gpu_guard" in self.policy:
                return (
                    min(maximum, self.policy["max_instances"]),
                    min(maximum, self.policy["max_physical_gpus"]),
                )
            return maximum, maximum
        # Old environments are not adopted by deploying a reader. Preserve the
        # old protected configuration until the explicit audited settings write.
        maximum = (
            LEGACY_DEMAND_MAX_MACHINES
            if for_demand or self.policy.get("demand_start")
            else 128
        )
        return (
            min(maximum, self.policy["max_instances"]),
            min(maximum, self.policy["max_physical_gpus"]),
        )

    def prelaunch_capacity_limits(
        self,
        connection,
        *,
        profile=None,
        request=None,
        capacity_snapshot=None,
        for_demand=False,
    ):
        from .capacity_settings import admission_snapshot
        from .control import effective_registration_limits
        from .repository import capacity_gate, pool_limits

        global_limits = (
            connection.execute(
                select(capacity_gate).where(capacity_gate.c.id == "global")
            )
            .mappings()
            .first()
        )
        stored = self._admin_limits(connection)
        current_snapshot = admission_snapshot(stored)
        selected = (
            self.profiles.get(profile.get("id")) if isinstance(profile, dict) else None
        )
        bound_profile = (
            selected is not None
            and all(
                profile.get(key) == selected.get(key)
                for key in (
                    "id",
                    "owner_id",
                    "project_id",
                    "pool",
                    "runtime_profile_id",
                    "mode",
                    "configuration_id",
                )
            )
            and profile.get("backend") in profile_backends(selected, PROVIDERS)
        )
        pool = None
        if bound_profile:
            pool = (
                connection.execute(
                    select(pool_limits).where(pool_limits.c.pool == selected["pool"])
                )
                .mappings()
                .first()
            )
            if pool is None:
                raise BudgetExceeded("pool_disabled")
        admitted = None
        if (
            current_snapshot is not None
            and bound_profile
            and type(self.policy.get("lifecycle_version")) is int
            and self.policy["lifecycle_version"] == 2
        ):
            if for_demand:
                # Eligible demand already selects this exact configured v2 profile;
                # the normal start revalidates and freezes the current snapshot.
                admitted = current_snapshot
            elif capacity_snapshot == current_snapshot and isinstance(request, dict):
                expected = {
                    "backend": request.get("backend")
                    if request.get("backend") in profile_backends(selected, PROVIDERS)
                    else selected["backend"],
                    "profile_id": selected["runtime_profile_id"],
                    "mode": selected["mode"],
                    "model_id": selected["model_id"],
                    "configuration_id": selected["configuration_id"],
                    "manifest_digest": selected["manifest_digest"],
                    "hard_deadline": None,
                    "gpu_count": 1,
                    "lifecycle_version": 2,
                }
                if (
                    all(
                        key in request and request[key] == value
                        for key, value in expected.items()
                    )
                    and type(request["gpu_count"]) is int
                    and type(request["lifecycle_version"]) is int
                ):
                    admitted = current_snapshot
        limits = effective_registration_limits(global_limits, pool, snapshot=admitted)
        maximum, gpus = self.machine_limits(connection, for_demand=for_demand)
        return {
            "max_instances": min(maximum, limits["max_instances"]),
            "max_physical_gpus": min(gpus, limits["max_physical_gpus"]),
        }

    def _limits(
        self, connection, hourly, *, profile=None, request=None, capacity_snapshot=None
    ):
        from .control import (
            registration_capacity_projection,
            check_registration_capacity,
        )

        usage = self.prelaunch_capacity_usage(connection)
        limits = self.prelaunch_capacity_limits(
            connection,
            profile=profile,
            request=request,
            capacity_snapshot=capacity_snapshot,
        )
        check_registration_capacity(registration_capacity_projection(usage), limits)
        require(
            usage["hourly_cost_microusd"] + hourly
            <= self._admin_limits(connection)["value"]["fleet_hourly_limit_microusd"],
            "dstack_hourly_limit_exceeded",
        )
        return usage

    def _observed_registration_usage(self, nodes):
        # Retain only this call's exact read in memory. The existing helper
        # owns immutable-run/identity validation and account absence; its
        # conservative count of one is not proof of a one-GPU allocation.
        observed_runs = {}
        client = self.capacity.client

        class ObservedClient:
            project, endpoint = client.project, getattr(client, "endpoint", None)

            def get(self, **identity):
                run = client.get(**identity)
                observed_runs[(identity.get("run_id"), identity.get("run_name"))] = run
                return run

        usage = live_capacity_usage(
            ObservedClient(),
            nodes,
            provider_inventory=self.provider_inventory,
            clock=self.repo.clock,
            fresh_seconds=self.policy["observation_fresh_seconds"],
        )
        return usage, observed_runs

    def prelaunch_capacity_usage(self, connection):
        """Use the same global overlay as registration under the capacity lock."""
        from .native_capacity_inventory import prelaunch_usage

        return prelaunch_usage(self, connection)

    def _registration_resource_overlay(
        self, connection, nodes, intents, usage, observed_runs
    ):
        """Read-only global registration inventory; no release/settlement writes."""
        by_id = {node["intent_id"]: node for node in nodes}
        excluded = set()
        absent_workers = {}
        for observation in usage["observations"]:
            if observation["counted"]:
                continue
            node, intent = (
                by_id[observation["intent_id"]],
                intents.get(observation["intent_id"]),
            )
            original = node["payload"]["dstack"]
            if (
                intent is None
                or PROVIDERS.get(original.get("backend")) != intent["provider"]
            ):
                continue
            if (
                observation["reason_code"] == "dstack_terminal_provider_absent"
                and original.get("provider_instance_id")
                == intent["provider_instance_id"]
            ):
                # The original provider identity may never have been journaled
                # before provisioning failed. live_capacity_usage still proved
                # this exact owned allocation terminal and its complete fresh
                # account absence. Two null journals are consistent with that
                # proof; conflicting identities remain conservative.
                excluded.add(intent["id"])
                if intent["provider_instance_id"]:
                    key = (intent["provider"], intent["provider_instance_id"])
                    absent_workers.setdefault(key, set()).add(
                        "dstack-" + intent["id"].replace("-", "")
                    )
            elif (
                observation["reason_code"]
                in {
                    "dstack_terminal_no_allocation",
                    "dstack_terminal_provider_accounted",
                }
                and intent["provider_instance_id"] is None
                and original.get("provider_instance_id") is None
            ):
                excluded.add(intent["id"])
            elif (
                observation["reason_code"] == "dstack_never_applied"
                and intent["state"] == "destroyed"
            ):
                excluded.add(intent["id"])
        # The same fresh owned native GET may know a provider identity before
        # observe() journals it. Normalize only a non-conflicting active identity;
        # otherwise an intent key and its known allocation would count twice.
        active_keys, unconfirmed_allocations = {}, []
        for observation in usage["observations"]:
            if observation["reason_code"] != "dstack_capacity_active":
                continue
            node = by_id[observation["intent_id"]]
            original = node["payload"]["dstack"]
            intent = intents.get(node["intent_id"])
            run = observed_runs.get(
                (original["run_id"], None)
                if original.get("run_id")
                else (None, original["run_name"])
            )
            jpd = (run.get("latest_job_submission") or {}).get(
                "job_provisioning_data"
            ) or {}
            instance = jpd.get("instance_id")
            if (
                intent is not None
                and isinstance(instance, str)
                and instance
                and jpd.get("backend") == original.get("backend")
                and PROVIDERS.get(jpd.get("backend")) == intent["provider"]
                and intent["provider_instance_id"] in (None, instance)
                and original.get("provider_instance_id") in (None, instance)
                and original["backend"] + ":" + instance in usage["resources"]
            ):
                active_keys[intent["id"]] = (intent["provider"], instance)
            gpus = (jpd.get("instance_type") or {}).get("resources", {}).get("gpus")
            if (
                node["intent_id"] not in active_keys
                or not isinstance(gpus, list)
                or not 1 <= len(gpus) <= 128
            ):
                unconfirmed_allocations.append(node["intent_id"])
        resources, active_intents = {}, []
        for intent in intents.values():
            if (
                intent["state"] == "destroyed"
                or intent["id"] in excluded
                or manually_reviewed_inactive(connection, intent)
            ):
                continue
            active_intents.append(intent["id"])
            key = active_keys.get(
                intent["id"],
                (
                    intent["provider"],
                    intent["provider_instance_id"] or "intent:" + intent["id"],
                ),
            )
            resources[key] = resources.get(key, 0) + intent["physical_gpus"]
        # A second included binding for the same physical identity defeats the
        # absence exclusion. Devices owned by unrelated/legacy workers remain.
        live_keys = {}
        for resource_key, count in usage["resources"].items():
            for backend, provider in PROVIDERS.items():
                if resource_key.startswith(backend + ":"):
                    live_keys[(provider, resource_key[len(backend) + 1 :])] = count
                    break
        device_counts = {}
        for device in connection.execute(
            select(registered_devices).where(
                registered_devices.c.state != "released",
                registered_devices.c.provider != "mock",
            )
        ).mappings():
            key = (device["provider"], device["instance_id"])
            if (
                device["worker_id"] in absent_workers.get(key, ())
                and key not in resources
                and key not in live_keys
            ):
                continue
            device_counts[key] = device_counts.get(key, 0) + 1
        for key, count in device_counts.items():
            resources[key] = max(resources.get(key, 0), count)
        # Positive live allocations may exceed the original intent's requested
        # count. Retain them globally even when a different node is registering;
        # unknown intent-key fallbacks never replace original ledger identities.
        for key, count in live_keys.items():
            resources[key] = max(resources.get(key, 0), count)
        return {
            "resources": resources,
            "instances": len(resources),
            "physical_gpus": sum(resources.values()),
            "all_resource_intent_ids": tuple(active_intents),
            "native_resources": tuple(live_keys),
            "unconfirmed_allocation_ids": tuple(unconfirmed_allocations),
        }

    def registration_capacity_usage(self, connection, spec):
        """Overlay proven dstack inactivity on ALL original global resources.

        WorkerControl holds the original capacity lock while this read runs.
        Terminal owned runs plus complete, fresh same-account absence are the
        existing live_capacity_usage evidence; they are NOT destruction or
        settlement. Unaccounted provider identities, legacy intents and
        unrelated physical devices keep counting. No row, reservation or
        provider is mutated.
        """
        nodes = [
            dict(node)
            for node in connection.execute(select(operator_nodes)).mappings()
            if node["payload"].get("capacity_backend") == BACKEND_MARKER
        ]
        intents = {
            row["id"]: dict(row)
            for row in connection.execute(select(instance_intents)).mappings()
        }
        matching = [
            node
            for node in nodes
            if (intent := intents.get(node["intent_id"])) is not None
            and intent["provider"] == spec.provider
            and intent["provider_instance_id"] == spec.instance_id
            and intent["pool"] == spec.pool
            and node["payload"].get("tenant_id") == self.settings.tenant_id
            and node["desired_state"] == "running"
            and intent["state"] in {"starting", "ready", "busy"}
        ]
        require(
            len(matching) == 1
            and spec.backend == "wangp-worker"
            and spec.dispatch_backend == "hatchet-v1",
            "dstack_registration_binding_invalid",
        )
        binding = matching[0]["payload"]["dstack"]
        require(
            binding.get("provider_instance_id") == spec.instance_id
            and PROVIDERS.get(binding.get("backend")) == spec.provider
            and binding.get("model_id") == spec.model_id
            and binding.get("configuration_id") == spec.configuration_id
            and binding.get("manifest_digest") == spec.engine_manifest_digest
            and spec.recipe_ids
            == (
                {"fl": ("h3-base-fl2va-v1",), "ref": ("h3-base-ref2va-v1",)}.get(
                    binding.get("mode")
                )
            )
            and len(spec.physical_gpu_ids) == 1,
            "dstack_registration_binding_invalid",
        )
        usage, observed_runs = self._observed_registration_usage(nodes)
        current = next(
            observation
            for observation in usage["observations"]
            if observation["intent_id"] == matching[0]["intent_id"]
        )
        require(
            current["counted"]
            and current["dstack_status"] == "running"
            and current["reason_code"] == "dstack_capacity_active",
            "dstack_registration_run_unconfirmed",
        )
        resource_key = binding["backend"] + ":" + spec.instance_id
        run = (
            observed_runs.get((binding["run_id"], None))
            if binding.get("run_id")
            else observed_runs.get((None, binding["run_name"]))
        )
        gpus = run["latest_job_submission"]["job_provisioning_data"]["instance_type"][
            "resources"
        ].get("gpus")
        require(
            isinstance(gpus, list)
            and len(gpus) == 1
            and usage["resources"].get(resource_key) == 1,
            "dstack_registration_allocation_unconfirmed",
        )
        from .native_capacity_inventory import resource_overlay

        result = resource_overlay(
            self, connection, nodes, intents, usage, observed_runs
        )
        resources = result["resources"]
        original = matching[0]["payload"]
        snapshot = original.get("capacity_policy_snapshot")
        if (
            snapshot is not None
            and original.get("request", {}).get("lifecycle_version") == 2
            and binding.get("lifecycle_version") == 2
        ):
            from .capacity_settings import validate_snapshot

            validate_snapshot(snapshot)
            intent = intents[matching[0]["intent_id"]]
            require(
                type(original["request"]["lifecycle_version"]) is int
                and type(binding["lifecycle_version"]) is int
                and type(original.get("lifecycle", {}).get("version")) is int
                and original["lifecycle"]["version"] == 2
                and original.get("hard_deadline") is None
                and intent["hard_deadline"] is None
                and intent["physical_gpus"] == intent["slots"] == 1
                and spec.worker_id
                == "dstack-" + matching[0]["intent_id"].replace("-", "")
                and resources.get((spec.provider, spec.instance_id)) == 1,
                "dstack_registration_admission_invalid",
            )
            # This fulfills an original one-GPU admission, not a newly paid
            # capacity grant under today's possibly lower settings.
            result["capacity_admission"] = {
                "capacity_backend": BACKEND_MARKER,
                "lifecycle_version": 2,
                "intent_id": intent["id"],
                "worker_id": spec.worker_id,
                "provider": spec.provider,
                "instance_id": spec.instance_id,
                "pool": spec.pool,
                "capacity_policy_snapshot": copy.deepcopy(snapshot),
            }
        return result

    def start(self, principal, body, key):
        owner = self.authorize(principal)
        return self._start_for_owner(owner, principal.actor_id, body, key)

    def _start_for_owner(self, owner, actor_id, body, key, *, demand_job_id=None):
        require(
            isinstance(body, dict)
            and set(body) == {"preview_id"}
            and safe_id(body["preview_id"]),
            "dstack_start_invalid",
            422,
        )
        actor = _actor(self.settings.tenant_id, owner)
        digest = request_hash({"kind": "dstack_start", "body": body})
        with self.repo.transaction() as connection:
            if demand_job_id is not None:
                from .identity import lock_account_gate

                lock_account_gate(connection, self.settings.tenant_id)
            self.repo._lock_capacity(connection)
            from .spending_limits import ADMIN_OWNER, lock_owner

            lock_owner(connection, self.settings.tenant_id, ADMIN_OWNER)
            existing = self._existing_command(connection, actor, key, digest)
            if existing:
                return {
                    "command_id": existing["id"],
                    "node_id": existing["payload"]["node_id"],
                    "state": existing["state"],
                }
            value, demand, profile = self._validate_start_preview(
                connection, owner, actor, body["preview_id"], demand_job_id
            )
            now, capacity_snapshot, usage = self._admit_start_capacity(
                connection, owner, value, profile
            )
            original = require_preview_ram(value["request"], profile)
            host_selection = self._validate_start_hosts(
                connection, profile, original, now, demand_job_id
            )
            intent, continuing = self._reserve_start_intent(
                connection,
                owner,
                actor_id,
                body["preview_id"],
                value,
                profile,
                original,
                usage,
                capacity_snapshot,
            )
            request, command_id = self._record_start_request(
                connection,
                owner,
                actor,
                key,
                digest,
                body["preview_id"],
                value,
                demand,
                profile,
                original,
                now,
                intent,
                host_selection,
                capacity_snapshot,
                continuing,
            )
        return self._apply_reserved_start(intent, request, command_id)

    def _validate_start_preview(
        self, connection, owner, actor, preview_id, demand_job_id
    ):
        """Validate the original preview and single-use fence under the start locks."""
        self._enabled()
        preview = self.repo._locked(
            connection,
            select(operator_previews).where(
                operator_previews.c.id == preview_id,
                operator_previews.c.actor == actor,
            ),
        )
        require(
            preview is not None and self.repo.clock() < preview["expires_at"],
            "dstack_preview_expired",
        )
        value = preview["payload"]
        require(
            value.get("config_fingerprint") == self.fingerprint
            and value.get("capacity_backend") == BACKEND_MARKER,
            "dstack_preview_configuration_changed",
        )
        require(
            value.get("admin_limits_snapshot")
            == request_hash(self._admin_limits(connection)),
            "spending_settings_changed",
        )
        from .operator_prices import OperatorPrices

        trusted = self.profiles.get(value["profile_id"])
        require(
            trusted is not None and trusted["owner_id"] == owner,
            "dstack_profile_not_found",
            404,
        )
        demand = value.get("demand_start")
        if demand_job_id is not None:
            from .dstack_demand import validate_start

            require(
                demand
                == validate_start(self, connection, demand_job_id, trusted["id"]),
                "dstack_demand_changed",
            )
        else:
            require(demand is None, "dstack_demand_start_internal_only")
        price, _, price_version = OperatorPrices(self.repo, self.settings).read(
            connection, trusted
        )
        require(
            price["snapshot"] == value["price_policy_snapshot"]
            if "price_policy_snapshot" in value
            else price_version == 0
            and value["profile"]["max_price_microusd"] == price["max_price_microusd"],
            "dstack_price_policy_changed",
        )
        # A preview is single-use even when another idempotency key is supplied.
        previous = connection.execute(
            select(operator_commands.c.id).where(
                operator_commands.c.actor == actor,
                operator_commands.c.kind == "dstack_start",
                operator_commands.c.payload["preview_id"].as_string() == preview_id,
            )
        ).first()
        require(previous is None, "dstack_preview_consumed")
        profile = value["profile"]
        return value, demand, profile

    def _admit_start_capacity(self, connection, owner, value, profile):
        from .capacity_lookahead_runtime import admit_capacity

        return admit_capacity(self, connection, owner, value, profile)

    def _validate_start_hosts(self, connection, profile, original, now, demand_job_id):
        """Re-read exact Vast offer identities before reserving the original intent."""
        host_selection = original.get("host_selection")
        if profile["backend"] == "vastai":
            from .host_policy import HostPolicyStore, selection

            require(host_selection is not None, "host_selection_required")
            host_selection = selection(host_selection, now=now)
            require(
                HostPolicyStore(self.repo, self.settings.tenant_id).admission_snapshot(
                    connection, host_selection["offers"], now=now
                )
                == host_selection["policy_snapshot"],
                "host_policy_changed",
            )
            # Re-read exact offer identities before any reservation. Stock
            # presence isn't allocation proof; a missing/changed mapping
            # refuses this new intent, without changing old paid runs.
            fresh = self.capacity.client.vast_offer_mapping(
                request_from_document(original), clock=self.repo.clock
            )
            current = {
                r['offer_id']: r['machine_id']
                for r in fresh['offers']
                if type(r.get('advertised_download_mbps')) in (int, float)
                and math.isfinite(r['advertised_download_mbps'])
                and r['advertised_download_mbps'] >= 1000
            }
            excluded = set()
            if demand_job_id is not None:
                from .dstack_demand_start import excluded_download_machines

                excluded = excluded_download_machines(
                    connection, self.settings.tenant_id, demand_job_id
                )
            require(
                all(
                    current.get(r["offer_id"]) == r["machine_id"]
                    and r['machine_id'] not in excluded
                    for r in host_selection["offers"]
                ),
                "host_offer_identity_changed",
            )
            self.capacity.client.reusable_hosts_confirmed(
                {r["offer_id"]: r["machine_id"] for r in host_selection["offers"]}
            )
            selection(host_selection, now=self.repo.clock())
        return host_selection

    def _reserve_start_intent(
        self,
        connection,
        owner,
        actor_id,
        preview_id,
        value,
        profile,
        original,
        usage,
        capacity_snapshot,
    ):
        """Preserve the scoped spend cap and reserve one immutable instance intent."""
        continuing = original.get("lifecycle_version", 1) == 2
        if continuing:
            # A tenant/global account alone cannot authorize unlimited
            # keep-on for a person. Require their original scoped cap.
            accounts = list(
                connection.execute(
                    select(budget_accounts).where(
                        budget_accounts.c.id.in_(profile["budget_account_ids"])
                    )
                ).mappings()
            )
            require(
                any(
                    account["tenant_id"] == self.settings.tenant_id
                    and account["owner_id"] == owner
                    and account["project_id"] in (None, profile["project_id"])
                    for account in accounts
                ),
                "dstack_owner_spend_limit_required",
            )
            require(
                self._expected_runtime(profile)
                == value["lifecycle"]["expected_runtime_s"],
                "dstack_execution_policy_changed",
            )
        scope = Scope(
            self.settings.tenant_id,
            owner,
            profile["project_id"],
            actor_id,
        )
        intent = self.repo.reserve_instance_intent(
            scope,
            profile["pool"],
            "dstack:" + preview_id,
            physical_gpus=1,
            slots=1,
            reserved_cost_microusd=value["reservation_microusd"],
            hard_deadline=original["hard_deadline"],
            budget_account_ids=profile["budget_account_ids"],
            dry_run=False,
            provider=PROVIDERS[profile["backend"]],
            connection=connection,
            continuing=continuing,
            capacity_usage=usage,
            active_instance_ids=usage["active_instance_ids"],
            capacity_policy_snapshot=capacity_snapshot if continuing else None,
            capacity_provenance={
                "capacity_backend": BACKEND_MARKER,
                "lifecycle_version": 2,
                "tenant_id": scope.tenant_id,
                "owner_id": scope.owner_id,
                "project_id": scope.project_id,
                "pool": profile["pool"],
                "provider": PROVIDERS[profile["backend"]],
            }
            if continuing and capacity_snapshot is not None
            else None,
        )
        return intent, continuing

    def _record_start_request(
        self,
        connection,
        owner,
        actor,
        key,
        digest,
        preview_id,
        value,
        demand,
        profile,
        original,
        now,
        intent,
        host_selection,
        capacity_snapshot,
        continuing,
    ):
        """Persist request, command, node and outbox in the original transaction."""
        request = self._capacity_request(
            profile,
            intent["id"],
            now,
            original["hard_deadline"],
            original["max_duration_s"],
            lifecycle_version=original.get("lifecycle_version", 1),
            test_max_duration=original.get("test_max_duration_s"),
            test_task_id=original.get("test_task_id"),
        )
        if host_selection is not None:
            request = replace(request, host_selection=copy.deepcopy(host_selection))
        binding = request.binding(self.config["ssh_public_key"])
        # Which dstack server owns this run (local or Sky); see live_capacity_usage.
        binding["control_plane"] = control_plane(self.capacity.client)
        command_id = str(uuid.uuid4())
        command_payload = {
            "preview_id": preview_id,
            "node_id": intent["id"],
            "capacity_backend": BACKEND_MARKER,
        }
        connection.execute(
            insert(operator_commands).values(
                id=command_id,
                actor=actor,
                idempotency_key=key,
                request_hash=digest,
                kind="dstack_start",
                state="accepted",
                payload=command_payload,
                created_at=now,
                updated_at=now,
            )
        )
        payload = {
            "capacity_backend": BACKEND_MARKER,
            "command_revision": 0,
            "tenant_id": self.settings.tenant_id,
            "owner_id": owner,
            "project_id": profile["project_id"],
            "provider": PROVIDERS[profile["backend"]],
            "configuration_id": profile["configuration_id"],
            "hard_deadline": request.hard_deadline,
            "group_id": value.get("group_id"),
            "observation_fresh_seconds": self.policy["observation_fresh_seconds"],
            "hourly_cost_microusd": profile["max_price_microusd"],
            "hold_until": 0,
            "selection": {
                "runtime_profile_id": profile["runtime_profile_id"],
                "mode": profile["mode"],
                "gpu_type": profile["gpu_names"][0],
                "node_count": 1,
                "gpu_count": 1,
                "ttl_seconds": original["max_duration_s"],
                "provider": PROVIDERS[profile["backend"]],
            },
            "request": asdict(request),
            "dstack": binding,
        }
        if demand is not None:
            payload["demand_start"] = copy.deepcopy(demand)
        if capacity_snapshot is not None:
            payload["capacity_policy_snapshot"] = copy.deepcopy(capacity_snapshot)
        if continuing:
            payload["lifecycle"] = copy.deepcopy(value["lifecycle"])
            payload["hold_until"] = payload["lifecycle"]["hold_until"]
            payload["extra_reservation_microusd"] = profile[
                "extra_reservation_microusd"
            ]
        connection.execute(
            insert(operator_nodes).values(
                intent_id=intent["id"],
                command_id=command_id,
                ordinal=0,
                binding_id=profile["id"],
                binding_hash=request_hash(binding),
                payload=payload,
                desired_state="running",
                runtime_state="reserved",
                updated_at=now,
            )
        )
        if value.get("group_id") is not None:
            from .dstack_workspace import advance_group_version

            advance_group_version(
                connection, self.settings, owner, value["group_id"], now
            )
        connection.execute(
            insert(scaler_actions).values(
                intent_id=intent["id"],
                pool=profile["pool"],
                launch_spec={
                    "capacity_backend": BACKEND_MARKER,
                    "request": asdict(request),
                },
            )
        )
        self.repo._emit(
            connection,
            "dstack.start_requested",
            intent["id"],
            {"intent_id": intent["id"], "command_id": command_id},
        )
        return request, command_id

    def _apply_reserved_start(self, intent, request, command_id):
        """Apply once after commit and preserve the reviewed-settlement read-back fence."""
        # The reservation, immutable request and command survive any crash before
        # or after HTTP. Controller restart resumes this exact intent only.
        try:
            observation = self.capacity.start(request, self.config["ssh_public_key"])
            state = "unknown" if "unknown" in observation["state"] else "running"
        except DstackError:
            state = "waiting"
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            current = self.repo._locked(
                connection,
                select(operator_commands).where(operator_commands.c.id == command_id),
            )
            node = self.repo._locked(
                connection,
                select(operator_nodes).where(
                    operator_nodes.c.intent_id == intent["id"]
                ),
            )
            current_intent = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == intent["id"]),
            )
            from .settlement_recovery import reviewed_instance_fenced

            if reviewed_instance_fenced(connection, current_intent, node):
                state = current["state"]
            else:
                connection.execute(
                    update(operator_commands)
                    .where(operator_commands.c.id == command_id)
                    .values(state=state, updated_at=self.repo.clock())
                )
        return {"command_id": command_id, "node_id": intent["id"], "state": state}

    def _owned_node(self, connection, principal, node_id, *, allow_admin=False):
        owner = self.authorize(principal)
        node = self.repo._locked(
            connection,
            select(operator_nodes).where(operator_nodes.c.intent_id == node_id),
        )
        require(
            node is not None
            and node["payload"].get("capacity_backend") == BACKEND_MARKER
            and node["payload"].get("tenant_id") == self.settings.tenant_id
            and (
                node["payload"].get("owner_id") == owner
                or allow_admin
                and self._is_admin(principal)
            ),
            "dstack_node_not_found",
            404,
        )
        return dict(node)

    def state(self, principal):
        owner = self.authorize(principal)
        return self._state_for_owner(owner)

    def _is_admin(self, principal):
        from .identity import is_admin

        return is_admin(principal, self.repo, self.settings)

    def _state_for_owner(self, owner):
        # Internal read projection: caller must authorize the original actor.
        now = self.repo.clock()
        result = []
        with self.repo.engine.connect() as connection:
            for node in connection.execute(
                select(operator_nodes).order_by(operator_nodes.c.updated_at.desc())
            ).mappings():
                payload = node["payload"]
                if (
                    payload.get("capacity_backend") != BACKEND_MARKER
                    or payload.get("tenant_id") != self.settings.tenant_id
                    or payload.get("owner_id") != owner
                ):
                    continue
                binding = payload["dstack"]
                observed = binding.get("observed_at")
                fresh = _finite(observed) and 0 <= now - observed <= self.policy.get(
                    "observation_fresh_seconds", 30
                )
                intent = (
                    connection.execute(
                        select(instance_intents).where(
                            instance_intents.c.id == node["intent_id"]
                        )
                    )
                    .mappings()
                    .one()
                )
                native_ready = bool(
                    fresh
                    and binding.get("ready") is True
                    and node["desired_state"] == "running"
                    and window_open(intent, node, now)
                )
                workers = (
                    list(
                        connection.execute(
                            select(registered_workers).where(
                                registered_workers.c.provider == intent["provider"],
                                registered_workers.c.instance_id
                                == intent["provider_instance_id"],
                            )
                        ).mappings()
                    )
                    if intent["provider_instance_id"]
                    else []
                )
                from .worker_admission import worker_window_reason

                expected = payload.get("lifecycle", {}).get("expected_runtime_s")
                eligible = [
                    worker
                    for worker in workers
                    if worker["state"] == "ready"
                    and not worker["drain_requested"]
                    and not worker["current_job_id"]
                    and worker["expires_at"] > now
                    and worker_window_reason(
                        connection,
                        worker,
                        now,
                        deployment_profile_id=payload["selection"][
                            "runtime_profile_id"
                        ],
                        expected_runtime_s=expected,
                    )
                    is None
                ]
                ready = bool(native_ready and eligible)
                dispatch_reason = None
                registration = binding.get("registration_observation", {})
                registration_time = registration.get("observed_at")
                registration_reason = (
                    registration.get("reason_code")
                    if _finite(registration_time)
                    and 0
                    <= now - registration_time
                    <= self.policy.get("observation_fresh_seconds", 30)
                    and registration.get("reason_code") in REGISTRATION_REASON_CODES
                    else None
                )
                if native_ready and not ready:
                    dispatch_reason = registration_reason or next(
                        (
                            reason
                            for worker in workers
                            if (
                                reason := worker_window_reason(
                                    connection,
                                    worker,
                                    now,
                                    deployment_profile_id=payload["selection"][
                                        "runtime_profile_id"
                                    ],
                                    expected_runtime_s=expected,
                                )
                            )
                        ),
                        "matching_worker_unavailable",
                    )
                # Presentation only: unresolved stopped creates still retain their
                # original ledger/fences until allocation evidence is reconciled.
                review_required = (
                    node["desired_state"] == "stopped"
                    and node["runtime_state"] == "creation_unknown"
                )
                result.append(
                    {
                        "node_id": node["intent_id"],
                        "provider": payload["provider"],
                        "runtime_profile_id": payload["selection"][
                            "runtime_profile_id"
                        ],
                        "mode": payload["selection"]["mode"],
                        "desired_state": node["desired_state"],
                        "state": node["runtime_state"],
                        "record_group": "pending_review"
                        if review_required
                        else "current",
                        "review_required": review_required,
                        "ready": ready,
                        "native_ready": native_ready,
                        "slots_ready": len(eligible) if native_ready else 0,
                        "observation_fresh": bool(fresh),
                        "observed_at": observed,
                        "hard_deadline": payload["hard_deadline"],
                        "hold_until": payload["hold_until"],
                        "hourly_cost_microusd": payload["hourly_cost_microusd"],
                        "hourly_cost_basis": "approved_ceiling",
                        **(
                            {"lifecycle": copy.deepcopy(payload["lifecycle"])}
                            if "lifecycle" in payload
                            else {}
                        ),
                        "billing_state": binding.get("billing_state", "unsettled"),
                        "reason_code": payload.get("stop_reason_code")
                        or binding.get("reason_code")
                        or dispatch_reason,
                        "dispatch_reason_code": dispatch_reason,
                        "registration_reason_code": registration_reason,
                        "version": command_revision(payload),
                    }
                )
        return {
            "capacity_backend": BACKEND_MARKER,
            "enabled": self.policy.get("enabled") is True,
            "operator": {"account": owner},
            "nodes": result,
            "summary": {
                "ready": sum(node["ready"] for node in result),
                "starting": sum(
                    node["state"]
                    in {"reserved", "creating", "starting", "runtime_unconfirmed"}
                    for node in result
                ),
                "unknown": sum(
                    "unknown" in node["state"] or not node["observation_fresh"]
                    for node in result
                ),
            },
        }

    def node_command(self, principal, node_id, body, key, kind="stop"):
        require(
            kind == "stop"
            and isinstance(body, dict)
            and not set(body) - {"expected_version", "acknowledge_capacity_loss"}
            and ("expected_version" not in body or _finite(body["expected_version"]))
            and (
                "acknowledge_capacity_loss" not in body
                or isinstance(body["acknowledge_capacity_loss"], str)
            ),
            "dstack_node_command_invalid",
            422,
        )
        owner = self.authorize(principal)
        actor = _actor(self.settings.tenant_id, owner)
        digest = request_hash({"kind": "dstack_stop", "node_id": node_id, "body": body})
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node = self._owned_node(connection, principal, node_id, allow_admin=True)
            existing = self._existing_command(connection, actor, key, digest)
            if existing:
                return {
                    "command_id": existing["id"],
                    "node_id": node_id,
                    "state": existing["state"],
                }
            require(
                "expected_version" not in body
                or body["expected_version"] == command_revision(node["payload"]),
                "operator_node_version_conflict",
            )
            now = self.repo.clock()
            from .dstack_workspace import CapacityLoss, stop_impact

            impact = stop_impact(connection, self.settings, [node_id], now)
            if (
                impact["warnings"]
                and body.get("acknowledge_capacity_loss") != impact["fingerprint"]
            ):
                raise CapacityLoss(impact)
            payload = advance_command_revision(copy.deepcopy(node["payload"]))
            payload.update(stop_requested_at=now, hold_until=0)
            if payload.get("dstack_observation"):
                payload["dstack_observation"]["ready"] = False
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(
                    desired_state="stopped",
                    runtime_state="draining",
                    payload=payload,
                    updated_at=now,
                )
            )
            instance = (
                connection.execute(
                    select(instance_intents).where(instance_intents.c.id == node_id)
                )
                .mappings()
                .one()
            )
            if instance.get("provider_instance_id"):
                connection.execute(
                    update(registered_workers)
                    .where(
                        registered_workers.c.provider == instance["provider"],
                        registered_workers.c.instance_id
                        == instance["provider_instance_id"],
                        registered_workers.c.state != "retired",
                    )
                    .values(drain_requested=1, state="draining", updated_at=now)
                )
            command_id = str(uuid.uuid4())
            connection.execute(
                insert(operator_commands).values(
                    id=command_id,
                    actor=actor,
                    idempotency_key=key,
                    request_hash=digest,
                    kind="dstack_stop",
                    state="accepted",
                    payload={"node_id": node_id},
                    created_at=now,
                    updated_at=now,
                )
            )
            self.repo._emit(
                connection,
                "dstack.stop_requested",
                node_id,
                {"intent_id": node_id, "command_id": command_id},
            )
        return {"command_id": command_id, "node_id": node_id, "state": "accepted"}

    def set_hold(self, principal, node_id, body, key):
        require(
            isinstance(body, dict)
            and set(body)
            <= {"hold_seconds", "expected_version", "keep_on", "idle_shutdown_seconds"}
            and ("expected_version" not in body or _finite(body["expected_version"]))
            and any(
                field in body
                for field in ("hold_seconds", "keep_on", "idle_shutdown_seconds")
            ),
            "dstack_hold_invalid",
            422,
        )
        owner = self.authorize(principal)
        actor = _actor(self.settings.tenant_id, owner)
        digest = request_hash({"kind": "dstack_hold", "node_id": node_id, "body": body})
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node = self._owned_node(connection, principal, node_id)
            existing = self._existing_command(connection, actor, key, digest)
            if existing:
                return copy.deepcopy(existing["payload"])
            require(
                "expected_version" not in body
                or body["expected_version"] == command_revision(node["payload"]),
                "operator_node_version_conflict",
            )
            now = self.repo.clock()
            payload = advance_command_revision(copy.deepcopy(node["payload"]))
            intent = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == node_id),
            )
            require(
                node["desired_state"] == "running" and window_open(intent, node, now),
                "dstack_hold_window_expired",
            )
            if payload.get("lifecycle", {}).get("version") == 2:
                original_policy = payload["lifecycle"]
                options = {
                    "idle_shutdown_seconds": original_policy["idle_shutdown_seconds"],
                    **body,
                }
                # Alter only the shutdown preference; frozen accounts and the
                # protected execution quote are not client-editable.
                candidate = self._lifecycle(
                    options,
                    now,
                    original_policy["expected_runtime_s"],
                    original_guard=original_policy.get("test_gpu_guard"),
                )
                if "keep_on" not in body and "hold_seconds" not in body:
                    candidate["keep_on"] = original_policy["keep_on"]
                    candidate["hold_until"] = original_policy["hold_until"]
                for field in ("reservation_seconds", "budget_account_ids"):
                    candidate[field] = copy.deepcopy(original_policy[field])
                if "test_gpu_guard" in original_policy:
                    candidate["test_gpu_guard"] = copy.deepcopy(
                        original_policy["test_gpu_guard"]
                    )
                payload.update(lifecycle=candidate, hold_until=candidate["hold_until"])
                connection.execute(
                    update(operator_nodes)
                    .where(operator_nodes.c.intent_id == node_id)
                    .values(payload=payload, updated_at=now)
                )
                funded = self.fund_window(
                    connection, intent, {**node, "payload": payload}, now
                )
                result = {
                    "node_id": node_id,
                    "hold_until": candidate["hold_until"],
                    "hard_deadline": None,
                    "lifecycle": copy.deepcopy(candidate),
                    "reserved_cost_microusd": funded,
                    "additional_reservation_microusd": funded
                    - intent["reserved_cost_microusd"],
                    "scope": "continuing_idle_policy_with_original_spend_cap",
                }
            else:
                require(
                    not set(body) - {"hold_seconds", "expected_version"}
                    and _integer(body.get("hold_seconds"), 0, 86400),
                    "dstack_hold_invalid",
                    422,
                )
                until = now + body["hold_seconds"] if body["hold_seconds"] else 0
                require(
                    until <= payload["hard_deadline"], "dstack_hold_deadline_exceeded"
                )
                payload["hold_until"] = until
                connection.execute(
                    update(operator_nodes)
                    .where(operator_nodes.c.intent_id == node_id)
                    .values(payload=payload, updated_at=now)
                )
                result = {
                    "node_id": node_id,
                    "hold_until": until,
                    "hard_deadline": payload["hard_deadline"],
                    "additional_reservation_microusd": 0,
                    "scope": "idle_hold_within_original_deadline",
                }
            connection.execute(
                insert(operator_commands).values(
                    id=str(uuid.uuid4()),
                    actor=actor,
                    idempotency_key=key,
                    request_hash=digest,
                    kind="dstack_hold",
                    state="completed",
                    payload=result,
                    created_at=self.repo.clock(),
                    updated_at=self.repo.clock(),
                )
            )
        return result


def from_environment(repo, settings, *, readiness=None):
    """Protected explicit config only; no provider I/O, DDL or budget widening."""
    filename = os.environ.get("DSTACK_OPERATOR_CONFIG", "")
    if not filename:
        return DstackOperator(repo, settings)
    try:
        config = json.loads(_protected_read(filename))
        require(
            isinstance(config, dict) and config.get("version") == 1,
            "dstack_config_invalid",
            422,
        )
        if config.get("policy", {}).get("enabled") is not True:
            return DstackOperator(repo, settings)
        token = _protected_read(config.pop("token_file"), maximum=4096).strip()
        ssh = _protected_read(config.pop("ssh_public_key_file"), maximum=16384).strip()
        client = DstackClient(config["endpoint"], token, config["project"])
        config["ssh_public_key"] = ssh
        store = LedgerDstackStore(
            repo,
            settings.tenant_id,
            observation_fresh_seconds=config["policy"]["observation_fresh_seconds"],
        )
        capacity = DstackCapacity(client, store, clock=repo.clock, readiness=readiness)
        return DstackOperator(repo, settings, capacity, config)
    except (KeyError, TypeError, json.JSONDecodeError):
        raise OperatorError("dstack_config_invalid", 422) from None


def create_capacity():
    """No-argument factory for the pinned capacity CLI; use original runtime DB."""
    from .repository import Repository
    from .settings import Settings

    settings = Settings.from_environment()
    repo = Repository(settings.database_url)
    service = from_environment(repo, settings)
    require(service.capacity is not None, "dstack_capacity_disabled")
    return service.capacity
