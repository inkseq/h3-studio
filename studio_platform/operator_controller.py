"""Manual capacity controller using the existing fenced rental authority.

No queue placeholders or second provider ledger. Every paid create first commits
its existing instance_intent + single-attempt scaler_action. Restarts reconcile
that same intent even when the original response was lost. Trusted factories
must supply qualified runtime bootstrap and protected provider configuration.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import signal
import threading
import time
import uuid

from sqlalchemy import insert, select, update

from .autoscale import ScalePolicy
from .operator_capacity import (
    ACTIVE_COMMANDS,
    OperatorError,
    operator_commands,
    operator_heartbeats,
    operator_nodes,
    require,
    safe_id,
    public_bootstrap,
    offer_fingerprint,
    command_binding_fingerprint,
    MANUAL_REVIEW_CONTROLLER_PREFIX,
)
from .repository import (
    BudgetExceeded,
    Conflict,
    LeaseLost,
    instance_intents,
    registered_workers,
    scaler_actions,
    manually_reviewed_inactive,
)
from .scaler import ScaleCoordinator


SAFE_ERRORS = frozenset(
    {
        "global_capacity_disabled",
        "global_capacity_exceeded",
        "pool_disabled",
        "instance_capacity_exceeded",
        "instance_budget_not_configured",
        "budget_exceeded",
        "budget_not_configured",
        "budget_scope_conflict",
        "manual_capacity_authority_changed",
        "operator_capacity_disabled",
        "operator_policy_changed",
        "operator_binding_changed",
        "operator_binding_unavailable",
        "operator_authority_expiring",
        "operator_bootstrap_unconfigured",
        "operator_deployment_not_qualified",
        "operator_hourly_cost_limit",
        "operator_instance_limit",
        "operator_gpu_limit",
        "operator_unpriced_existing_capacity",
        "operator_provider_disabled",
        "operator_offer_changed",
        "operator_selected_offer_unavailable",
        "operator_selected_offer_stale",
        "operator_exact_offer_unsupported",
        "operator_selected_offer_invalid",
        "operator_offer_observation_unavailable",
        "operator_offer_unavailable",
        "operator_offer_selection_mismatch",
        "operator_topology_not_qualified",
        "operator_execution_slots_unqualified",
        "operator_offer_quantity_invalid",
        "operator_offer_required",
    }
)

LIFETIME_FRESH_SECONDS = 30


def provider_lifetime_current(payload, intent, now):
    """Shared late-start/worker-admission check of the persisted provider proof."""
    value = payload.get("lifetime", {}) if isinstance(payload, dict) else {}
    return bool(
        value.get("state") == "verified"
        and value.get("instance_id") == intent["provider_instance_id"]
        and type(value.get("observed_at")) in (int, float)
        and math.isfinite(value["observed_at"])
        and 0 <= now - value["observed_at"] <= LIFETIME_FRESH_SECONDS
        and type(value.get("safe_deadline")) in (int, float)
        and math.isfinite(value["safe_deadline"])
        and now < intent["hard_deadline"] <= value["safe_deadline"]
    )


def safe_error(error):
    if isinstance(error, OperatorError):
        return (
            error.code
            if error.code in SAFE_ERRORS
            else "operator_configuration_invalid"
        )
    value = str(error)
    if isinstance(error, (BudgetExceeded, Conflict)) and value in SAFE_ERRORS:
        return value
    return "operator_controller_error"


class OperatorController:
    def __init__(
        self,
        service,
        *,
        provider_factory,
        boot_factory=None,
        enabled=False,
        leader_id=None,
        coordinator_factory=None,
        inventory_refresh=None,
        status_writer=None,
    ):
        require(
            type(enabled) is bool and callable(provider_factory),
            "operator_controller_configuration_invalid",
            422,
        )
        self.service, self.repo = service, service.repo
        self.provider_factory, self.boot_factory = provider_factory, boot_factory
        self.enabled = enabled
        # Versioned control protocol prevents a newly released API from handing
        # an exact-machine command to an old controller that ignores the choice.
        self.leader_id = leader_id or MANUAL_REVIEW_CONTROLLER_PREFIX + uuid.uuid4().hex
        require(safe_id(self.leader_id), "operator_controller_identity_invalid", 422)
        self.coordinator_factory = coordinator_factory
        self.inventory_refresh = inventory_refresh
        self.status_writer = status_writer
        self.coordinators, self.boots = {}, {}
        self.shutting_down = False
        self.shutdown_started_at = None
        self.shutdown_targets = set()
        self.shutdown_released = set()

    def request_shutdown(self):
        """Signal-safe intent only. Database/provider work stays in the main loop."""
        self.shutting_down = True

    def _drain_for_shutdown(self, nodes):
        if self.shutdown_started_at is None:
            self.shutdown_started_at = time.monotonic()
        self.shutdown_targets.update(self.boots)
        for node in nodes:
            if node["intent_id"] in self.shutdown_released:
                continue
            key = (node["binding_id"], node["binding_hash"])
            if key not in self.coordinators and node["intent_id"] not in self.boots:
                continue
            intent_id = node["intent_id"]
            coordinator = self.coordinators.get(key)
            boot = self.boots.get(intent_id)
            # Local children are ours to drain even after losing provider
            # leadership. This never authorizes a provider mutation.
            if boot is not None:
                drain = getattr(boot, "request_drain", None)
                if callable(drain):
                    drain()
            if coordinator is None:
                continue
            binding = self.service.registry.get(node["binding_id"])
            lease = coordinator.acquire(binding.pool, self.leader_id)
            if lease is None:
                continue
            with self.repo.transaction() as connection:
                coordinator._leader(connection, lease)
                self.repo._lock_capacity(connection)
                current = self.repo._locked(
                    connection,
                    select(operator_nodes).where(
                        operator_nodes.c.intent_id == intent_id
                    ),
                )
                intent = self.repo._locked(
                    connection,
                    select(instance_intents).where(instance_intents.c.id == intent_id),
                )
                if not current or not intent or intent["state"] == "destroyed":
                    continue
                self.shutdown_targets.add(intent_id)
                if current["desired_state"] == "running":
                    connection.execute(
                        update(operator_nodes)
                        .where(operator_nodes.c.intent_id == intent_id)
                        .values(desired_state="drained", updated_at=self.repo.clock())
                    )
                    self.repo._emit(
                        connection,
                        "operator.capacity.controller_draining",
                        intent_id,
                        {"intent_id": intent_id, "controller_id": self.leader_id},
                    )

    def shutdown_status(self):
        """Release only proven local ownership, never claim supplier cleanup."""
        if not self.shutting_down:
            return {"state": "running", "local_connections_released": False}
        pending = []
        for intent_id, boot in list(self.boots.items()):
            if intent_id in self.shutdown_released:
                continue
            try:
                inspect = getattr(boot, "shutdown_status", None)
                release = getattr(boot, "release_after_drain", None)
                proof = inspect() if callable(inspect) else None
                if (
                    not isinstance(proof, dict)
                    or proof.get("ownership_known") is not True
                ):
                    pending.append(
                        {
                            "node_id": intent_id,
                            "code": "operator_child_ownership_unconfirmed",
                        }
                    )
                elif proof.get("children_done") is not True:
                    pending.append(
                        {
                            "node_id": intent_id,
                            "code": "operator_collection_still_running",
                        }
                    )
                elif not callable(release):
                    pending.append(
                        {
                            "node_id": intent_id,
                            "code": "operator_local_release_unavailable",
                        }
                    )
                else:
                    release()
                    self.shutdown_released.add(intent_id)
            except Exception:
                pending.append(
                    {"node_id": intent_id, "code": "operator_local_release_unconfirmed"}
                )
        result = {
            "state": "shutdown_waiting" if pending else "shutdown_complete",
            "local_connections_released": not pending,
            "pending": pending,
            "cloud_removal_confirmed": False,
            "billing_settled": False,
        }
        if self.status_writer is not None:
            self.status_writer(self.leader_id, result)
        return result

    def _coordinator(self, binding):
        key = (binding.binding_id, binding.fingerprint)
        if key not in self.coordinators:
            provider = self.provider_factory(binding)
            require(
                provider.provider_id == binding.launch.provider
                and provider.enabled is True,
                "operator_provider_disabled",
            )
            coordinator = (
                self.coordinator_factory(binding, provider)
                if self.coordinator_factory
                else ScaleCoordinator(
                    self.repo, provider=provider, enabled=self.enabled
                )
            )
            # Optional provider support. This proves its dedicated runtime is idle,
            # not merely that the VM reports RUNNING. Unknown boots remain busy.
            if hasattr(provider, "_idle_probe"):
                provider._idle_probe = self.idle_probe
            self.coordinators[key] = coordinator
        return self.coordinators[key]

    def idle_proof(self, intent_id, instance_id=None):
        boot = self.boots.get(intent_id)
        if boot is None:
            return False
        probe = getattr(boot, "is_idle", None) or getattr(boot, "idle_proof", None)
        try:
            return callable(probe) and probe() is True
        except Exception:
            return False

    def idle_probe(self, intent_id, instance_id):
        """Preserve Lium's timestamped, instance-bound proof type unchanged."""
        boot = self.boots.get(intent_id)
        if boot is None:
            return None
        probe = getattr(boot, "idle_probe", None)
        if not callable(probe):
            return None
        return probe(intent_id, instance_id)

    def _policy(self, binding, deadline):
        value = self.service.policy()
        return ScalePolicy(
            dry_run=False,
            max_instances=value["max_instances"],
            max_physical_gpus=value["max_physical_gpus"],
            new_instance_slots=binding.execution_slots,
            new_instance_physical_gpus=binding.gpu_count,
            idle_before_drain_s=value["idle_shutdown_seconds"],
            instance_reservation_microusd=binding.reservation_per_node_microusd,
            hard_deadline=deadline,
        )

    def _record_command(self, command_id, state, reason=None):
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            row = self.repo._locked(
                connection,
                select(operator_commands).where(operator_commands.c.id == command_id),
            )
            if row is None or row["state"] == "completed":
                return
            if row["state"] == state and row["reason_code"] == reason:
                return
            connection.execute(
                update(operator_commands)
                .where(operator_commands.c.id == command_id)
                .values(state=state, reason_code=reason, updated_at=self.repo.clock())
            )
            self.repo._emit(
                connection,
                "operator.capacity.operation_updated",
                command_id,
                {"operation_id": command_id, "state": state, "reason_code": reason},
            )

    def _authorize_start(self, connection, command, binding):
        require(not self.shutting_down, "operator_controller_stopping")
        current = self.repo._locked(
            connection,
            select(operator_commands).where(operator_commands.c.id == command["id"]),
        )
        policy = self.service._policy(connection)
        require(
            current is not None and current["state"] in ACTIVE_COMMANDS,
            "operator_policy_changed",
        )
        require(policy["enabled"], "operator_capacity_disabled")
        require(
            policy["version"] == command["payload"]["policy_version"],
            "operator_policy_changed",
        )
        require(binding.enabled, "operator_deployment_not_qualified")
        chosen = command["payload"]["selection"]
        require(
            command_binding_fingerprint(binding, chosen)
            == command["payload"]["binding_hash"],
            "operator_binding_changed",
        )
        if chosen.get("offer_id"):
            from .capacity_candidates import resolve_selected_offer

            offer = resolve_selected_offer(
                connection, self.service.registry, chosen, self.repo.clock()
            )
            require(
                offer_fingerprint(offer) == command["payload"].get("offer_fingerprint"),
                "operator_offer_changed",
            )
        deadline = command["payload"]["hard_deadline"]
        require(
            self.repo.clock() < deadline <= binding.expires_at,
            "operator_authority_expiring",
        )
        usage = self.service._committed_capacity(connection)
        require(not usage["unpriced"], "operator_unpriced_existing_capacity")
        # The durable pending command already includes this proposed node. The
        # reserve callback replaces that pending count atomically with the intent.
        require(
            usage["instances"] <= policy["max_instances"], "operator_instance_limit"
        )
        require(
            usage["physical_gpus"] <= policy["max_physical_gpus"], "operator_gpu_limit"
        )
        require(
            usage["hourly"] <= policy["max_hourly_cost_microusd"],
            "operator_hourly_cost_limit",
        )
        return True

    def _start(self, command):
        if self.shutting_down:
            return "shutting_down"
        payload = command["payload"]
        binding = self.service.registry.get(payload["binding_id"])
        require(callable(self.boot_factory), "operator_bootstrap_unconfigured")
        coordinator = self._coordinator(binding)
        chosen = payload["selection"]
        for ordinal in range(chosen["node_count"]):
            if self.shutting_down:
                return "shutting_down"
            with self.repo.engine.connect() as connection:
                existing = (
                    connection.execute(
                        select(operator_nodes).where(
                            operator_nodes.c.command_id == command["id"],
                            operator_nodes.c.ordinal == ordinal,
                        )
                    )
                    .mappings()
                    .first()
                )
            if existing is not None:
                # Never resubmit an existing allocation, including UNKNOWN.
                continue

            def reserved(connection, intent):
                connection.execute(
                    insert(operator_nodes).values(
                        intent_id=intent["id"],
                        command_id=command["id"],
                        ordinal=ordinal,
                        binding_id=binding.binding_id,
                        binding_hash=binding.fingerprint,
                        payload={
                            "selection": chosen,
                            "hourly_cost_microusd": binding.hourly_cost_microusd,
                        },
                        desired_state="running",
                        runtime_state="waiting_provider",
                        updated_at=self.repo.clock(),
                    )
                )

            extra = (
                {"selected_offer": payload["selected_offer"]}
                if chosen.get("offer_id")
                else {}
            )
            outcome = coordinator.create_manual_once(
                self.leader_id,
                binding.scope,
                binding.pool,
                "operator-" + command["id"] + "-" + str(ordinal),
                launch=binding.launch,
                policy=self._policy(binding, payload["hard_deadline"]),
                budget_account_ids=binding.budget_account_ids,
                authorize=lambda connection: self._authorize_start(
                    connection, command, binding
                ),
                on_reserved=reserved,
                **extra,
            )
            if outcome["state"] in {"disabled", "not_leader"}:
                return outcome["state"]
        return "waiting"

    def _node_snapshot(self, intent_id):
        with self.repo.engine.connect() as connection:
            intent = (
                connection.execute(
                    select(instance_intents).where(instance_intents.c.id == intent_id)
                )
                .mappings()
                .one()
            )
            node = (
                connection.execute(
                    select(operator_nodes).where(
                        operator_nodes.c.intent_id == intent_id
                    )
                )
                .mappings()
                .one()
            )
            action = (
                connection.execute(
                    select(scaler_actions).where(
                        scaler_actions.c.intent_id == intent_id
                    )
                )
                .mappings()
                .one()
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
        return dict(intent), dict(node), dict(action), workers

    def _qualified(self, binding, workers):
        valid = [
            worker
            for worker in workers
            if worker["expires_at"] > self.repo.clock()
            and worker["state"] in {"ready", "busy"}
            and not worker["drain_requested"]
            and worker["spec"].get("backend") == "wangp-worker"
            and worker["spec"].get("model_id") == binding.model_id
            and worker["spec"].get("configuration_id") == binding.configuration_id
            and worker["spec"].get("engine_manifest_digest")
            == binding.engine_manifest_digest
            and set(worker["spec"].get("recipe_ids", ())) == set(binding.recipe_ids)
        ]
        return (
            len(valid) == binding.execution_slots
            and len({gpu for w in valid for gpu in w["spec"]["physical_gpu_ids"]})
            == binding.gpu_count
        )

    def _start_guard(self, coordinator, lease, binding, intent_id):
        """Late upload callbacks must retain the original durable authority."""
        try:
            if not self.enabled or self.shutting_down:
                return False
            current_binding = self.service.registry.get(binding.binding_id)
            if (
                not current_binding.enabled
                or current_binding.fingerprint != binding.fingerprint
            ):
                return False
            with self.repo.transaction() as connection:
                coordinator._leader(connection, lease)
                self.repo._lock_capacity(connection)
                node = self.repo._locked(
                    connection,
                    select(operator_nodes).where(
                        operator_nodes.c.intent_id == intent_id
                    ),
                )
                intent = self.repo._locked(
                    connection,
                    select(instance_intents).where(instance_intents.c.id == intent_id),
                )
                return bool(
                    self.service._policy(connection)["enabled"]
                    and node
                    and intent
                    and node["desired_state"] == "running"
                    and node["binding_hash"] == binding.fingerprint
                    and intent["pool"] == binding.pool
                    and intent["state"] in {"starting", "ready", "busy"}
                    and self.repo.clock()
                    < intent["hard_deadline"]
                    <= current_binding.expires_at
                    and provider_lifetime_current(
                        node["payload"], intent, self.repo.clock()
                    )
                )
        except Exception:
            return False

    def _provider_lifetime(self, coordinator, lease, binding, intent):
        """Shorten the original ledger window; never renew or change reservation.

        Provider inspection is outside the database transaction. Persist only
        under the same still-current pool fence and exact instance identity.
        Failure closes new runtime/job admission while existing collection stays.
        """
        proof = None
        try:
            method = getattr(coordinator.provider, "lifetime", None)
            require(callable(method), "operator_provider_lifetime_unverified")
            hours = min(4, math.ceil(binding.max_ttl_seconds / 3600))
            result = method(
                intent["id"],
                intent["provider_instance_id"],
                local_created_at=intent["created_at"],
                maximum_hours=hours,
            )
            require(
                isinstance(result, dict)
                and result.get("instance_id") == intent["provider_instance_id"]
                and type(result.get("safe_deadline")) in (int, float)
                and math.isfinite(result["safe_deadline"])
                and self.repo.clock()
                < result["safe_deadline"]
                <= intent["created_at"] + hours * 3600,
                "operator_provider_lifetime_unverified",
            )
            proof = {
                "state": "verified",
                "instance_id": intent["provider_instance_id"],
                "safe_deadline": result["safe_deadline"],
                "observed_at": self.repo.clock(),
            }
        except Exception:
            pass
        with self.repo.transaction() as connection:
            coordinator._leader(connection, lease)
            self.repo._lock_capacity(connection)
            current = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == intent["id"]),
            )
            node = self.repo._locked(
                connection,
                select(operator_nodes).where(
                    operator_nodes.c.intent_id == intent["id"]
                ),
            )
            require(
                current
                and node
                and current["provider_instance_id"] == intent["provider_instance_id"]
                and node["binding_hash"] == binding.fingerprint,
                "operator_binding_changed",
            )
            previous = node["payload"].get("lifetime", {})
            if proof:
                safe = min(current["hard_deadline"], proof["safe_deadline"])
                # Retain the strictest verified proof even if the provider later
                # reports a longer window. No observation renews an old node.
                if type(previous.get("safe_deadline")) in (int, float):
                    safe = min(safe, previous["safe_deadline"])
                proof["safe_deadline"] = safe
                if safe < current["hard_deadline"]:
                    connection.execute(
                        update(instance_intents)
                        .where(instance_intents.c.id == intent["id"])
                        .values(hard_deadline=safe, updated_at=self.repo.clock())
                    )
                    self.repo._emit(
                        connection,
                        "operator.capacity.deadline_shortened",
                        intent["id"],
                        {"intent_id": intent["id"], "hard_deadline": safe},
                    )
            else:
                proof = {
                    "state": "unverified",
                    "instance_id": intent["provider_instance_id"],
                    "safe_deadline": previous.get("safe_deadline"),
                    "observed_at": self.repo.clock(),
                }
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == intent["id"])
                .values(
                    payload={**node["payload"], "lifetime": proof},
                    updated_at=self.repo.clock(),
                )
            )
        return (
            proof["state"] == "verified" and proof["safe_deadline"] > self.repo.clock()
        )

    def _observe(self, node):
        with self.repo.engine.connect() as connection:
            intent = (
                connection.execute(
                    select(instance_intents).where(
                        instance_intents.c.id == node["intent_id"]
                    )
                )
                .mappings()
                .one()
            )
            # tick() can have cached this node before the recovery transaction
            # committed. Bind the destroyed-intent fence to the current journal.
            if intent["state"] == "destroyed":
                node = (
                    connection.execute(
                        select(operator_nodes).where(
                            operator_nodes.c.intent_id == intent["id"]
                        )
                    )
                    .mappings()
                    .one()
                )
            from .settlement_recovery import reviewed_instance_fenced

            if reviewed_instance_fenced(connection, intent, node):
                return "stopped"
            if manually_reviewed_inactive(connection, intent):
                return "manually_reviewed"
        binding = self.service.registry.get(node["binding_id"])
        require(binding.fingerprint == node["binding_hash"], "operator_binding_changed")
        coordinator = self._coordinator(binding)
        intent_id = node["intent_id"]
        intent, latest, action, workers = self._node_snapshot(intent_id)
        policy = self._policy(binding, intent["hard_deadline"])
        desired = latest["desired_state"]
        stopping = desired != "running" or intent["hard_deadline"] <= self.repo.clock()
        existing_boot = self.boots.get(intent_id)
        if existing_boot and stopping:
            method = getattr(existing_boot, "request_drain", None)
            if callable(method):
                method()
            if desired == "stopped":
                cancel = getattr(existing_boot, "cancel_preparation", None)
                if callable(cancel):
                    cancel()
        result = coordinator.observe_manual_instance(
            self.leader_id,
            intent_id,
            policy=policy,
            drain=stopping,
            stop=desired == "stopped",
        )
        if result["state"] == "not_leader":
            return "not_leader"
        intent, latest, action, workers = self._node_snapshot(intent_id)
        runtime = {"destroyed": "destroyed", "destroying": "removal_pending"}.get(
            intent["state"], "waiting_provider"
        )
        bootstrap = None
        if runtime == "destroyed" and intent_id in self.boots:
            close = getattr(self.boots[intent_id], "close", None)
            if callable(close):
                close()
            self.boots.pop(intent_id, None)
        fact = action["last_observation"] or {}
        fresh = (
            action["last_observed_at"] is not None
            and 0 <= self.repo.clock() - action["last_observed_at"] <= 30
        )
        can_observe = (
            fresh
            and fact.get("state") == "running"
            and intent["provider_instance_id"]
            and fact.get("instance_id") == intent["provider_instance_id"]
            and intent["state"]
            not in {"destroyed", "destroying", "creation_unknown", "creating"}
        )
        if can_observe:
            lease = coordinator.acquire(binding.pool, self.leader_id)
            if lease is None:
                return "not_leader"
            lifetime_verified = self._provider_lifetime(
                coordinator, lease, binding, intent
            )
            intent, latest, action, workers = self._node_snapshot(intent_id)
            stopping = (
                desired != "running" or intent["hard_deadline"] <= self.repo.clock()
            )
            # Re-check the provider's immutable execution binding when available.
            execution_allowed = getattr(coordinator.provider, "execution_allowed", None)
            if not lifetime_verified and not stopping:
                runtime = "provider_lifetime_unverified"
            elif (
                callable(execution_allowed)
                and execution_allowed(intent_id, intent["provider_instance_id"])
                is not True
            ):
                runtime = "provider_execution_unverified"
            elif self.shutting_down and intent_id not in self.boots:
                # No owned runtime/child exists. Preserve its durable drained
                # allocation; shutdown must not construct a new boot process.
                runtime = "draining"
            elif callable(self.boot_factory):
                if intent_id not in self.boots:
                    self.boots[intent_id] = self.boot_factory(
                        binding, intent, latest["payload"]["selection"]
                    )
                boot = self.boots[intent_id]
                setter = getattr(boot, "set_start_guard", None)
                if callable(setter):
                    setter(
                        lambda: self._start_guard(
                            coordinator, lease, binding, intent_id
                        )
                    )
                if stopping:
                    drain = getattr(boot, "request_drain", None)
                    if callable(drain):
                        drain()
                    if desired == "stopped":
                        cancel = getattr(boot, "cancel_preparation", None)
                        if callable(cancel):
                            cancel()
                report = boot.tick(intent_id, stopping=bool(stopping))
                reported = report.get("state") if isinstance(report, dict) else report
                allowed = {
                    "ready",
                    "preparing",
                    "starting",
                    "waiting",
                    "blocked",
                    "failed",
                    "draining",
                    "stopped",
                    "recovering",
                }
                runtime = reported if reported in allowed else "preparing"
                bootstrap = public_bootstrap(
                    {
                        **(report if isinstance(report, dict) else {}),
                        "state": runtime,
                        "observed_at": self.repo.clock(),
                    }
                )
                intent, latest, action, workers = self._node_snapshot(intent_id)
                # A hook's 'ready' string is not readiness evidence. Qualified
                # fresh registrations must match exact model/engine/device set.
                if (
                    not stopping
                    and runtime not in {"blocked", "failed"}
                    and self._qualified(binding, workers)
                ):
                    runtime = "ready"
                    with self.repo.transaction() as connection:
                        coordinator._leader(connection, lease)
                        self.repo._lock_capacity(connection)
                        current = self.repo._locked(
                            connection,
                            select(instance_intents).where(
                                instance_intents.c.id == intent_id
                            ),
                        )
                        if current["state"] == "starting":
                            self.repo.update_instance(
                                intent_id, "ready", connection=connection
                            )
                elif runtime == "ready":
                    runtime = "awaiting_qualified_workers"
            else:
                runtime = "bootstrap_unconfigured"
        if intent["state"] in {"creating", "creation_unknown"}:
            runtime = "creation_unknown"
        if stopping and runtime == "ready":
            runtime = "draining"
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            current = self.repo._locked(
                connection,
                select(operator_nodes).where(operator_nodes.c.intent_id == intent_id),
            )
            current_intent = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == intent_id),
            )
            from .settlement_recovery import reviewed_instance_fenced

            if reviewed_instance_fenced(connection, current_intent, current):
                return "stopped"
            if manually_reviewed_inactive(connection, current_intent):
                return "manually_reviewed"
            values = {"runtime_state": runtime, "updated_at": self.repo.clock()}
            if bootstrap is not None:
                values["payload"] = {**current["payload"], "bootstrap": bootstrap}
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == intent_id)
                .values(**values)
            )
        return runtime

    def _summarize_commands(self):
        with self.repo.engine.connect() as connection:
            commands = list(
                connection.execute(
                    select(operator_commands).where(
                        operator_commands.c.state.in_(ACTIVE_COMMANDS)
                    )
                ).mappings()
            )
            nodes = [
                n
                for n in connection.execute(select(operator_nodes)).mappings()
                if n["payload"].get("capacity_backend") != "dstack-v1"
            ]
            intents = {
                row["id"]: row
                for row in connection.execute(select(instance_intents)).mappings()
            }
            workers = list(connection.execute(select(registered_workers)).mappings())
        commands = [c for c in commands if not c["kind"].startswith("dstack_")]
        for command in commands if not self.shutting_down else ():
            if command["kind"] == "start":
                owned = [row for row in nodes if row["command_id"] == command["id"]]
                if any(
                    intents[n["intent_id"]]["state"] in {"creating", "creation_unknown"}
                    for n in owned
                ):
                    self._record_command(
                        command["id"], "unknown", "operator_creation_unknown"
                    )
                elif len(owned) == command["payload"]["selection"]["node_count"]:
                    if all(n["runtime_state"] == "ready" for n in owned):
                        self._record_command(command["id"], "completed")
                    elif all(
                        intents[n["intent_id"]]["state"] == "destroyed" for n in owned
                    ):
                        self._record_command(
                            command["id"],
                            "blocked",
                            "operator_nodes_stopped_before_ready",
                        )
                    elif any(
                        intents[n["intent_id"]]["state"] == "destroyed" for n in owned
                    ):
                        # An authoritative failed/removed allocation is final.
                        # Keep the surviving identities; never replace it merely
                        # to make the requested node count appear complete.
                        self._record_command(
                            command["id"], "partial", "operator_partial_capacity"
                        )
                    elif any(
                        n["runtime_state"] in {"failed", "blocked"} for n in owned
                    ):
                        reconciliation = any(
                            n["runtime_state"] == "blocked"
                            and (
                                public_bootstrap(n["payload"].get("bootstrap")) or {}
                            ).get("reason_code")
                            == "bootstrap_reconciliation_required"
                            for n in owned
                        )
                        self._record_command(
                            command["id"],
                            "blocked",
                            "bootstrap_reconciliation_required"
                            if reconciliation
                            else "operator_bootstrap_failed",
                        )
                    else:
                        self._record_command(
                            command["id"], "waiting", "operator_preparing"
                        )
            else:
                intent = intents.get(command["payload"]["node_id"])
                if not intent:
                    continue
                bound = [
                    w
                    for w in workers
                    if w["provider"] == intent["provider"]
                    and w["instance_id"] == intent["provider_instance_id"]
                    and w["state"] != "retired"
                ]
                if (
                    intent["state"] == "destroyed"
                    or command["kind"] == "drain"
                    and bound
                    and all(w["drain_requested"] for w in bound)
                ):
                    self._record_command(command["id"], "completed")
                else:
                    self._record_command(
                        command["id"],
                        "waiting",
                        "operator_safe_stop_waiting"
                        if command["kind"] == "stop"
                        else "operator_drain_waiting",
                    )

    def tick(self):
        if not self.enabled:
            return {"state": "disabled", "commands": 0, "nodes": 0}
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            row = self.repo._locked(
                connection,
                select(operator_heartbeats).where(operator_heartbeats.c.id == "global"),
            )
            value = {
                "controller_id": self.leader_id,
                "observed_at": self.repo.clock(),
                "state": "running",
            }
            if row:
                connection.execute(
                    update(operator_heartbeats)
                    .where(operator_heartbeats.c.id == "global")
                    .values(**value)
                )
            else:
                connection.execute(
                    insert(operator_heartbeats).values(id="global", **value)
                )
            commands = list(
                connection.execute(
                    select(operator_commands)
                    .where(
                        operator_commands.c.kind == "start",
                        operator_commands.c.state.in_(ACTIVE_COMMANDS),
                    )
                    .order_by(operator_commands.c.created_at, operator_commands.c.id)
                ).mappings()
            )
        for command in commands:
            try:
                result = self._start(command)
                if result == "not_leader":
                    continue
            except LeaseLost:
                continue
            except Exception as error:
                if self.shutting_down:
                    continue
                self._record_command(command["id"], "blocked", safe_error(error))
        with self.repo.engine.connect() as connection:
            # Includes completed/blocked commands: rentals still need lifecycle
            # reconciliation, pending invoice settlement and conservative cleanup.
            nodes = [
                n
                for n in connection.execute(select(operator_nodes)).mappings()
                if n["payload"].get("capacity_backend") != "dstack-v1"
            ]
        if self.shutting_down:
            self._drain_for_shutdown(nodes)
        errors = 0
        for node in nodes:
            if self.shutting_down and (
                node["intent_id"] not in self.shutdown_targets
                or node["intent_id"] in self.shutdown_released
            ):
                continue
            try:
                self._observe(node)
            except LeaseLost:
                continue
            except Exception:
                with self.repo.transaction() as connection:
                    self.repo._lock_capacity(connection)
                    current = self.repo._locked(
                        connection,
                        select(operator_nodes).where(
                            operator_nodes.c.intent_id == node["intent_id"]
                        ),
                    )
                    intent = self.repo._locked(
                        connection,
                        select(instance_intents).where(
                            instance_intents.c.id == node["intent_id"]
                        ),
                    )
                    from .settlement_recovery import reviewed_instance_fenced

                    if reviewed_instance_fenced(connection, intent, current):
                        continue
                    errors += 1
                    connection.execute(
                        update(operator_nodes)
                        .where(operator_nodes.c.intent_id == node["intent_id"])
                        .values(
                            runtime_state="observation_failed",
                            updated_at=self.repo.clock(),
                        )
                    )
        self._summarize_commands()
        if self.inventory_refresh is not None:
            try:
                self.inventory_refresh(self.leader_id, stopping=self.shutting_down)
            except Exception:
                # A missing/stale observation blocks future previews, never
                # interrupts an existing rental's lifetime/collection loop.
                pass
        status = (
            "draining" if self.shutting_down else "degraded" if errors else "running"
        )
        with self.repo.transaction() as connection:
            # A competing controller's newer heartbeat is not ours to overwrite.
            connection.execute(
                update(operator_heartbeats)
                .where(
                    operator_heartbeats.c.id == "global",
                    operator_heartbeats.c.controller_id == self.leader_id,
                )
                .values(observed_at=self.repo.clock(), state=status)
            )
        result = {
            "state": status,
            "commands": len(commands),
            "nodes": len(nodes),
            "errors": errors,
        }
        if self.status_writer is not None:
            self.status_writer(self.leader_id, result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--factory",
        required=True,
        help="Trusted deployment module:callable; never supplied by HTTP",
    )
    parser.add_argument(
        "--config", required=True, help="Protected server configuration path"
    )
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument(
        "--shutdown-grace-seconds",
        type=int,
        default=900,
        help="After this grace window report attention required but retain live collection/tunnels",
    )
    args = parser.parse_args(argv)
    if not args.enabled:
        print(json.dumps({"state": "disabled"}))
        return 0
    if not 1 <= args.interval <= 60:
        parser.error("interval must be between 1 and 60 seconds")
    if not 30 <= args.shutdown_grace_seconds <= 86400:
        parser.error("shutdown grace must be between 30 and 86400 seconds")
    module, separator, name = args.factory.partition(":")
    if not separator or not name.isidentifier():
        parser.error("trusted factory must be module:callable")
    previous = {}
    try:
        controller = getattr(importlib.import_module(module), name)(args.config)
        require(
            isinstance(controller, OperatorController) and controller.enabled,
            "operator_controller_configuration_invalid",
        )
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda *_: controller.request_shutdown())
        attention_reported = False
        while True:
            try:
                print(json.dumps(controller.tick()), flush=True)
                if args.once and not controller.shutting_down:
                    controller.request_shutdown()
                    # Even --once must drain any runtime it just acquired;
                    # it is not permission to abandon its collectors.
                    print(json.dumps(controller.tick()), flush=True)
                if controller.shutting_down:
                    status = controller.shutdown_status()
                    print(json.dumps(status), flush=True)
                    if status["state"] == "shutdown_complete":
                        return 0
                    if (
                        not attention_reported
                        and controller.shutdown_started_at is not None
                        and time.monotonic() - controller.shutdown_started_at
                        >= args.shutdown_grace_seconds
                    ):
                        print(
                            json.dumps(
                                {
                                    "state": "shutdown_attention_required",
                                    "code": "operator_shutdown_requires_recovery",
                                    "connections_retained": True,
                                }
                            ),
                            flush=True,
                        )
                        attention_reported = True
                time.sleep(args.interval)
            except KeyboardInterrupt:
                controller.request_shutdown()
                continue
            except Exception:
                # An unexpected controller error must not abandon owned
                # collectors. Switch to drain and keep the process observable.
                controller.request_shutdown()
                print(
                    json.dumps(
                        {
                            "state": "shutdown_waiting",
                            "code": "operator_controller_error",
                        }
                    ),
                    flush=True,
                )
                try:
                    time.sleep(args.interval)
                except KeyboardInterrupt:
                    pass
    except KeyboardInterrupt:
        # Before a controller exists there are no connections owned here.
        return 1
    except Exception:
        print(
            json.dumps(
                {"state": "failed", "code": "operator_controller_startup_failed"}
            ),
            flush=True,
        )
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    # Factories import the canonical OperatorController class. Running the
    # implementation as __main__ would otherwise compare distinct class types.
    from studio_platform.operator_controller import main as canonical_main

    raise SystemExit(canonical_main())
