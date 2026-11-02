"""Owner-scoped capacity commands over the original ledger and public bindings."""

from __future__ import annotations

import logging
import math
import uuid
from sqlalchemy import (
    func,
    insert,
    or_,
    select,
    update,
)
from .repository import (
    canonical,
    request_hash,
    capacity_gate,
    instance_intents,
    jobs,
    attempts,
    registered_workers,
    registered_devices,
    scaler_actions,
    scaler_receipts,
    paused_capacity_pools,
    manually_reviewed_inactive,
)
from .operator_extensions import OperatorExtensions

from .operator_capacity_contract import (
    operator_policy as operator_policy,
    operator_previews as operator_previews,
    operator_commands as operator_commands,
    operator_nodes as operator_nodes,
    operator_heartbeats as operator_heartbeats,
    operator_inventory as operator_inventory,
    INVENTORY_FRESH_SECONDS as INVENTORY_FRESH_SECONDS,
    CONTROLLER_FRESH_SECONDS as CONTROLLER_FRESH_SECONDS,
    REMOVAL_ATTENTION_SECONDS as REMOVAL_ATTENTION_SECONDS,
    MANUAL_REVIEW_CONTROLLER_PREFIX as MANUAL_REVIEW_CONTROLLER_PREFIX,
    removal_confirmation as removal_confirmation,
    inventory_projection as inventory_projection,
    POLICY_FIELDS as POLICY_FIELDS,
    DEFAULT_POLICY as DEFAULT_POLICY,
    FILTER_FIELDS as FILTER_FIELDS,
    ACTIVE_COMMANDS as ACTIVE_COMMANDS,
    OperatorError as OperatorError,
    require as require,
    safe_id as safe_id,
    positive_int as positive_int,
    command_binding_fingerprint as command_binding_fingerprint,
    offer_fingerprint as offer_fingerprint,
    exact_offer_controller_ready as exact_offer_controller_ready,
    selection as selection,
    DeploymentBinding as DeploymentBinding,
    OperatorRegistry as OperatorRegistry,
    command_public as command_public,
    command_revision as command_revision,
    advance_command_revision as advance_command_revision,
    node_version as node_version,
    public_bootstrap as public_bootstrap,
)

_logger = logging.getLogger(__name__)


class OperatorCapacity(OperatorExtensions):
    def __init__(self, repo, settings, registry=None):
        self.repo, self.settings, self.registry = (
            repo,
            settings,
            registry or OperatorRegistry(),
        )

    def authorize(self, principal):
        from .identity import require_admin

        return require_admin(
            principal,
            self.repo,
            self.settings,
            legacy_capacity=True,
            legacy_code="operator_forbidden",
        )

    def offers(self, principal, chosen):
        self.authorize(principal)
        chosen = selection(chosen)
        now = self.repo.clock()
        try:
            value = self.registry.offers(chosen, now)
        except Exception as _observation_error:
            _logger.warning(
                "operator_capacity_offers_failed error_type=%s",
                type(_observation_error).__name__,
            )
            value = {
                "status": "unavailable",
                "observed_at": None,
                "offers": [],
                "reason_code": "operator_inventory_unavailable",
            }
        from .capacity_market import market_projection

        with self.repo.engine.connect() as connection:
            market = market_projection(connection, self.registry, chosen, now)
        return {**value, "market": market}

    def _policy(self, connection):
        row = (
            connection.execute(
                select(operator_policy).where(operator_policy.c.id == "global")
            )
            .mappings()
            .first()
        )
        return (
            {"version": row["version"], **row["payload"]}
            if row
            else {"version": 0, **DEFAULT_POLICY}
        )

    def policy(self):
        with self.repo.engine.connect() as connection:
            return self._policy(connection)

    def update_policy(self, principal, body):
        actor = self.authorize(principal)
        require(
            isinstance(body, dict)
            and set(body) == POLICY_FIELDS | {"expected_version"},
            "operator_policy_invalid",
            422,
        )
        require(
            positive_int(body["expected_version"], 2**31 - 1, 0)
            and type(body["enabled"]) is bool
            and positive_int(body["max_instances"], 128, 0)
            and positive_int(body["max_physical_gpus"], 1024, 0)
            and positive_int(body["max_hourly_cost_microusd"], 9_000_000_000_000, 0)
            and positive_int(body["idle_shutdown_seconds"], 86400, 1)
            and positive_int(body["max_ttl_seconds"], 14400, 120),
            "operator_policy_limits_invalid",
            422,
        )
        require(
            not body["enabled"]
            or min(
                body["max_instances"],
                body["max_physical_gpus"],
                body["max_hourly_cost_microusd"],
            )
            > 0,
            "operator_enabled_policy_requires_limits",
            422,
        )
        with self.repo.transaction() as connection:
            gate = self.repo._locked(
                connection, select(capacity_gate).where(capacity_gate.c.id == "global")
            )
            require(gate is not None, "operator_global_capacity_not_initialized")
            row = self.repo._locked(
                connection,
                select(operator_policy).where(operator_policy.c.id == "global"),
            )
            require(
                (row["version"] if row else 0) == body["expected_version"],
                "operator_policy_version_conflict",
            )
            payload = {key: body[key] for key in POLICY_FIELDS}
            value = dict(
                version=body["expected_version"] + 1,
                payload=payload,
                actor=actor,
                updated_at=self.repo.clock(),
            )
            if row:
                connection.execute(
                    update(operator_policy)
                    .where(operator_policy.c.id == "global")
                    .values(**value)
                )
            else:
                connection.execute(insert(operator_policy).values(id="global", **value))
            # Explicit global maximum update; never touches budgets, original TTLs, reservations or jobs.
            connection.execute(
                update(capacity_gate)
                .where(capacity_gate.c.id == "global")
                .values(
                    max_instances=body["max_instances"],
                    max_physical_gpus=body["max_physical_gpus"],
                )
            )
            self.repo._emit(
                connection,
                "operator.capacity.policy_updated",
                "global",
                {"actor": actor, "version": value["version"]},
            )
        return {"policy": {"version": value["version"], **payload}}

    def _committed_capacity(self, connection):
        """Include pending manual commands and real cross-pool resources once.

        The repository's reserve barrier still independently enforces total real
        capacity. An unpriced existing rental blocks a new hourly-cap promise.
        """
        usage = self.repo._global_usage(connection)
        nodes = list(connection.execute(select(operator_nodes)).mappings())
        nodes_by_id = {row["intent_id"]: row for row in nodes}
        hourly = 0
        unknown = False
        known_resources = set()
        for row in connection.execute(
            select(instance_intents).where(instance_intents.c.state != "destroyed")
        ).mappings():
            if manually_reviewed_inactive(connection, row):
                continue
            known_resources.add(
                (row["provider"], row["provider_instance_id"] or "intent:" + row["id"])
            )
            managed = nodes_by_id.get(row["id"])
            if managed is None:
                unknown = True
            else:
                hourly += managed["payload"]["hourly_cost_microusd"]
        unknown = unknown or bool(set(usage["resources"]) - known_resources)
        pending_nodes = pending_gpus = 0
        for cmd in connection.execute(
            select(operator_commands).where(
                operator_commands.c.kind == "start",
                operator_commands.c.state.in_(ACTIVE_COMMANDS),
            )
        ).mappings():
            count = sum(node["command_id"] == cmd["id"] for node in nodes)
            remaining = max(0, cmd["payload"]["selection"]["node_count"] - count)
            pending_nodes += remaining
            pending_gpus += remaining * cmd["payload"]["selection"]["gpu_count"]
            hourly += remaining * cmd["payload"]["hourly_cost_microusd"]
        return {
            "instances": usage["instances"] + pending_nodes,
            "physical_gpus": usage["physical_gpus"] + pending_gpus,
            "allocated_instances": usage["instances"],
            "allocated_physical_gpus": usage["physical_gpus"],
            "hourly": hourly,
            "unpriced": unknown,
        }

    def preview(self, principal, body):
        actor = self.authorize(principal)
        chosen = selection(body)
        blockers = []
        try:
            binding = self.registry.resolve(chosen)
        except OperatorError as error:
            binding = None
            blockers.append({"code": error.code})
        with self.repo.transaction() as connection:
            policy = self._policy(connection)
            now = self.repo.clock()
            usage = self._committed_capacity(connection)
            if not policy["enabled"]:
                blockers.append({"code": "operator_capacity_disabled"})
            if chosen["ttl_seconds"] > policy["max_ttl_seconds"]:
                blockers.append({"code": "operator_ttl_limit"})
            if usage["instances"] + chosen["node_count"] > policy["max_instances"]:
                blockers.append({"code": "operator_instance_limit"})
            if (
                usage["physical_gpus"] + chosen["node_count"] * chosen["gpu_count"]
                > policy["max_physical_gpus"]
            ):
                blockers.append({"code": "operator_gpu_limit"})
            if usage["unpriced"]:
                blockers.append({"code": "operator_unpriced_existing_capacity"})
            hourly = reservation = None
            selected_offer = None
            if binding:
                if binding.pool in paused_capacity_pools(connection):
                    blockers.append({"code": "operator_pool_paused"})
                chosen = {**chosen, "filters": binding.filters}
                hourly = binding.hourly_cost_microusd * chosen["node_count"]
                reservation = (
                    binding.reservation_per_node_microusd * chosen["node_count"]
                )
                if not binding.enabled:
                    blockers.append({"code": "operator_deployment_not_qualified"})
                if chosen["ttl_seconds"] < binding.min_ttl_seconds:
                    blockers.append({"code": "operator_ttl_below_provider_minimum"})
                if now + chosen["ttl_seconds"] > binding.expires_at:
                    blockers.append({"code": "operator_authority_expiring"})
                if usage["hourly"] + hourly > policy["max_hourly_cost_microusd"]:
                    blockers.append({"code": "operator_hourly_cost_limit"})
                if binding.reservation_per_node_microusd < math.ceil(
                    binding.hourly_cost_microusd * chosen["ttl_seconds"] / 3600
                ):
                    blockers.append({"code": "operator_reservation_insufficient"})
                if chosen.get("offer_id"):
                    if (
                        self.registry.inventory_required
                        and not exact_offer_controller_ready(connection, now)
                    ):
                        blockers.append(
                            {"code": "operator_exact_offer_controller_unavailable"}
                        )
                    try:
                        from .capacity_candidates import resolve_selected_offer

                        selected_offer = resolve_selected_offer(
                            connection, self.registry, chosen, now
                        )
                    except OperatorError as error:
                        if {"code": error.code} not in blockers:
                            blockers.append({"code": error.code})
                elif self.registry.inventory_required:
                    inventory = inventory_projection(connection, binding, now)
                    if inventory["status"] != "available":
                        blockers.append({"code": inventory["reason_code"]})
            public = {
                "preview_id": str(uuid.uuid4()),
                "expires_at": now + 120,
                "policy_version": policy["version"],
                "selection": chosen,
                "can_start": not blockers,
                "blockers": blockers,
                "estimated_hourly_cost_microusd": hourly,
                "reservation_microusd": reservation,
                "configuration_id": binding.configuration_id if binding else None,
                "minimum_ttl_seconds": binding.min_ttl_seconds if binding else None,
                "recipe_ids": list(binding.recipe_ids) if binding else [],
            }
            private = {
                **public,
                "binding_id": binding.binding_id if binding else None,
                "binding_hash": command_binding_fingerprint(binding, chosen)
                if binding
                else None,
            }
            if selected_offer is not None:
                # This quote is explicit; the protected ceiling/reservation above
                # remains conservative and is not silently raised by stock.
                public["selected_offer"] = selected_offer
                private["selected_offer"] = selected_offer
                private["offer_fingerprint"] = offer_fingerprint(selected_offer)
            connection.execute(
                insert(operator_previews).values(
                    id=public["preview_id"],
                    actor=actor,
                    payload=private,
                    created_at=now,
                    expires_at=public["expires_at"],
                )
            )
        return public

    def _existing_command(self, connection, actor, key, hashed):
        require(safe_id(key), "operator_idempotency_key_invalid", 422)
        existing = (
            connection.execute(
                select(operator_commands).where(
                    operator_commands.c.actor == actor,
                    operator_commands.c.idempotency_key == key,
                )
            )
            .mappings()
            .first()
        )
        if existing:
            require(existing["request_hash"] == hashed, "operator_idempotency_conflict")
            nodes = list(
                connection.execute(
                    select(operator_nodes).where(
                        operator_nodes.c.command_id == existing["id"]
                    )
                ).mappings()
            )
            result = command_public(existing, nodes)
            if existing["kind"] != "start":
                result["node_ids"] = [existing["payload"]["node_id"]]
            return {"operation": result}

    def start(self, principal, body, key):
        actor = self.authorize(principal)
        require(
            isinstance(body, dict)
            and set(body) == {"preview_id"}
            and safe_id(body["preview_id"]),
            "operator_start_invalid",
            422,
        )
        hashed = request_hash({"kind": "start", "body": body})
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            existing = self._existing_command(connection, actor, key, hashed)
            if existing:
                return existing
            consumed = connection.execute(
                select(operator_commands.c.id).where(
                    operator_commands.c.kind == "start",
                    operator_commands.c.payload["preview_id"].as_string()
                    == body["preview_id"],
                )
            ).first()
            require(consumed is None, "operator_preview_already_confirmed")
            row = (
                connection.execute(
                    select(operator_previews).where(
                        operator_previews.c.id == body["preview_id"],
                        operator_previews.c.actor == actor,
                    )
                )
                .mappings()
                .first()
            )
            require(row is not None, "operator_preview_not_found", 404)
            value = row["payload"]
            policy = self._policy(connection)
            now = self.repo.clock()
            require(row["expires_at"] > now, "operator_preview_expired")
            require(value["can_start"], "operator_preview_blocked")
            require(
                policy["enabled"] and policy["version"] == value["policy_version"],
                "operator_policy_changed",
            )
            binding = self.registry.get(value["binding_id"])
            chosen = value["selection"]
            require(
                binding.enabled
                and command_binding_fingerprint(binding, chosen)
                == value["binding_hash"],
                "operator_binding_changed",
            )
            require(
                binding.pool not in paused_capacity_pools(connection),
                "operator_pool_paused",
            )
            if chosen.get("offer_id"):
                require(
                    not self.registry.inventory_required
                    or exact_offer_controller_ready(connection, now),
                    "operator_exact_offer_controller_unavailable",
                )
                from .capacity_candidates import resolve_selected_offer

                offer = resolve_selected_offer(connection, self.registry, chosen, now)
                require(
                    offer_fingerprint(offer) == value.get("offer_fingerprint"),
                    "operator_offer_changed",
                )
            elif self.registry.inventory_required:
                inventory = inventory_projection(connection, binding, now)
                require(inventory["status"] == "available", inventory["reason_code"])
            require(
                chosen["ttl_seconds"] >= binding.min_ttl_seconds,
                "operator_ttl_below_provider_minimum",
            )
            require(
                now + chosen["ttl_seconds"] <= binding.expires_at,
                "operator_authority_expiring",
            )
            usage = self._committed_capacity(connection)
            require(not usage["unpriced"], "operator_unpriced_existing_capacity")
            require(
                usage["instances"] + chosen["node_count"] <= policy["max_instances"],
                "operator_instance_limit",
            )
            require(
                usage["physical_gpus"] + chosen["node_count"] * chosen["gpu_count"]
                <= policy["max_physical_gpus"],
                "operator_gpu_limit",
            )
            require(
                usage["hourly"] + value["estimated_hourly_cost_microusd"]
                <= policy["max_hourly_cost_microusd"],
                "operator_hourly_cost_limit",
            )
            payload = {
                "selection": chosen,
                "preview_id": row["id"],
                "policy_version": policy["version"],
                "binding_id": binding.binding_id,
                "binding_hash": value["binding_hash"],
                "hourly_cost_microusd": binding.hourly_cost_microusd,
                "hard_deadline": now + chosen["ttl_seconds"],
            }
            if chosen.get("offer_id"):
                payload.update(
                    selected_offer=value["selected_offer"],
                    offer_fingerprint=value["offer_fingerprint"],
                )
            command = dict(
                id=str(uuid.uuid4()),
                actor=actor,
                idempotency_key=key,
                request_hash=hashed,
                kind="start",
                state="accepted",
                payload=payload,
                reason_code=None,
                created_at=now,
                updated_at=now,
            )
            connection.execute(insert(operator_commands).values(**command))
            self.repo._emit(
                connection,
                "operator.capacity.start_requested",
                command["id"],
                {"actor": actor, "operation_id": command["id"]},
            )
        return {"operation": command_public(command)}

    def node_command(self, principal, node_id, body, key, kind):
        actor = self.authorize(principal)
        require(
            kind in {"drain", "stop"}
            and safe_id(node_id)
            and isinstance(body, dict)
            and set(body) == {"expected_version"}
            and isinstance(body["expected_version"], str),
            "operator_node_command_invalid",
            422,
        )
        hashed = request_hash({"kind": kind, "node_id": node_id, "body": body})
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            existing = self._existing_command(connection, actor, key, hashed)
            if existing:
                return existing
            node = self.repo._locked(
                connection,
                select(operator_nodes).where(operator_nodes.c.intent_id == node_id),
            )
            intent = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == node_id),
            )
            require(
                node is not None and intent is not None, "operator_node_not_found", 404
            )
            require(
                node_version(intent, node) == body["expected_version"],
                "operator_node_version_conflict",
            )
            require(
                not manually_reviewed_inactive(connection, intent),
                "operator_node_manually_reviewed",
            )
            require(intent["state"] != "destroyed", "operator_node_already_destroyed")
            now = self.repo.clock()
            desired = (
                "stopped"
                if kind == "stop" or node["desired_state"] == "stopped"
                else "drained"
            )
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(
                    desired_state=desired,
                    payload=advance_command_revision(node["payload"]),
                    updated_at=now,
                )
            )
            command = dict(
                id=str(uuid.uuid4()),
                actor=actor,
                idempotency_key=key,
                request_hash=hashed,
                kind=kind,
                state="accepted",
                payload={
                    "node_id": node_id,
                    "expected_version": body["expected_version"],
                },
                reason_code=None,
                created_at=now,
                updated_at=now,
            )
            connection.execute(insert(operator_commands).values(**command))
            self.repo._emit(
                connection,
                "operator.capacity." + kind + "_requested",
                command["id"],
                {"actor": actor, "node_id": node_id},
            )
        return {"operation": {**command_public(command), "node_ids": [node_id]}}

    def _review_blocker(
        self, connection, intent, node, now, *, lock=False, local_releases=None
    ):
        if intent["provider"] not in {"lium", "targon"}:
            return "operator_manual_review_provider_unsupported"
        if (
            intent["state"]
            not in {
                "creating",
                "creation_unknown",
                "starting",
                "ready",
                "busy",
                "draining",
                "destroying",
            }
            or intent["provider_instance_id"] is None
            and intent["state"] not in {"creating", "creation_unknown"}
            or intent["provider_instance_id"] is not None
            and not safe_id(intent["provider_instance_id"])
        ):
            return "operator_manual_review_instance_unconfirmed"
        if intent["provider_instance_id"] is None:
            # Ambiguous creation has no physical identity to match. Do not
            # invent one or retire actors by guessing: the entire original
            # pool must have no execution/ownership obligations instead.
            worker_query = select(registered_workers).where(
                registered_workers.c.pool == intent["pool"]
            )
            if lock:
                worker_query = worker_query.order_by(
                    registered_workers.c.id
                ).with_for_update()
            workers = list(connection.execute(worker_query).mappings())
            if any(w["state"] != "retired" or w["current_job_id"] for w in workers):
                return "operator_manual_review_worker_active"
            worker_ids = [w["id"] for w in workers]
            device_query = select(registered_devices).where(
                or_(
                    registered_devices.c.worker_id.in_(worker_ids),
                    (registered_devices.c.provider == intent["provider"])
                    & registered_devices.c.instance_id.is_(None),
                )
            )
            if lock:
                device_query = device_query.order_by(
                    registered_devices.c.gpu_id
                ).with_for_update()
            if any(
                d["state"] != "released"
                for d in connection.execute(device_query).mappings()
            ):
                return "operator_manual_review_device_unreleased"
            job_query = select(jobs.c.id, jobs.c.status).where(
                jobs.c.pool == intent["pool"]
            )
            if lock:
                job_query = job_query.order_by(jobs.c.id).with_for_update()
            job_rows = list(connection.execute(job_query).mappings())
            if any(
                j["status"] not in {"succeeded", "failed", "cancelled"}
                for j in job_rows
            ):
                return "operator_manual_review_worker_active"
            attempt_query = select(attempts).where(
                or_(
                    attempts.c.job_id.in_([j["id"] for j in job_rows]),
                    attempts.c.worker_id.in_(worker_ids),
                )
            )
            if lock:
                attempt_query = attempt_query.order_by(attempts.c.id).with_for_update()
            if any(
                h["status"] not in {"succeeded", "failed", "cancelled"}
                or (
                    h["submission_started_at"] is not None
                    or h["upstream_task_id"] is not None
                )
                and h["upstream_stopped"] != 1
                for h in connection.execute(attempt_query).mappings()
            ):
                return "operator_manual_review_attempt_unsafe"
            return None
        # An operator can stop a machine in the provider dashboard. Exact human
        # attestation does not depend on a live retired controller or an earlier
        # platform stop request; local execution obligations still gate closure.
        worker_query = (
            select(registered_workers)
            .where(
                registered_workers.c.provider == intent["provider"],
                registered_workers.c.instance_id == intent["provider_instance_id"],
            )
            .order_by(registered_workers.c.id)
        )
        if lock:
            worker_query = worker_query.with_for_update()
        workers = list(connection.execute(worker_query).mappings())
        if any(
            w["current_job_id"] or w["state"] != "retired" and w["expires_at"] > now
            for w in workers
        ):
            return "operator_manual_review_worker_active"
        worker_ids = [w["id"] for w in workers]
        exact_device = (registered_devices.c.provider == intent["provider"]) & (
            registered_devices.c.instance_id == intent["provider_instance_id"]
        )
        # Include devices owned by these workers outside the exact node too:
        # inconsistent/cross-node ownership must block, never be released.
        device_query = (
            select(registered_devices)
            .where(or_(exact_device, registered_devices.c.worker_id.in_(worker_ids)))
            .order_by(
                registered_devices.c.provider,
                registered_devices.c.instance_id,
                registered_devices.c.gpu_id,
            )
        )
        if lock:
            device_query = device_query.with_for_update()
        devices = list(connection.execute(device_query).mappings())
        if any(
            d["state"] != "released"
            and (
                d["worker_id"] not in worker_ids
                or d["provider"] != intent["provider"]
                or d["instance_id"] != intent["provider_instance_id"]
            )
            for d in devices
        ):
            return "operator_manual_review_device_unreleased"
        history = []
        if workers:
            attempt_query = (
                select(attempts)
                .where(attempts.c.worker_id.in_(worker_ids))
                .order_by(attempts.c.id)
            )
            history = list(connection.execute(attempt_query).mappings())
            job_ids = [h["job_id"] for h in history]
            job_query = (
                select(jobs.c.id, jobs.c.status, jobs.c.lease_worker_id)
                .where(
                    or_(jobs.c.id.in_(job_ids), jobs.c.lease_worker_id.in_(worker_ids))
                )
                .order_by(jobs.c.id)
            )
            if lock:
                job_query = job_query.with_for_update()
            job_rows = {j["id"]: j for j in connection.execute(job_query).mappings()}
            if lock:
                # Follow worker -> job -> attempt locking used by WorkerControl.
                history = list(
                    connection.execute(attempt_query.with_for_update()).mappings()
                )
            if any(
                j["lease_worker_id"] in worker_ids
                and j["status"] not in {"succeeded", "failed", "cancelled"}
                for j in job_rows.values()
            ):
                return "operator_manual_review_worker_active"
            if any(
                h["job_id"] not in job_rows
                or job_rows[h["job_id"]]["status"]
                not in {"succeeded", "failed", "cancelled"}
                or h["status"] not in {"succeeded", "failed", "cancelled"}
                or (
                    h["submission_started_at"] is not None
                    or h["upstream_task_id"] is not None
                )
                and h["upstream_stopped"] != 1
                for h in history
            ):
                return "operator_manual_review_attempt_unsafe"
        release_candidates = [
            w
            for w in workers
            if w["state"] != "retired"
            or any(
                d["worker_id"] == w["id"] and d["state"] != "released" for d in devices
            )
        ]
        if release_candidates:
            from .control import WorkerSpec, worker_spec_payload

            try:
                binding = self.registry.get(node["binding_id"])
                if (
                    binding.fingerprint != node["binding_hash"]
                    or binding.pool != intent["pool"]
                    or binding.launch.provider != intent["provider"]
                ):
                    return "operator_manual_review_worker_binding_unconfirmed"
                for worker in release_candidates:
                    raw = worker["spec"]
                    spec = WorkerSpec(
                        **{
                            **raw,
                            "physical_gpu_ids": tuple(raw["physical_gpu_ids"]),
                            "recipe_ids": tuple(raw["recipe_ids"]),
                        }
                    )
                    if (
                        request_hash(worker_spec_payload(spec)) != worker["spec_hash"]
                        or spec.worker_id != worker["id"]
                        or spec.provider != intent["provider"]
                        or spec.instance_id != intent["provider_instance_id"]
                        or spec.pool != binding.pool
                        or worker["pool"] != binding.pool
                        or spec.backend != "wangp-worker"
                        or spec.model_id != binding.model_id
                        or spec.configuration_id != binding.configuration_id
                        or spec.engine_manifest_digest != binding.engine_manifest_digest
                        or set(spec.recipe_ids) != set(binding.recipe_ids)
                        or worker["state"]
                        not in {
                            "registered",
                            "ready",
                            "busy",
                            "draining",
                            "unknown",
                            "retired",
                        }
                        or not math.isfinite(worker["expires_at"])
                        or worker["expires_at"] > now
                        or type(worker["fence"]) is not int
                        or worker["fence"] < 0
                    ):
                        return "operator_manual_review_worker_binding_unconfirmed"
                    held = [
                        d
                        for d in devices
                        if d["worker_id"] == worker["id"] and d["state"] != "released"
                    ]
                    if any(
                        d["gpu_id"] not in spec.physical_gpu_ids
                        or d["state"] not in {"owned", "reserved"}
                        for d in held
                    ):
                        return "operator_manual_review_device_unreleased"
                    if local_releases is not None:
                        local_releases.append(
                            {
                                "worker_id": worker["id"],
                                "previous_state": worker["state"],
                                "previous_fence": worker["fence"],
                                "next_fence": worker["fence"] + 1,
                                "expires_at": worker["expires_at"],
                                "spec_hash": worker["spec_hash"],
                                "devices": [
                                    {
                                        "gpu_id": d["gpu_id"],
                                        "previous_state": d["state"],
                                    }
                                    for d in held
                                ],
                                "terminal_attempt_ids": [
                                    h["id"]
                                    for h in history
                                    if h["worker_id"] == worker["id"]
                                ],
                            }
                        )
            except (OperatorError, KeyError, TypeError, ValueError):
                return "operator_manual_review_worker_binding_unconfirmed"
        return None

    def manual_review(self, principal, node_id, body, key):
        actor = self.authorize(principal)
        require(
            safe_id(node_id)
            and isinstance(body, dict)
            and set(body)
            == {
                "expected_version",
                "provider_instance_id",
                "account_absent",
                "no_continuing_charge",
            }
            and isinstance(body["expected_version"], str)
            and (
                body["provider_instance_id"] is None
                or safe_id(body["provider_instance_id"])
            )
            and body["account_absent"] is True
            and body["no_continuing_charge"] is True,
            "operator_manual_review_attestation_required",
            422,
        )
        hashed = request_hash(
            {"kind": "manual_review", "node_id": node_id, "body": body}
        )
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            existing = self._existing_command(connection, actor, key, hashed)
            if existing:
                return existing
            node = self.repo._locked(
                connection,
                select(operator_nodes).where(operator_nodes.c.intent_id == node_id),
            )
            intent = self.repo._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == node_id),
            )
            require(
                node is not None and intent is not None, "operator_node_not_found", 404
            )
            require(
                node_version(intent, node) == body["expected_version"],
                "operator_node_version_conflict",
            )
            require(
                body["provider_instance_id"] == intent["provider_instance_id"],
                "operator_manual_review_identity_mismatch",
            )
            require(
                not manually_reviewed_inactive(connection, intent),
                "operator_node_manually_reviewed",
            )
            now = self.repo.clock()
            local_releases = []
            blocker = self._review_blocker(
                connection, intent, node, now, lock=True, local_releases=local_releases
            )
            require(blocker is None, blocker or "operator_manual_review_blocked")
            stop = next(
                (
                    command
                    for command in connection.execute(
                        select(operator_commands)
                        .where(operator_commands.c.kind == "stop")
                        .order_by(operator_commands.c.created_at.desc())
                    ).mappings()
                    if command["payload"].get("node_id") == node_id
                ),
                None,
            )
            command = dict(
                id=str(uuid.uuid4()),
                actor=actor,
                idempotency_key=key,
                request_hash=hashed,
                kind="manual_review",
                state="completed",
                payload={"node_id": node_id, **body},
                reason_code="operator_removal_manually_reviewed",
                created_at=now,
                updated_at=now,
            )
            review = {
                "schema_version": 1,
                "state": "manually_reviewed",
                "intent_id": node_id,
                "instance_id": intent["provider_instance_id"],
                "operation_id": command["id"],
                "actor": actor,
                "account_absent": True,
                "no_continuing_charge": True,
                "deadline": intent["hard_deadline"],
                "provider": intent["provider"],
                "original_intent_state": intent["state"],
                "removal_source": "platform_stop" if stop else "operator_attestation",
                "stop_operation_id": stop["id"] if stop else None,
            }
            if local_releases:
                review["local_execution_release"] = {
                    "state": "local_ownership_released",
                    "provider_removal_confirmed": False,
                    "billing_settled": False,
                    "workers": local_releases,
                }
                for released in local_releases:
                    connection.execute(
                        update(registered_workers)
                        .where(registered_workers.c.id == released["worker_id"])
                        .values(
                            state="retired",
                            current_job_id=None,
                            drain_requested=1,
                            fence=released["next_fence"],
                            updated_at=now,
                        )
                    )
                    for device in released["devices"]:
                        connection.execute(
                            update(registered_devices)
                            .where(
                                registered_devices.c.provider == intent["provider"],
                                registered_devices.c.instance_id
                                == intent["provider_instance_id"],
                                registered_devices.c.worker_id == released["worker_id"],
                                registered_devices.c.gpu_id == device["gpu_id"],
                            )
                            .values(state="released")
                        )
                    self.repo._emit(
                        connection,
                        "operator.capacity.local_worker_retired",
                        released["worker_id"],
                        {
                            "actor": actor,
                            "operation_id": command["id"],
                            "intent_id": node_id,
                            "instance_id": intent["provider_instance_id"],
                            **released,
                        },
                    )
            connection.execute(insert(operator_commands).values(**command))
            connection.execute(
                insert(scaler_receipts).values(
                    id=str(uuid.uuid4()),
                    intent_id=node_id,
                    operation="manual_review",
                    observed_at=now,
                    facts=canonical(review),
                )
            )
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(
                    payload={
                        **advance_command_revision(node["payload"]),
                        "manual_review": review,
                    },
                    desired_state="stopped",
                    runtime_state="manually_reviewed",
                    updated_at=now,
                )
            )
            # Existing stop intent is completed by human review, not provider proof.
            for stop in connection.execute(
                select(operator_commands).where(
                    operator_commands.c.kind == "stop",
                    operator_commands.c.state.in_(ACTIVE_COMMANDS),
                )
            ).mappings():
                if stop["payload"].get("node_id") == node_id:
                    connection.execute(
                        update(operator_commands)
                        .where(operator_commands.c.id == stop["id"])
                        .values(
                            state="completed",
                            reason_code="operator_removal_manually_reviewed",
                            updated_at=now,
                        )
                    )
            self.repo._emit(
                connection,
                "operator.capacity.removal_manually_reviewed",
                node_id,
                review,
            )
        return {"operation": {**command_public(command), "node_ids": [node_id]}}

    def state(self, principal):
        actor = self.authorize(principal)
        now = self.repo.clock()
        with self.repo.engine.connect() as connection:
            policy = self._policy(connection)
            controller = (
                connection.execute(
                    select(operator_heartbeats).where(
                        operator_heartbeats.c.id == "global"
                    )
                )
                .mappings()
                .first()
            )
            worker_rows = list(
                connection.execute(select(registered_workers)).mappings()
            )
            from .worker_admission import worker_window_reason

            controller_fresh = bool(
                controller
                and controller["state"] == "running"
                and 0 <= now - controller["observed_at"] <= CONTROLLER_FRESH_SECONDS
            )
            managed = [
                node
                for node in connection.execute(select(operator_nodes)).mappings()
                if node["payload"].get("capacity_backend") != "dstack-v1"
            ]
            intents = {
                row["id"]: row
                for row in connection.execute(select(instance_intents)).mappings()
            }
            node_profiles = {
                (
                    intents[node["intent_id"]]["provider"],
                    intents[node["intent_id"]]["provider_instance_id"],
                ): node["payload"].get("selection", {}).get("runtime_profile_id")
                for node in managed
                if node["intent_id"] in intents
            }
            worker_admission = {
                worker["id"]: worker_window_reason(
                    connection,
                    worker,
                    now,
                    deployment_profile_id=node_profiles.get(
                        (worker["provider"], worker["instance_id"])
                    ),
                )
                for worker in worker_rows
            }
            actions = {
                row["intent_id"]: row
                for row in connection.execute(select(scaler_actions)).mappings()
            }
            commands = list(
                connection.execute(
                    select(operator_commands)
                    .where(
                        ~operator_commands.c.kind.startswith("dstack_", autoescape=True)
                    )
                    .order_by(operator_commands.c.created_at.desc())
                    .limit(100)
                ).mappings()
            )
            removal_checks = dict(
                connection.execute(
                    select(
                        scaler_receipts.c.intent_id,
                        func.max(scaler_receipts.c.observed_at),
                    )
                    .where(scaler_receipts.c.operation == "removal_check_started")
                    .group_by(scaler_receipts.c.intent_id)
                ).all()
            )
            manual_reviews = {
                row["id"]: manually_reviewed_inactive(connection, row)
                for row in intents.values()
            }
            extension_values = {
                row["intent_id"]: self._extension_value(
                    connection, intents[row["intent_id"]], row, 60
                )
                for row in managed
                if row["intent_id"] in intents
            }
            review_blockers = {
                row["intent_id"]: self._review_blocker(
                    connection, intents[row["intent_id"]], row, now
                )
                for row in managed
                if row["intent_id"] in intents
            }
            stop_requests = {}
            for command in connection.execute(
                select(operator_commands)
                .where(operator_commands.c.kind == "stop")
                .order_by(operator_commands.c.created_at.desc())
            ).mappings():
                stop_requests[command["payload"].get("node_id")] = command["created_at"]
            usage = self._committed_capacity(connection)
            running = connection.execute(
                select(func.count())
                .select_from(jobs)
                .where(jobs.c.status.in_(("running", "submitting", "claimed")))
            ).scalar_one()
            waiting = connection.execute(
                select(func.count())
                .select_from(jobs)
                .where(jobs.c.status.in_(("waiting_capacity", "queued")))
            ).scalar_one()
        nodes = []
        for row in managed:
            intent = intents.get(row["intent_id"])
            if not intent:
                continue
            payload = row["payload"]
            observed = actions.get(intent["id"], {}).get("last_observed_at")
            action = actions.get(intent["id"], {})
            requested_at = action.get("destroy_started_at")
            if requested_at is None:
                requested_at = stop_requests.get(intent["id"])
            stale = observed is None or not 0 <= now - observed <= 60
            slots = []
            for worker in worker_rows:
                if (
                    worker["provider"] != intent["provider"]
                    or worker["instance_id"] != intent["provider_instance_id"]
                ):
                    continue
                spec = worker["spec"]
                slots.append(
                    {
                        "id": worker["id"],
                        "gpu_ids": spec["physical_gpu_ids"],
                        "state": worker["state"],
                        "stale": worker["expires_at"] <= now,
                        "current_job_id": worker["current_job_id"],
                        "admission_allowed": bool(
                            controller_fresh
                            and policy["enabled"]
                            and worker["state"] == "ready"
                            and not worker["drain_requested"]
                            and worker["current_job_id"] is None
                            and worker["expires_at"] > now
                            and worker["updated_at"] <= now
                            and worker["spec_hash"] == request_hash(spec)
                            and worker_admission[worker["id"]] is None
                        ),
                        "admission_reason_code": worker_admission[worker["id"]],
                        "configuration_id": spec["configuration_id"],
                        "recipe_ids": spec["recipe_ids"],
                        "engine_manifest_digest": spec.get("engine_manifest_digest"),
                    }
                )
            review = manual_reviews.get(intent["id"])
            allowed = intent["state"] != "destroyed" and not review
            availability = {
                "allowed": allowed,
                "blockers": []
                if allowed
                else [
                    {
                        "code": "operator_node_manually_reviewed"
                        if review
                        else "operator_node_already_destroyed"
                    }
                ],
            }
            review_blocker = (
                "operator_node_manually_reviewed"
                if review
                else review_blockers.get(intent["id"])
            )
            bootstrap = public_bootstrap(payload.get("bootstrap"))
            extension = extension_values[intent["id"]]
            removal = removal_confirmation(
                intent,
                action,
                removal_checks.get(intent["id"]),
                requested_at,
                now,
                review,
            )
            record_group = (
                "history"
                if review or intent["state"] == "destroyed"
                else "pending_review"
                if intent["state"] == "creation_unknown"
                or row["runtime_state"]
                in {"blocked", "failed", "observation_failed", "bootstrap_unconfigured"}
                or removal
                and removal["state"] == "overdue"
                else "current"
            )
            nodes.append(
                {
                    "id": intent["id"],
                    "version": node_version(intent, row),
                    "provider": intent["provider"],
                    "provider_instance_id": intent["provider_instance_id"],
                    "state": intent["state"],
                    "runtime_state": row["runtime_state"],
                    "desired_state": row["desired_state"],
                    "record_group": record_group,
                    "review_required": record_group == "pending_review",
                    "bootstrap": bootstrap,
                    "removal_confirmation": removal,
                    "reason_code": bootstrap.get("reason_code")
                    if bootstrap and row["runtime_state"] in {"blocked", "failed"}
                    else None,
                    "gpu_count": intent["physical_gpus"],
                    "gpu_model": payload["selection"]["gpu_type"],
                    "runtime_profile_id": payload["selection"]["runtime_profile_id"],
                    "observed_at": observed,
                    "stale": stale,
                    "mode": payload["selection"]["mode"],
                    "hourly_cost_microusd": payload["hourly_cost_microusd"],
                    "hourly_cost_basis": "approved_ceiling",
                    "hard_deadline": intent["hard_deadline"],
                    "provider_safe_deadline": payload.get("lifetime", {}).get(
                        "safe_deadline"
                    ),
                    "provider_lifetime_state": payload.get("lifetime", {}).get(
                        "state", "unverified"
                    ),
                    "provider_lifetime_observed_at": payload.get("lifetime", {}).get(
                        "observed_at"
                    ),
                    "last_extension": payload.get("last_extension"),
                    "slots": slots,
                    "actions": {
                        "drain": availability,
                        "stop": availability,
                        "extend": {
                            "allowed": extension["can_extend"],
                            "blockers": extension["blockers"],
                        },
                        "manual_review": {
                            "allowed": review_blocker is None,
                            "blockers": [{"code": review_blocker}]
                            if review_blocker
                            else [],
                        },
                    },
                }
            )
        age = None if controller is None else now - controller["observed_at"]
        return {
            "schema_version": 1,
            "observed_at": now,
            "operator": {
                "account": actor,
                "permissions": {
                    key: True
                    for key in (
                        "view",
                        "start",
                        "drain",
                        "stop",
                        "extend",
                        "manual_review",
                        "update_policy",
                    )
                },
            },
            "policy": policy,
            "controller": {
                "state": controller["state"] if controller else "offline",
                "last_heartbeat_at": controller["observed_at"] if controller else None,
                "stale": age is None or not 0 <= age <= 60,
            },
            "summary": {
                "nodes_active": usage["allocated_instances"],
                "gpus_allocated": usage["allocated_physical_gpus"],
                "slots_ready": sum(
                    slot["admission_allowed"]
                    for node in nodes
                    for slot in node["slots"]
                ),
                "jobs_running": running,
                "jobs_waiting": waiting,
                "hourly_cost_microusd": None if usage["unpriced"] else usage["hourly"],
                "hourly_cost_basis": "approved_ceiling_including_pending_commands",
            },
            "nodes": nodes,
            "operations": [
                command_public(
                    row, [node for node in managed if node["command_id"] == row["id"]]
                )
                if row["kind"] == "start"
                else {**command_public(row), "node_ids": [row["payload"]["node_id"]]}
                for row in commands
            ],
        }
