"""Explicit operating-window extensions inside already funded provider authority.

No provider calls or deadline re-arming. A scheduled provider removal or immutable
watchdog cannot be extended by changing a browser timer. Such requests retain the
original node and report the concrete unsupported boundary.
"""

import math
import uuid

from sqlalchemy import insert, select, update

from .repository import (
    budget_reservations,
    instance_intents,
    request_hash,
    manually_reviewed_inactive,
)


class OperatorExtensions:
    def _extension_value(self, connection, intent, node, additional_seconds):
        from .operator_capacity import (
            node_version,
            operator_heartbeats,
            CONTROLLER_FRESH_SECONDS,
        )
        from .provider_lifetime import provider_lifetime_current

        now = self.repo.clock()
        policy = self._policy(connection)
        deadline = intent["hard_deadline"]
        new_deadline = deadline + additional_seconds
        blockers = []
        binding = None
        try:
            binding = self.registry.get(node["binding_id"])
        except ValueError:
            blockers.append({"code": "operator_binding_changed"})
        if (
            node["desired_state"] != "running"
            or intent["state"] not in {"starting", "ready", "busy"}
            or manually_reviewed_inactive(connection, intent)
        ):
            blockers.append({"code": "operator_extension_requires_active_node"})
        if not policy["enabled"]:
            blockers.append({"code": "operator_capacity_disabled"})
        heartbeat = (
            connection.execute(
                select(operator_heartbeats).where(operator_heartbeats.c.id == "global")
            )
            .mappings()
            .first()
        )
        if not (
            heartbeat
            and heartbeat["state"] == "running"
            and 0 <= now - heartbeat["observed_at"] <= CONTROLLER_FRESH_SECONDS
        ):
            blockers.append({"code": "operator_controller_unavailable"})
        if deadline <= now + 300:
            blockers.append({"code": "operator_extension_window_closed"})
        if not provider_lifetime_current(node["payload"], intent, now):
            blockers.append({"code": "operator_provider_lifetime_unverified"})
        provider_deadline = node["payload"].get("lifetime", {}).get("safe_deadline")
        valid_cap = type(provider_deadline) in (int, float) and math.isfinite(
            provider_deadline
        )
        if not valid_cap or new_deadline > provider_deadline:
            blockers.append({"code": "operator_provider_extension_unsupported"})
        if binding:
            if not binding.enabled or binding.fingerprint != node["binding_hash"]:
                blockers.append({"code": "operator_binding_changed"})
            if new_deadline > binding.expires_at:
                blockers.append({"code": "operator_authority_expiring"})
            if new_deadline - intent["created_at"] > min(
                binding.max_ttl_seconds, policy["max_ttl_seconds"]
            ):
                blockers.append({"code": "operator_ttl_limit"})
        hourly = node["payload"].get("hourly_cost_microusd")
        if type(hourly) is not int or hourly <= 0:
            blockers.append({"code": "operator_extension_quote_unknown"})
        total = (
            math.ceil(hourly * (new_deadline - intent["created_at"]) / 3600)
            if type(hourly) is int and hourly > 0
            else None
        )
        reservations = list(
            connection.execute(
                select(budget_reservations).where(
                    budget_reservations.c.reference_type == "instance",
                    budget_reservations.c.reference_id == intent["id"],
                )
            ).mappings()
        )
        covered = bool(
            binding
            and total is not None
            and total <= intent["reserved_cost_microusd"]
            and {row["account_id"] for row in reservations}
            == set(binding.budget_account_ids)
            and all(
                row["state"] == "reserved" and row["amount_microusd"] >= total
                for row in reservations
            )
        )
        if not covered:
            blockers.append({"code": "operator_extension_reservation_insufficient"})
        # One reason per constraint, with no private configuration or account IDs.
        blockers = list({b["code"]: b for b in blockers}.values())
        return {
            "node_id": intent["id"],
            "node_version": node_version(intent, node),
            "can_extend": not blockers,
            "current_deadline": deadline,
            "new_deadline": new_deadline,
            "provider_deadline": provider_deadline if valid_cap else None,
            "additional_seconds": additional_seconds,
            "estimated_incremental_cost_microusd": math.ceil(
                hourly * additional_seconds / 3600
            )
            if total is not None
            else None,
            "incremental_reservation_microusd": 0 if covered else None,
            "cost_basis": "approved_ceiling",
            "reservation_basis": "existing_instance_reservation",
            "blockers": blockers,
        }

    def extension_preview(self, principal, node_id, body):
        from .operator_capacity import (
            operator_nodes,
            operator_previews,
            require,
            safe_id,
            positive_int,
            node_version,
        )

        actor = self.authorize(principal)
        require(
            safe_id(node_id)
            and isinstance(body, dict)
            and set(body) == {"expected_version", "additional_seconds"}
            and isinstance(body["expected_version"], str)
            and positive_int(body["additional_seconds"], 14400, 60),
            "operator_extension_invalid",
            422,
        )
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
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
            value = self._extension_value(
                connection, intent, node, body["additional_seconds"]
            )
            now = self.repo.clock()
            value.update(
                preview_id=str(uuid.uuid4()),
                expires_at=now + 120,
                policy_version=self._policy(connection)["version"],
            )
            connection.execute(
                insert(operator_previews).values(
                    id=value["preview_id"],
                    actor=actor,
                    payload={"kind": "extend", "extension": value},
                    created_at=now,
                    expires_at=value["expires_at"],
                )
            )
        return value

    def extend(self, principal, node_id, body, key):
        from .operator_capacity import (
            advance_command_revision,
            operator_nodes,
            operator_previews,
            operator_commands,
            require,
            safe_id,
            node_version,
            command_public,
        )

        actor = self.authorize(principal)
        require(
            safe_id(node_id)
            and isinstance(body, dict)
            and set(body) == {"preview_id"}
            and safe_id(body["preview_id"]),
            "operator_extension_invalid",
            422,
        )
        hashed = request_hash({"kind": "extend", "node_id": node_id, "body": body})
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            existing = self._existing_command(connection, actor, key, hashed)
            if existing:
                return existing
            preview = (
                connection.execute(
                    select(operator_previews).where(
                        operator_previews.c.id == body["preview_id"],
                        operator_previews.c.actor == actor,
                    )
                )
                .mappings()
                .first()
            )
            require(
                preview is not None and preview["payload"].get("kind") == "extend",
                "operator_extension_preview_not_found",
                404,
            )
            value = preview["payload"]["extension"]
            require(
                value["node_id"] == node_id, "operator_extension_preview_not_found", 404
            )
            require(
                preview["expires_at"] > self.repo.clock(), "operator_preview_expired"
            )
            require(value["can_extend"], "operator_extension_preview_blocked")
            require(
                not connection.execute(
                    select(operator_commands.c.id).where(
                        operator_commands.c.kind == "extend",
                        operator_commands.c.payload["preview_id"].as_string()
                        == body["preview_id"],
                    )
                ).first(),
                "operator_preview_already_confirmed",
            )
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
                node_version(intent, node) == value["node_version"],
                "operator_node_version_conflict",
            )
            require(
                self._policy(connection)["version"] == value["policy_version"],
                "operator_policy_changed",
            )
            current = self._extension_value(
                connection, intent, node, value["additional_seconds"]
            )
            require(
                current["can_extend"],
                current["blockers"][0]["code"]
                if current["blockers"]
                else "operator_extension_blocked",
            )
            require(
                all(
                    current[k] == value[k]
                    for k in (
                        "current_deadline",
                        "new_deadline",
                        "estimated_incremental_cost_microusd",
                        "incremental_reservation_microusd",
                    )
                ),
                "operator_extension_quote_changed",
            )
            now = self.repo.clock()
            command = dict(
                id=str(uuid.uuid4()),
                actor=actor,
                idempotency_key=key,
                request_hash=hashed,
                kind="extend",
                state="completed",
                reason_code=None,
                payload={
                    "node_id": node_id,
                    "preview_id": value["preview_id"],
                    "extension": current,
                },
                created_at=now,
                updated_at=now,
            )
            connection.execute(insert(operator_commands).values(**command))
            # Append evidence; original selection, start command, provider identity
            # and reservation remain byte-for-byte unchanged.
            connection.execute(
                update(instance_intents)
                .where(instance_intents.c.id == node_id)
                .values(hard_deadline=current["new_deadline"], updated_at=now)
            )
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(
                    payload={
                        **advance_command_revision(node["payload"]),
                        "last_extension": {
                            **current,
                            "operation_id": command["id"],
                            "confirmed_at": now,
                        },
                    },
                    updated_at=now,
                )
            )
            self.repo._emit(
                connection,
                "operator.capacity.window_extended",
                node_id,
                {
                    "operation_id": command["id"],
                    "node_id": node_id,
                    "previous_deadline": current["current_deadline"],
                    "new_deadline": current["new_deadline"],
                    "additional_seconds": current["additional_seconds"],
                },
            )
        return {"operation": {**command_public(command), "node_ids": [node_id]}}
