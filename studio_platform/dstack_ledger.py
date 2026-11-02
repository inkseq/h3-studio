"""Atomic native-capacity journals on the original database rows."""

from __future__ import annotations

import logging
import copy
import uuid
from sqlalchemy import insert, select, update
from .capacity_policy import window_open, cold_unadmitted_runtime
from .operator_capacity import (
    advance_command_revision,
    operator_nodes,
    require,
    safe_id,
)
from .repository import (
    attempts,
    budget_reservations,
    instance_intents,
    jobs,
    registered_workers,
    scaler_actions,
    scaler_receipts,
)
from .providers.registry import VASTAI

from .dstack_operator_contract import (
    BACKEND_MARKER,
    PROVIDERS,
    TERMINAL_JOBS,
    OBSERVATION_KEYS,
    IDENTITY_KEYS,
    REGISTRATION_REASON_CODES,
    _finite,
)

_logger = logging.getLogger(__name__)


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
                    binding["backend"] == VASTAI.id
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
        except Exception as _observation_error:
            _logger.warning(
                "dstack_ledger_record_failed error_type=%s",
                type(_observation_error).__name__,
            )
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
                and review.get("provider") == binding.get("backend") == VASTAI.id
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


# Preserve the original public import and pickle identity of moved definitions.
LedgerDstackStore.__module__ = "studio_platform.dstack_operator"
