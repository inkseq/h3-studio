"""dstack application commands backed exclusively by the original capacity ledger.

Constructing the service does not contact providers, initialize budgets or change
capacity ceilings. HTTP callers select trusted, pinned deployments, not images,
credentials or arbitrary resources. The existing CPU controller supplies native
readiness; a dstack RUNNING task alone never authorizes generation.
"""
from __future__ import annotations

from dataclasses import asdict
import copy
import json
import math
import os
from pathlib import Path
import stat
import uuid

from sqlalchemy import insert, select, update

from .dstack_capacity import CapacityRequest, DstackCapacity, DstackClient, DstackError, live_capacity_usage
from .operator_capacity import (OperatorError, operator_commands, operator_nodes,
                               operator_previews, require, safe_id)
from .repository import (Scope, BudgetExceeded, attempts, budget_accounts, budget_reservations, instance_intents,
                         jobs, manually_reviewed_inactive, registered_workers, registered_devices, request_hash, scaler_actions,
                         scaler_receipts)
from .runtime_catalog import engine_manifest, get_profile

BACKEND_MARKER = "dstack-v1"
PROVIDERS = {"vastai": "vast", "runpod": "runpod"}
TERMINAL_JOBS = {"succeeded", "failed", "cancelled"}
OBSERVATION_KEYS = {"run_id", "run_name", "state", "ready", "reason_code", "observed_at",
    "provider_instance_id", "runtime_incarnation", "dstack_status", "model_load_state",
    "billing_state", "estimated_cost_microusd", "bootstrap_phase"}
IDENTITY_KEYS = {"run_id", "provider_instance_id", "runtime_incarnation"}


def _actor(tenant, owner):
    return "dstack:" + request_hash({"tenant_id": tenant, "owner_id": owner})


def _integer(value, minimum=1, maximum=9_000_000_000_000):
    return type(value) is int and minimum <= value <= maximum


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _window_open(intent, node, now):
    deadline = intent.get("hard_deadline")
    if deadline is not None:
        return _finite(deadline) and now < deadline
    from .worker_admission import dstack_policy_reason
    return dstack_policy_reason(intent, node, now) is None


def _cold_unadmitted_runtime(binding, workers, instance_id, now, fresh_seconds):
    """No platform execution ever acquired this exact cold preparation.

    Worker rows and the first-ready/incarnation latches are retained history,
    not just current availability. Unknown previously admitted execution must
    never use this exception. The caller still checks ownership and stop intent.
    """
    observed = binding.get("observed_at")
    return (not workers and bool(instance_id)
        and binding.get("provider_instance_id") == instance_id
        and binding.get("state") in {"starting", "runtime_unconfirmed"}
        and binding.get("runtime_incarnation") is None
        and binding.get("first_runtime_ready_at") is None
        and _finite(observed) and 0 <= now-observed <= fresh_seconds)


def plan_offers(plan, request):
    """Project pinned 0.22.3 offers; no backend_data, SSH or user/job payload."""
    require(isinstance(plan,dict) and isinstance(plan.get("job_plans"),list)
        and len(plan["job_plans"])==1, "dstack_plan_unconfirmed")
    offered=plan["job_plans"][0].get("offers")
    require(isinstance(offered,list),"dstack_plan_unconfirmed")
    normalize=lambda text:"".join(c.lower() for c in text if c.isalnum())
    result=[]
    for item in offered:
        if not isinstance(item,dict) or item.get("backend")!=request.backend:
            continue
        instance=item.get("instance",{})
        resources=instance.get("resources",{})
        gpus=resources.get("gpus")
        price=item.get("price")
        memory=resources.get("memory_mib"); disk=resources.get("disk",{}).get("size_mib")
        cpus=resources.get("cpus")
        availability=item.get("availability")
        if (not isinstance(gpus,list) or len(gpus)!=1 or not isinstance(gpus[0],dict)
                or not isinstance(gpus[0].get("name"),str)
                or normalize(gpus[0]["name"]) not in {normalize(name) for name in request.gpu_names}
                or not all(_finite(v) and v>0 for v in (memory,disk,cpus,gpus[0].get("memory_mib")))
                or memory<request.memory_gib*1024 or disk<request.disk_gib*1024
                or cpus<request.cpu_count or gpus[0]["memory_mib"]<request.gpu_memory_gib*1024
                or not _finite(price) or price<=0 or math.ceil(price*1_000_000)>request.max_price_microusd
                or availability not in {"available","idle","unknown"}):
            continue
        result.append({"backend":request.backend,"region":item.get("region"),
            "gpu_type":gpus[0]["name"],"gpu_count":1,"memory_gib":memory/1024,
            "disk_gib":disk/1024,"gpu_memory_gib":gpus[0]["memory_mib"]/1024,"cpu_count":cpus,
            "hourly_cost_microusd":math.ceil(price*1_000_000),"availability":availability,
            "inventory_confirmed":availability in {"available","idle"}})
    return sorted(result,key=lambda row:(not row["inventory_confirmed"],row["hourly_cost_microusd"]))[:100]


def _request(document):
    document = copy.deepcopy(document)
    for key in ("gpu_names", "regions"):
        if key in document:
            document[key] = tuple(document[key])
    return CapacityRequest(**document)


def _protected_read(filename, *, maximum=262144):
    path = Path(filename)
    require(path.is_absolute(), "dstack_config_path_invalid", 422)
    try:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and (os.name == "nt" or
                not info.st_mode & (stat.S_IRWXO | stat.S_IWGRP)), "dstack_config_permissions_invalid", 422)
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

    def _node(self, connection, intent_id):
        node = self.repo._locked(connection, select(operator_nodes).where(operator_nodes.c.intent_id == intent_id))
        require(node is not None and node["payload"].get("capacity_backend") == BACKEND_MARKER
            and node["payload"].get("tenant_id") == self.tenant_id, "dstack_node_not_found", 404)
        intent = self.repo._locked(connection, select(instance_intents).where(instance_intents.c.id == intent_id))
        require(intent is not None and intent["provider"] in PROVIDERS.values(), "dstack_intent_binding_invalid")
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
            require(stored.get("spec_digest") == binding["spec_digest"] and
                all(stored.get(key) == value for key, value in binding.items()), "dstack_intent_binding_changed")
            action = self.repo._locked(connection, select(scaler_actions).where(scaler_actions.c.intent_id == intent_id))
            if (stored.get("apply_started") or node["desired_state"] != "running"
                    or action is None or action["create_started_at"] is not None):
                return False
            require(intent["state"] == "reserved" and _window_open(intent, node, self.repo.clock()),
                "dstack_start_window_expired")
            reservations = list(connection.execute(select(budget_reservations).where(
                budget_reservations.c.reference_type == "instance",
                budget_reservations.c.reference_id == intent_id)).mappings())
            require(reservations and all(row["state"] == "reserved" and
                row["amount_microusd"] == intent["reserved_cost_microusd"] for row in reservations),
                "dstack_budget_reservation_missing")
            now = self.repo.clock()
            stored.update(apply_started=True, apply_started_at=now)
            payload["dstack"] = stored
            self.repo.update_instance(intent_id, "creating", connection=connection)
            connection.execute(update(scaler_actions).where(scaler_actions.c.intent_id == intent_id)
                .values(create_started_at=now))
            self._save(connection, node, payload, runtime_state="creating")
            self._receipt(connection, intent_id, "create", {"state": "journaled", "run_name": binding["run_name"]})
            return True

    def _save(self, connection, node, payload, *, runtime_state=None):
        values = {"payload": payload, "updated_at": self.repo.clock()}
        if runtime_state is not None:
            values["runtime_state"] = runtime_state
        connection.execute(update(operator_nodes).where(operator_nodes.c.intent_id == node["intent_id"]).values(**values))

    def _receipt(self, connection, intent_id, operation, facts):
        connection.execute(insert(scaler_receipts).values(id=str(uuid.uuid4()), intent_id=intent_id,
            operation=operation, observed_at=self.repo.clock(), facts=facts))

    def _advance(self, connection, intent, observation):
        """Use the original state machine; uncertainty never settles invoices."""
        identity = observation.get("provider_instance_id") or intent.get("provider_instance_id")
        current = intent["state"]
        if identity and not intent.get("provider_instance_id"):
            connection.execute(update(instance_intents).where(instance_intents.c.id == intent["id"])
                .values(provider_instance_id=identity, updated_at=self.repo.clock()))
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
            self.repo.update_instance(intent["id"], state, provider_instance_id=identity, connection=connection)

    def record(self, intent_id, observation):
        require(isinstance(observation, dict) and not set(observation) - OBSERVATION_KEYS,
            "dstack_observation_invalid", 422)
        require(_finite(observation.get("observed_at")) and
            0 <= observation["observed_at"] <= self.repo.clock(), "dstack_observation_time_invalid", 422)
        require("bootstrap_phase" not in observation or observation["bootstrap_phase"] in {
            "provisioning", "model_download", "runtime_start", "model_load", "runtime_ready"},
            "dstack_observation_phase_invalid", 422)
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self._node(connection, intent_id)
            payload = copy.deepcopy(node["payload"])
            binding = payload["dstack"]
            if observation["observed_at"] < binding.get("observed_at", 0):
                return
            for key in IDENTITY_KEYS:
                require(not observation.get(key) or not binding.get(key) or observation[key] == binding[key],
                    "dstack_observation_identity_changed")
            if observation.get("run_name"):
                require(observation["run_name"] == binding["run_name"], "dstack_observation_identity_changed")
            value = copy.deepcopy(observation)
            # A stale error, stop intent or expired window cannot leave a ready projection.
            value["ready"] = bool(value.get("ready") is True and value.get("state") == "ready"
                and node["desired_state"] == "running"
                and _window_open(intent, node, self.repo.clock()))
            if value.get("runtime_incarnation") and value.get("state") in {"ready", "busy"}:
                binding.setdefault("first_runtime_ready_at", value["observed_at"])
            if value.get("state") == "stopped":
                # Preserve the first terminal observation in the original row;
                # later polls must not extend ended runtime/cost estimates.
                binding.setdefault("first_stopped_at", value["observed_at"])
            binding.update(value)
            payload["dstack"] = binding
            payload["dstack_observation"] = {"run_id": binding.get("run_id"),
                "instance_id": binding.get("provider_instance_id"), "observed_at": value["observed_at"],
                "status": value.get("dstack_status", "unknown"), "ready": value["ready"],
                "runtime_incarnation": binding.get("runtime_incarnation"),
                "reason_code": value.get("reason_code"), "manifest_digest": binding["manifest_digest"]}
            self._advance(connection, intent, value)
            self._save(connection, node, payload, runtime_state=value.get("state", "observation_unknown"))
            connection.execute(update(scaler_actions).where(scaler_actions.c.intent_id == intent_id)
                .values(last_observation=value, last_observed_at=value["observed_at"]))

    def record_broker(self, intent_id, worker_id, observation):
        """Separate transport eligibility from native/provider readiness."""
        require(isinstance(observation,dict) and set(observation) <= {
            "ready","observed_at","heartbeat_at","reason_code","worker_id"},
            "dstack_broker_observation_invalid")
        require(_finite(observation.get("observed_at")) and
            0 <= observation["observed_at"] <= self.repo.clock() and
            observation.get("worker_id") == worker_id, "dstack_broker_observation_invalid")
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node,intent=self._node(connection,intent_id)
            payload=copy.deepcopy(node["payload"])
            binding=payload["dstack"]
            prior=binding.get("broker_observation",{})
            if prior.get("observed_at",0)>observation["observed_at"]:
                return
            worker=connection.execute(select(registered_workers).where(registered_workers.c.id==worker_id)).mappings().one_or_none()
            require(worker is not None and worker["instance_id"]==intent["provider_instance_id"]
                and worker["provider"]==intent["provider"]
                and worker["spec"].get("dispatch_backend")=="hatchet-v1"
                and worker["spec"].get("engine_manifest_digest")==binding["manifest_digest"]
                and worker["spec"].get("configuration_id")==binding["configuration_id"],
                "dstack_broker_binding_changed")
            value=copy.deepcopy(observation)
            value["ready"]=bool(value.get("ready") is True and node["desired_state"]=="running"
                and _window_open(intent, node, self.repo.clock()))
            binding["broker_observation"]=value
            self._save(connection,node,payload)

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
                if (not binding.get("stop_started") or binding.get("stop_attempt_count", 0) >= 5
                        or now < binding.get("stop_next_retry_at", float("inf"))):
                    return False
                if (binding.get("dstack_status") != "running" or not _finite(observed)
                        or not 0 <= now-observed <= self.observation_fresh_seconds
                        or binding.get("provider_instance_id") != intent.get("provider_instance_id")):
                    return False
            elif binding.get("stop_started"):
                return False
            workers = list(connection.execute(select(registered_workers).where(
                registered_workers.c.provider == intent["provider"],
                registered_workers.c.instance_id == intent["provider_instance_id"])).mappings()) if intent.get("provider_instance_id") else []
            worker_ids = [worker["id"] for worker in workers]
            if workers and any(worker["state"] != "retired" or worker["current_job_id"] for worker in workers):
                return False
            if worker_ids:
                relevant = jobs.outerjoin(attempts, jobs.c.id == attempts.c.job_id)
                for row in connection.execute(select(jobs.c.status, attempts.c.submission_started_at,
                        attempts.c.upstream_stopped).select_from(relevant).where(
                    (jobs.c.lease_worker_id.in_(worker_ids)) | (attempts.c.worker_id.in_(worker_ids)))).mappings():
                    if row["status"] not in TERMINAL_JOBS or row["submission_started_at"] is not None and row["upstream_stopped"] != 1:
                        return False
            # An owned cold runtime which has never admitted a worker cannot
            # have business inference to drain. Model download may already be
            # running; waiting for ready/idle would make that preparation
            # impossible to cancel. A previously qualified runtime still needs
            # exact fresh native idle proof, even if its worker is retired.
            launch_started = binding.get("bootstrap_launch_started", binding.get("bootstrap_started"))
            cold_unadmitted = _cold_unadmitted_runtime(binding, workers, intent.get("provider_instance_id"),
                now, self.observation_fresh_seconds)
            if not retry and not cold_unadmitted and (launch_started or workers
                    or binding.get("runtime_incarnation") is not None
                    or binding.get("first_runtime_ready_at") is not None):
                observed = binding.get("observed_at")
                if not (binding.get("state") == "ready" and binding.get("runtime_incarnation")
                        and _finite(observed) and 0 <= self.repo.clock() - observed <= self.observation_fresh_seconds):
                    return False
            count = binding.get("stop_attempt_count", 0) + 1
            binding.update(stop_started=True, stop_attempt_count=count, stop_last_attempt_at=now,
                stop_next_retry_at=now + min(30 * 2**(count-1), 300))
            binding.setdefault("stop_started_at", now)
            payload["dstack"] = binding
            connection.execute(update(scaler_actions).where(scaler_actions.c.intent_id == intent_id)
                .values(destroy_started_at=now))
            self._save(connection, node, payload, runtime_state="stopping")
            self._receipt(connection, intent_id, "destroy", {"state": "retry_journaled" if retry else "journaled",
                "run_id": run_id, "attempt_count": count})
            return True

    def authorize_bootstrap_launch(self, intent_id, run_id, instance_id):
        """Recheck authority for immutable uploads which have not launched yet."""
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self._node(connection, intent_id)
            binding = node["payload"]["dstack"]
            require(binding.get("bootstrap_started") and binding.get("run_id") == run_id
                and binding.get("provider_instance_id") == instance_id,
                "dstack_bootstrap_identity_unconfirmed")
            require(node["desired_state"] == "running" and _window_open(intent, node, self.repo.clock()),
                "dstack_bootstrap_window_expired")
            return True

    def begin_runtime_launch(self, intent_id, run_id, instance_id):
        """Durable launch journal; a response gap cannot run setup twice."""
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self._node(connection, intent_id)
            payload = copy.deepcopy(node["payload"])
            binding = payload["dstack"]
            require(binding.get("bootstrap_started") and binding.get("run_id") == run_id
                and binding.get("provider_instance_id") == instance_id,
                "dstack_bootstrap_identity_unconfirmed")
            require(node["desired_state"] == "running" and _window_open(intent, node, self.repo.clock()),
                "dstack_bootstrap_window_expired")
            if binding.get("bootstrap_launch_started", True):
                return False
            binding.update(bootstrap_launch_started=True, bootstrap_launch_started_at=self.repo.clock())
            self._save(connection, node, payload)
            self._receipt(connection, intent_id, "bootstrap", {"state": "launch_journaled", "run_id": run_id})
            return True

    def begin_bootstrap(self, intent_id, run_id, instance_id):
        """CPU must commit this before the existing one-shot remote bootstrap."""
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self._node(connection, intent_id)
            payload = copy.deepcopy(node["payload"])
            binding = payload["dstack"]
            require(binding.get("apply_started") and binding.get("run_id") == run_id and
                binding.get("provider_instance_id") == instance_id and instance_id,
                "dstack_bootstrap_identity_unconfirmed")
            if binding.get("bootstrap_started"):
                return False
            require(node["desired_state"] == "running" and _window_open(intent, node, self.repo.clock()),
                "dstack_bootstrap_window_expired")
            binding.update(bootstrap_started=True, bootstrap_started_at=self.repo.clock(), bootstrap_launch_started=False)
            payload["dstack"] = binding
            self._save(connection, node, payload)
            self._receipt(connection, intent_id, "bootstrap", {"state": "journaled", "run_id": run_id,
                "instance_id": instance_id})
            return True

    def cancel_unapplied(self, intent_id):
        """Release only an original intent atomically proven never sent to dstack."""
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node, intent = self._node(connection, intent_id)
            action = self.repo._locked(connection, select(scaler_actions).where(scaler_actions.c.intent_id == intent_id))
            binding = node["payload"]["dstack"]
            if (node["desired_state"] != "stopped" or intent["state"] != "reserved" or
                binding.get("apply_started") or action is None or action["create_started_at"] is not None):
                return False
            self.repo.update_instance(intent_id, "destroyed", destruction_confirmed=True,
                actual_cost_microusd=0, connection=connection)
            payload = copy.deepcopy(node["payload"])
            payload["dstack"].update(state="stopped", ready=False, billing_state="no_charge_confirmed",
                reason_code="dstack_never_applied", observed_at=self.repo.clock())
            self._save(connection, node, payload, runtime_state="stopped")
            self._receipt(connection, intent_id, "cancel", {"state": "never_applied", "actual_cost_microusd": 0})
            return True


class DstackOperator:
    """Small owner-scoped application service; mount alongside existing operator API."""
    def __init__(self, repo, settings, capacity=None, profile_config=None, *, provider_inventory=None):
        self.repo, self.settings, self.capacity = repo, settings, capacity
        self.provider_inventory = provider_inventory or getattr(getattr(capacity, "client", None), "provider_inventory", None)
        self.config = copy.deepcopy(profile_config or {"version": 1, "policy": {"enabled": False}, "profiles": []})
        require(self.config.get("version") == 1, "dstack_config_version_invalid", 422)
        self.policy = self.config.get("policy", {})
        self.profiles = {}
        if self.policy.get("enabled"):
            require(capacity is not None and type(self.policy["enabled"]) is bool, "dstack_adapter_unavailable")
            for name, maximum in (("max_instances", 128), ("max_physical_gpus", 128),
                    ("max_hourly_cost_microusd", 9_000_000_000_000), ("max_ttl_seconds", 86400),
                    ("idle_shutdown_seconds", 86400), ("observation_fresh_seconds", 120)):
                require(_integer(self.policy.get(name), maximum=maximum), "dstack_policy_limits_invalid", 422)
            require(_finite(self.policy.get("expires_at")), "dstack_policy_window_invalid", 422)
            require(type(self.policy.get("lifecycle_version", 1)) is int and
                self.policy.get("lifecycle_version", 1) in {1, 2}, "dstack_lifecycle_version_invalid", 422)
            if self.policy.get("lifecycle_version") == 2:
                require(_integer(self.policy.get("reservation_seconds"), 120, 86400),
                    "dstack_reservation_window_invalid", 422)
        for value in self.config.get("profiles", []):
            profile = self._validate_profile(value)
            require(profile["id"] not in self.profiles, "dstack_profile_duplicate", 422)
            self.profiles[profile["id"]] = profile
        self.fingerprint = request_hash(self.config)

    def _validate_profile(self, value):
        require(isinstance(value, dict), "dstack_profile_invalid", 422)
        profile = copy.deepcopy(value)
        required = {"id", "owner_id", "project_id", "pool", "backend", "runtime_profile_id", "mode",
            "configuration_id", "image", "gpu_names", "memory_gib", "disk_gib", "gpu_memory_gib",
            "cpu_count", "max_price_microusd", "max_ttl_seconds", "extra_reservation_microusd", "budget_account_ids"}
        require(required <= set(profile) and not set(profile) - required - {"regions", "min_reliability"},
            "dstack_profile_fields_invalid", 422)
        require(all(safe_id(profile[key]) for key in ("id", "owner_id", "project_id", "pool", "configuration_id"))
            and profile["owner_id"] in getattr(self.settings, "operator_capacity_owners", ())
            and isinstance(profile["budget_account_ids"], list) and profile["budget_account_ids"]
            and len(profile["budget_account_ids"]) == len(set(profile["budget_account_ids"]))
            and all(safe_id(item) for item in profile["budget_account_ids"]), "dstack_profile_scope_invalid", 422)
        try:
            native = get_profile(profile["runtime_profile_id"])
            manifest = engine_manifest(profile["runtime_profile_id"], profile["mode"])
        except (KeyError, ValueError):
            raise OperatorError("dstack_native_profile_invalid", 422) from None
        floors = {"memory_gib": max(native.get("minimum_ram_bytes", 0),
            native["runtime"].get("minimum_available_ram_bytes", 0),
            native["runtime"].get("minimum_total_ram_bytes", 0)),
            "disk_gib": native.get("minimum_disk_bytes", 0),
            "gpu_memory_gib": max(native.get("minimum_free_vram_bytes", 0),
                native["runtime"].get("minimum_free_vram_bytes", 0),
                native.get("hardware_admission", {}).get("minimum_total_vram_bytes", 0))}
        require(all(_integer(profile.get(key)) and profile[key] * 2**30 >= floor
            for key, floor in floors.items()), "dstack_native_resource_floor_invalid", 422)
        normalize = lambda name: "".join(char.lower() for char in name if char.isalnum())
        allowed = {normalize(name) for name in native.get("hardware_admission", {}).get("gpu_models", native["gpu_models"])}
        require(isinstance(profile["gpu_names"], list) and profile["gpu_names"] and
            all(isinstance(name, str) and normalize(name) in allowed for name in profile["gpu_names"]),
            "dstack_native_gpu_binding_invalid", 422)
        require(_integer(profile["extra_reservation_microusd"], 0) and
            _integer(profile["max_ttl_seconds"], 120, 86400), "dstack_profile_limits_invalid", 422)
        profile.update(model_id=native["model_id"], manifest_digest=manifest.digest,
            recipe_id=manifest.document["generation_recipe_id"])
        # Exercise exact transport validation without constructing any provider request.
        self._capacity_request(profile, str(uuid.uuid4()), 1., 121., 120)
        return profile

    def authorize(self, principal):
        require(principal is not None, "operator_login_required", 401)
        require(not principal.machine and principal.owner in getattr(self.settings, "operator_capacity_owners", ()),
            "operator_forbidden", 403)
        return principal.owner

    def authorize_admin(self, principal):
        owner = self.authorize(principal)
        require(owner in getattr(self.settings, "operator_admin_owners", ()),
            "operator_admin_forbidden", 403)
        return owner

    def _enabled(self):
        require(self.policy.get("enabled") is True and self.capacity is not None, "dstack_capacity_disabled")
        require(self.repo.clock() < self.policy["expires_at"], "dstack_authority_expired")

    def _capacity_request(self, profile, intent_id, created_at, deadline, duration, *, lifecycle_version=1):
        return CapacityRequest(intent_id=intent_id, backend=profile["backend"], profile_id=profile["runtime_profile_id"],
            mode=profile["mode"], model_id=profile["model_id"], configuration_id=profile["configuration_id"],
            manifest_digest=profile["manifest_digest"], image=profile["image"], gpu_names=tuple(profile["gpu_names"]),
            memory_gib=profile["memory_gib"], disk_gib=profile["disk_gib"], gpu_memory_gib=profile["gpu_memory_gib"],
            cpu_count=profile["cpu_count"], max_price_microusd=profile["max_price_microusd"],
            created_at=created_at, hard_deadline=deadline, max_duration_s=duration,
            regions=tuple(profile.get("regions", [])), min_reliability=profile.get("min_reliability", .9),
            lifecycle_version=lifecycle_version)

    def _expected_runtime(self, profile):
        """Use the original protected execution quote, never an HTTP estimate."""
        from .execution_profiles import selected_policy
        from .execution_policy import reservation_for_duration
        try:
            policy = selected_policy(self.settings, profile_id=profile["runtime_profile_id"], recipe_id=profile["recipe_id"])
            now = self.repo.clock()
            require(policy and policy["enabled"] is True and policy["backend"] == "wangp-worker"
                and policy.get("dispatch_backend") == "hatchet-v1"
                and policy["pool"] == profile["pool"] and policy["configuration_id"] == profile["configuration_id"]
                and policy["model_id"] == profile["model_id"]
                and policy["engine_manifest_digest"] == profile["manifest_digest"]
                and policy["qualification"]["status"] in {"accepted", "runtime_required"}
                and policy["qualification"]["verified_at"] <= now
                and now < min(policy["qualification"]["expires_at"], policy["reservation"]["expires_at"]),
                "dstack_execution_policy_unconfirmed")
            duration = reservation_for_duration(policy, policy["envelope"]["max_duration_seconds"])["expected_runtime_s"]
            require(_finite(duration) and 0 < duration <= 86400, "dstack_execution_runtime_unconfirmed")
            return duration
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, OperatorError):
                raise
            raise OperatorError("dstack_execution_policy_unconfirmed") from None

    def _lifecycle(self, body, now, expected_runtime):
        keep = body.get("keep_on", "until" if body.get("hold_seconds", 0) else "idle")
        hold = body.get("hold_seconds", 0)
        idle = body.get("idle_shutdown_seconds", self.policy["idle_shutdown_seconds"])
        require(isinstance(keep, str) and keep in {"idle", "until", "until_stopped"}
            and _integer(hold, 0, 86400) and _integer(idle, 1, 86400)
            and (keep == "until" and hold > 0 or keep != "until" and hold == 0),
            "dstack_lifecycle_policy_invalid", 422)
        return {"version": 2, "keep_on": keep, "hold_until": now + hold if hold else 0,
            "idle_shutdown_seconds": idle, "reservation_seconds": self.policy["reservation_seconds"],
            "expected_runtime_s": expected_runtime}

    def fund_window(self, connection, intent, node, now):
        """Fund preparation, elapsed paid time and a full safe execution runway."""
        from .worker_admission import COMPLETION_MARGIN_SECONDS
        payload, lifecycle = node["payload"], node["payload"]["lifecycle"]
        require(_window_open(intent, node, now), "dstack_continuing_policy_unconfirmed")
        started = connection.execute(select(scaler_actions.c.create_started_at).where(
            scaler_actions.c.intent_id == intent["id"])).scalar_one_or_none()
        started = intent["created_at"] if started is None else started
        expected = lifecycle.get("expected_runtime_s")
        hourly, extra = payload.get("hourly_cost_microusd"), payload.get("extra_reservation_microusd", 0)
        require(_finite(started) and 0 <= now-started and _finite(expected) and 0 < expected <= 86400
            and _integer(hourly) and _integer(extra, 0), "dstack_funding_basis_unconfirmed")
        chunk = lifecycle["reservation_seconds"]
        paid = now-started + expected + max(lifecycle["idle_shutdown_seconds"], COMPLETION_MARGIN_SECONDS)
        # Admission requires strictly more funding than the complete runway.
        seconds = math.ceil((paid + 1) / chunk) * chunk + 300
        minimum = math.ceil(hourly * seconds / 3600) + extra
        return self.repo.fund_instance_window(connection, intent["id"], minimum, lifecycle["budget_account_ids"])

    def catalog(self, principal):
        owner = self.authorize(principal)
        fields = ("id", "runtime_profile_id", "model_id", "mode", "pool", "backend", "gpu_names",
            "memory_gib", "disk_gib", "gpu_memory_gib", "cpu_count", "max_price_microusd", "max_ttl_seconds")
        return {"capacity_backend": BACKEND_MARKER, "enabled": self.policy.get("enabled") is True,
            "lifecycle_version": self.policy.get("lifecycle_version", 1),
            "operator": {"account": owner},
            "profiles": [{key: copy.deepcopy(profile[key]) for key in fields}
                for profile in self.profiles.values() if profile["owner_id"] == owner]}

    def preview(self, principal, body):
        owner = self.authorize(principal)
        self._enabled()
        continuing = self.policy.get("lifecycle_version") == 2
        allowed = {"profile_id", "ttl_seconds", "keep_on", "hold_seconds", "idle_shutdown_seconds", "group_id"} if continuing else {"profile_id", "ttl_seconds", "group_id"}
        require(isinstance(body, dict) and "profile_id" in body and not set(body)-allowed
            and (continuing or {"profile_id", "ttl_seconds"} <= set(body)), "dstack_preview_invalid", 422)
        require(safe_id(body["profile_id"]), "dstack_preview_invalid", 422)
        profile = self.profiles.get(body["profile_id"])
        require(profile is not None and profile["owner_id"] == owner, "dstack_profile_not_found", 404)
        ttl = body.get("ttl_seconds", self.policy.get("reservation_seconds"))
        require(_integer(ttl, 120, 86400 if continuing else min(profile["max_ttl_seconds"], self.policy["max_ttl_seconds"])),
            "dstack_runtime_limit_invalid", 422)
        now = self.repo.clock()
        if not continuing:
            require(now + ttl <= self.policy["expires_at"], "dstack_authority_window_exceeded")
        lifecycle = self._lifecycle(body, now, self._expected_runtime(profile)) if continuing else None
        if continuing:
            lifecycle["budget_account_ids"] = copy.deepcopy(profile["budget_account_ids"])
        preview_id = str(uuid.uuid4())
        request = self._capacity_request(profile, preview_id, now, None if continuing else now + ttl,
            None if continuing else ttl, lifecycle_version=2 if continuing else 1)
        offers=plan_offers(self.capacity.plan(request, self.config["ssh_public_key"]),request)
        require(bool(offers),"dstack_no_compatible_offers")
        # Reserve preparation and a complete job from the first paid request,
        # plus shutdown grace; continuing funding has no absolute lifetime.
        initial_seconds = lifecycle["reservation_seconds"] if continuing else ttl
        if continuing:
            from .worker_admission import COMPLETION_MARGIN_SECONDS
            initial_seconds = math.ceil((lifecycle["expected_runtime_s"] +
                max(lifecycle["idle_shutdown_seconds"], COMPLETION_MARGIN_SECONDS) + 1) /
                initial_seconds) * initial_seconds
        cost = math.ceil(profile["max_price_microusd"] * (initial_seconds + 300) / 3600) + profile["extra_reservation_microusd"]
        payload = {"capacity_backend": BACKEND_MARKER, "tenant_id": self.settings.tenant_id, "owner_id": owner,
            "profile_id": profile["id"], "profile": profile, "config_fingerprint": self.fingerprint,
            "request": asdict(request), "reservation_microusd": cost}
        if continuing:
            payload["lifecycle"] = lifecycle
        expires = min(now + 120, self.policy["expires_at"], request.hard_deadline or self.policy["expires_at"])
        with self.repo.transaction() as connection:
            from .dstack_workspace import owned_group
            group_id = body.get("group_id")
            if group_id is not None:
                owned_group(connection, self.settings, owner, group_id)
            payload["group_id"] = group_id
            connection.execute(insert(operator_previews).values(id=preview_id, actor=_actor(self.settings.tenant_id, owner),
                payload=payload, created_at=now, expires_at=expires))
        return {"preview_id": preview_id, "expires_at": expires,
            "hard_deadline": request.hard_deadline, "reservation_microusd": cost,
            "hourly_cost_microusd": profile["max_price_microusd"], "price_basis": "approved_ceiling",
            "offer_status": "advisory", "offers": offers, "can_start": True, "capacity_backend": BACKEND_MARKER,
            **({"lifecycle": copy.deepcopy(lifecycle), "initial_reservation_seconds": initial_seconds}
               if continuing else {})}

    def _existing_command(self, connection, actor, key, digest):
        require(safe_id(key), "operator_idempotency_key_invalid", 422)
        command = self.repo._locked(connection, select(operator_commands).where(
            operator_commands.c.actor == actor, operator_commands.c.idempotency_key == key))
        if command:
            require(command["request_hash"] == digest, "operator_idempotency_conflict")
        return dict(command) if command else None

    def _limits(self, connection, hourly):
        # Legacy invoices/intents remain in the ledger, but are not current
        # dstack capacity. Only exact owned new-path allocations count here.
        nodes = [dict(node) for node in connection.execute(select(operator_nodes)).mappings()
            if node["payload"].get("capacity_backend") == BACKEND_MARKER]
        usage = live_capacity_usage(self.capacity.client, nodes, provider_inventory=self.provider_inventory,
            clock=self.repo.clock, fresh_seconds=self.policy["observation_fresh_seconds"])
        require(usage["instances"] + 1 <= self.policy["max_instances"] and
            usage["physical_gpus"] + 1 <= self.policy["max_physical_gpus"], "dstack_capacity_limit_exceeded")
        require(usage["hourly_cost_microusd"] + hourly <= self.policy["max_hourly_cost_microusd"], "dstack_hourly_limit_exceeded")
        return usage

    def start(self, principal, body, key):
        owner = self.authorize(principal)
        require(isinstance(body, dict) and set(body) == {"preview_id"} and safe_id(body["preview_id"]),
            "dstack_start_invalid", 422)
        actor = _actor(self.settings.tenant_id, owner)
        digest = request_hash({"kind": "dstack_start", "body": body})
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            existing = self._existing_command(connection, actor, key, digest)
            if existing:
                return {"command_id": existing["id"], "node_id": existing["payload"]["node_id"], "state": existing["state"]}
            self._enabled()
            preview = self.repo._locked(connection, select(operator_previews).where(operator_previews.c.id == body["preview_id"],
                operator_previews.c.actor == actor))
            require(preview is not None and self.repo.clock() < preview["expires_at"], "dstack_preview_expired")
            value = preview["payload"]
            require(value.get("config_fingerprint") == self.fingerprint and value.get("capacity_backend") == BACKEND_MARKER,
                "dstack_preview_configuration_changed")
            # A preview is single-use even when another idempotency key is supplied.
            previous = connection.execute(select(operator_commands.c.id).where(operator_commands.c.actor == actor,
                operator_commands.c.kind == "dstack_start",
                operator_commands.c.payload["preview_id"].as_string() == body["preview_id"])).first()
            require(previous is None, "dstack_preview_consumed")
            profile = value["profile"]
            if value.get("group_id") is not None:
                from .dstack_workspace import owned_group
                owned_group(connection, self.settings, owner, value["group_id"])
            usage = self._limits(connection, profile["max_price_microusd"])
            now = self.repo.clock()
            original = value["request"]
            continuing = original.get("lifecycle_version", 1) == 2
            if continuing:
                # A tenant/global account alone cannot authorize unlimited
                # keep-on for a person. Require their original scoped cap.
                accounts = list(connection.execute(select(budget_accounts).where(
                    budget_accounts.c.id.in_(profile["budget_account_ids"]))).mappings())
                require(any(account["tenant_id"] == self.settings.tenant_id and account["owner_id"] == owner
                    and account["project_id"] in (None, profile["project_id"]) for account in accounts),
                    "dstack_owner_spend_limit_required")
                require(self._expected_runtime(profile) == value["lifecycle"]["expected_runtime_s"],
                    "dstack_execution_policy_changed")
            scope = Scope(self.settings.tenant_id, owner, profile["project_id"], principal.actor_id)
            intent = self.repo.reserve_instance_intent(scope, profile["pool"], "dstack:" + body["preview_id"],
                physical_gpus=1, slots=1, reserved_cost_microusd=value["reservation_microusd"],
                hard_deadline=original["hard_deadline"], budget_account_ids=profile["budget_account_ids"],
                dry_run=False, provider=PROVIDERS[profile["backend"]], connection=connection,
                continuing=continuing, capacity_usage=usage, active_instance_ids=usage["active_instance_ids"])
            request = self._capacity_request(profile, intent["id"], now, original["hard_deadline"], original["max_duration_s"],
                lifecycle_version=original.get("lifecycle_version", 1))
            binding = request.binding(self.config["ssh_public_key"])
            command_id = str(uuid.uuid4())
            command_payload = {"preview_id": body["preview_id"], "node_id": intent["id"], "capacity_backend": BACKEND_MARKER}
            connection.execute(insert(operator_commands).values(id=command_id, actor=actor, idempotency_key=key,
                request_hash=digest, kind="dstack_start", state="accepted", payload=command_payload,
                created_at=now, updated_at=now))
            payload = {"capacity_backend": BACKEND_MARKER, "tenant_id": self.settings.tenant_id, "owner_id": owner,
                "project_id": profile["project_id"], "provider": PROVIDERS[profile["backend"]],
                "configuration_id": profile["configuration_id"], "hard_deadline": request.hard_deadline,
                "group_id": value.get("group_id"),
                "observation_fresh_seconds": self.policy["observation_fresh_seconds"],
                "hourly_cost_microusd": profile["max_price_microusd"], "hold_until": 0,
                "selection": {"runtime_profile_id": profile["runtime_profile_id"], "mode": profile["mode"],
                    "gpu_type": profile["gpu_names"][0], "node_count": 1, "gpu_count": 1,
                    "ttl_seconds": original["max_duration_s"], "provider": PROVIDERS[profile["backend"]]},
                "request": asdict(request), "dstack": binding}
            if continuing:
                payload["lifecycle"] = copy.deepcopy(value["lifecycle"])
                payload["hold_until"] = payload["lifecycle"]["hold_until"]
                payload["extra_reservation_microusd"] = profile["extra_reservation_microusd"]
            connection.execute(insert(operator_nodes).values(intent_id=intent["id"], command_id=command_id,
                ordinal=0, binding_id=profile["id"], binding_hash=request_hash(binding), payload=payload,
                desired_state="running", runtime_state="reserved", updated_at=now))
            if value.get("group_id") is not None:
                from .dstack_workspace import advance_group_version
                advance_group_version(connection, self.settings, owner, value["group_id"], now)
            connection.execute(insert(scaler_actions).values(intent_id=intent["id"], pool=profile["pool"],
                launch_spec={"capacity_backend": BACKEND_MARKER, "request": asdict(request)}))
            self.repo._emit(connection, "dstack.start_requested", intent["id"], {"intent_id": intent["id"], "command_id": command_id})
        # The reservation, immutable request and command survive any crash before
        # or after HTTP. Controller restart resumes this exact intent only.
        try:
            observation = self.capacity.start(request, self.config["ssh_public_key"])
            state = "unknown" if "unknown" in observation["state"] else "running"
        except DstackError:
            state = "waiting"
        with self.repo.transaction() as connection:
            connection.execute(update(operator_commands).where(operator_commands.c.id == command_id)
                .values(state=state, updated_at=self.repo.clock()))
        return {"command_id": command_id, "node_id": intent["id"], "state": state}

    def _owned_node(self, connection, principal, node_id, *, allow_admin=False):
        owner = self.authorize(principal)
        node = self.repo._locked(connection, select(operator_nodes).where(operator_nodes.c.intent_id == node_id))
        require(node is not None and node["payload"].get("capacity_backend") == BACKEND_MARKER and
            node["payload"].get("tenant_id") == self.settings.tenant_id and
            (node["payload"].get("owner_id") == owner or allow_admin and
                owner in getattr(self.settings, "operator_admin_owners", ())),
            "dstack_node_not_found", 404)
        return dict(node)

    def state(self, principal):
        owner = self.authorize(principal)
        now = self.repo.clock()
        result = []
        with self.repo.engine.connect() as connection:
            for node in connection.execute(select(operator_nodes).order_by(operator_nodes.c.updated_at.desc())).mappings():
                payload = node["payload"]
                if payload.get("capacity_backend") != BACKEND_MARKER or payload.get("tenant_id") != self.settings.tenant_id or payload.get("owner_id") != owner:
                    continue
                binding = payload["dstack"]
                observed = binding.get("observed_at")
                fresh = _finite(observed) and 0 <= now - observed <= self.policy.get("observation_fresh_seconds", 30)
                intent = connection.execute(select(instance_intents).where(instance_intents.c.id==node["intent_id"])).mappings().one()
                native_ready = bool(fresh and binding.get("ready") is True and node["desired_state"] == "running"
                    and _window_open(intent, node, now))
                workers = list(connection.execute(select(registered_workers).where(
                    registered_workers.c.provider==intent["provider"],
                    registered_workers.c.instance_id==intent["provider_instance_id"])).mappings()) if intent["provider_instance_id"] else []
                from .worker_admission import worker_window_reason
                expected = payload.get("lifecycle", {}).get("expected_runtime_s")
                eligible = [worker for worker in workers if worker["state"]=="ready" and not worker["drain_requested"]
                    and not worker["current_job_id"] and worker["expires_at"]>now
                    and worker_window_reason(connection,worker,now,
                        deployment_profile_id=payload["selection"]["runtime_profile_id"], expected_runtime_s=expected) is None]
                ready = bool(native_ready and eligible)
                dispatch_reason = None
                if native_ready and not ready:
                    dispatch_reason = next((reason for worker in workers if
                        (reason := worker_window_reason(connection,worker,now,
                            deployment_profile_id=payload["selection"]["runtime_profile_id"], expected_runtime_s=expected))),
                        "matching_worker_unavailable")
                result.append({"node_id": node["intent_id"], "provider": payload["provider"],
                    "runtime_profile_id": payload["selection"]["runtime_profile_id"], "mode": payload["selection"]["mode"],
                    "desired_state": node["desired_state"], "state": node["runtime_state"], "ready": ready,
                    "native_ready": native_ready, "slots_ready": len(eligible) if native_ready else 0,
                    "observation_fresh": bool(fresh), "observed_at": observed, "hard_deadline": payload["hard_deadline"],
                    "hold_until": payload["hold_until"], "hourly_cost_microusd": payload["hourly_cost_microusd"],
                    **({"lifecycle": copy.deepcopy(payload["lifecycle"])} if "lifecycle" in payload else {}),
                    "billing_state": binding.get("billing_state", "unsettled"),
                    "reason_code": payload.get("stop_reason_code") or binding.get("reason_code") or dispatch_reason,
                    "dispatch_reason_code": dispatch_reason, "version": node["updated_at"]})
        return {"capacity_backend": BACKEND_MARKER, "enabled": self.policy.get("enabled") is True,
            "operator": {"account": owner},
            "nodes": result, "summary": {"ready": sum(node["ready"] for node in result),
                "starting": sum(node["state"] in {"reserved", "creating", "starting", "runtime_unconfirmed"} for node in result),
                "unknown": sum("unknown" in node["state"] or not node["observation_fresh"] for node in result)}}

    def node_command(self, principal, node_id, body, key, kind="stop"):
        require(kind == "stop" and isinstance(body, dict) and not set(body) - {"expected_version", "acknowledge_capacity_loss"}
            and ("acknowledge_capacity_loss" not in body or isinstance(body["acknowledge_capacity_loss"], str)),
            "dstack_node_command_invalid", 422)
        owner = self.authorize(principal)
        actor = _actor(self.settings.tenant_id, owner)
        digest = request_hash({"kind": "dstack_stop", "node_id": node_id, "body": body})
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node = self._owned_node(connection, principal, node_id, allow_admin=True)
            existing = self._existing_command(connection, actor, key, digest)
            if existing:
                return {"command_id": existing["id"], "node_id": node_id, "state": existing["state"]}
            require("expected_version" not in body or body["expected_version"] == node["updated_at"], "operator_node_version_conflict")
            now = self.repo.clock()
            from .dstack_workspace import CapacityLoss, stop_impact
            impact = stop_impact(connection, self.settings, [node_id], now)
            if impact["warnings"] and body.get("acknowledge_capacity_loss") != impact["fingerprint"]:
                raise CapacityLoss(impact)
            payload = copy.deepcopy(node["payload"])
            payload.update(stop_requested_at=now, hold_until=0)
            if payload.get("dstack_observation"):
                payload["dstack_observation"]["ready"] = False
            connection.execute(update(operator_nodes).where(operator_nodes.c.intent_id == node_id)
                .values(desired_state="stopped", runtime_state="draining", payload=payload, updated_at=now))
            instance = connection.execute(select(instance_intents).where(instance_intents.c.id == node_id)).mappings().one()
            if instance.get("provider_instance_id"):
                connection.execute(update(registered_workers).where(registered_workers.c.provider == instance["provider"],
                    registered_workers.c.instance_id == instance["provider_instance_id"], registered_workers.c.state != "retired")
                    .values(drain_requested=1, state="draining", updated_at=now))
            command_id = str(uuid.uuid4())
            connection.execute(insert(operator_commands).values(id=command_id, actor=actor, idempotency_key=key,
                request_hash=digest, kind="dstack_stop", state="accepted", payload={"node_id": node_id},
                created_at=now, updated_at=now))
            self.repo._emit(connection, "dstack.stop_requested", node_id, {"intent_id": node_id, "command_id": command_id})
        return {"command_id": command_id, "node_id": node_id, "state": "accepted"}

    def set_hold(self, principal, node_id, body, key):
        require(isinstance(body, dict) and set(body) <= {"hold_seconds", "expected_version", "keep_on", "idle_shutdown_seconds"}
            and any(field in body for field in ("hold_seconds", "keep_on", "idle_shutdown_seconds")),
            "dstack_hold_invalid", 422)
        owner = self.authorize(principal)
        actor = _actor(self.settings.tenant_id, owner)
        digest = request_hash({"kind": "dstack_hold", "node_id": node_id, "body": body})
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            node = self._owned_node(connection, principal, node_id)
            existing = self._existing_command(connection, actor, key, digest)
            if existing:
                return copy.deepcopy(existing["payload"])
            require("expected_version" not in body or body["expected_version"] == node["updated_at"], "operator_node_version_conflict")
            now = self.repo.clock()
            payload = copy.deepcopy(node["payload"])
            intent = self.repo._locked(connection, select(instance_intents).where(instance_intents.c.id == node_id))
            require(node["desired_state"] == "running" and _window_open(intent, node, now), "dstack_hold_window_expired")
            if payload.get("lifecycle", {}).get("version") == 2:
                original_policy = payload["lifecycle"]
                options = {"idle_shutdown_seconds": original_policy["idle_shutdown_seconds"], **body}
                # Alter only the shutdown preference; frozen accounts and the
                # protected execution quote are not client-editable.
                candidate = self._lifecycle(options, now, original_policy["expected_runtime_s"])
                if "keep_on" not in body and "hold_seconds" not in body:
                    candidate["keep_on"] = original_policy["keep_on"]
                    candidate["hold_until"] = original_policy["hold_until"]
                for field in ("reservation_seconds", "budget_account_ids"):
                    candidate[field] = copy.deepcopy(original_policy[field])
                payload.update(lifecycle=candidate, hold_until=candidate["hold_until"])
                connection.execute(update(operator_nodes).where(operator_nodes.c.intent_id == node_id)
                    .values(payload=payload, updated_at=now))
                funded = self.fund_window(connection, intent, {**node, "payload": payload}, now)
                result = {"node_id": node_id, "hold_until": candidate["hold_until"], "hard_deadline": None,
                    "lifecycle": copy.deepcopy(candidate), "reserved_cost_microusd": funded,
                    "additional_reservation_microusd": funded-intent["reserved_cost_microusd"],
                    "scope": "continuing_idle_policy_with_original_spend_cap"}
            else:
                require(not set(body)-{"hold_seconds", "expected_version"} and
                    _integer(body.get("hold_seconds"), 0, 86400), "dstack_hold_invalid", 422)
                until = now + body["hold_seconds"] if body["hold_seconds"] else 0
                require(until <= payload["hard_deadline"], "dstack_hold_deadline_exceeded")
                payload["hold_until"] = until
                connection.execute(update(operator_nodes).where(operator_nodes.c.intent_id == node_id)
                    .values(payload=payload, updated_at=now))
                result = {"node_id": node_id, "hold_until": until, "hard_deadline": payload["hard_deadline"],
                    "additional_reservation_microusd": 0, "scope": "idle_hold_within_original_deadline"}
            connection.execute(insert(operator_commands).values(id=str(uuid.uuid4()), actor=actor, idempotency_key=key,
                request_hash=digest, kind="dstack_hold", state="completed", payload=result,
                created_at=self.repo.clock(), updated_at=self.repo.clock()))
        return result


def from_environment(repo, settings, *, readiness=None):
    """Protected explicit config only; no provider I/O, DDL or budget widening."""
    filename = os.environ.get("DSTACK_OPERATOR_CONFIG", "")
    if not filename:
        return DstackOperator(repo, settings)
    try:
        config = json.loads(_protected_read(filename))
        require(isinstance(config, dict) and config.get("version") == 1, "dstack_config_invalid", 422)
        if config.get("policy", {}).get("enabled") is not True:
            return DstackOperator(repo, settings)
        token = _protected_read(config.pop("token_file"), maximum=4096).strip()
        ssh = _protected_read(config.pop("ssh_public_key_file"), maximum=16384).strip()
        client = DstackClient(config["endpoint"], token, config["project"])
        config["ssh_public_key"] = ssh
        store = LedgerDstackStore(repo, settings.tenant_id,
            observation_fresh_seconds=config["policy"]["observation_fresh_seconds"])
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
