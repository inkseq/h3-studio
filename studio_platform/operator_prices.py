"""Price-only overlays on the original operator policy and audit tables.

No provider calls, credential reads, runtime mutations or new accounting ledger.
Callers authorize the owner/admin and serialize writes with the capacity gate.
"""
import copy
import uuid

from sqlalchemy import insert, select, update

from .operator_capacity import operator_policy, require
from .repository import outbox, request_hash


class OperatorPrices:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings

    def _key(self, profile):
        return "price:" + request_hash({"tenant": self.settings.tenant_id, "profile": profile["id"]})[:24]

    def read(self, connection, profile):
        row = connection.execute(select(operator_policy).where(
            operator_policy.c.id == self._key(profile))).mappings().first()
        binding = request_hash(profile)
        value = {"tenant_id": self.settings.tenant_id, "profile_id": profile["id"],
                 "owner_id": profile["owner_id"], "profile_binding": binding,
                 "max_price_microusd": profile["max_price_microusd"],
                 "price_ceiling_microusd": profile["max_price_microusd"]}
        version = 0
        if row:
            stored = row["payload"]
            require(isinstance(stored, dict) and set(stored) == set(value) and
                all(stored[key] == value[key] for key in
                    ("tenant_id", "profile_id", "owner_id", "profile_binding")),
                "dstack_price_policy_binding_changed")
            value, version = copy.deepcopy(stored), row["version"]
        require(type(version) is int and version >= (1 if row else 0) and
            all(type(value[key]) is int and 1 <= value[key] <= 9_000_000_000_000
                for key in ("max_price_microusd", "price_ceiling_microusd")) and
            value["max_price_microusd"] <= value["price_ceiling_microusd"],
            "dstack_price_policy_invalid")
        return {"id": profile["id"], "owner_id": profile["owner_id"],
            "max_price_microusd": value["max_price_microusd"],
            "price_ceiling_microusd": value["price_ceiling_microusd"],
            "snapshot": request_hash({"version": version, "value": value})}, value, version

    def update(self, profile, body, principal):
        require(isinstance(body, dict) and set(body) ==
            {"expected_snapshot", "max_price_microusd", "price_ceiling_microusd"} and
            isinstance(body["expected_snapshot"], str) and len(body["expected_snapshot"]) == 64 and
            all(type(body[key]) is int and 1 <= body[key] <= 9_000_000_000_000 for key in
                ("max_price_microusd", "price_ceiling_microusd")) and
            body["max_price_microusd"] <= body["price_ceiling_microusd"],
            "dstack_price_policy_invalid", 422)
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            old, value, version = self.read(connection, profile)
            require(old["snapshot"] == body["expected_snapshot"], "dstack_price_policy_conflict")
            fields = ("max_price_microusd", "price_ceiling_microusd")
            if all(old[key] == body[key] for key in fields):
                return {"profile": old, "changed": False, "audit_event_id": None}
            value.update({key: body[key] for key in fields})
            now, event_id = self.repo.clock(), str(uuid.uuid4())
            stored = {"version": version + 1, "payload": value,
                      "actor": principal.actor_id, "updated_at": now}
            if version:
                connection.execute(update(operator_policy).where(
                    operator_policy.c.id == self._key(profile)).values(**stored))
            else:
                connection.execute(insert(operator_policy).values(id=self._key(profile), **stored))
            new, _, _ = self.read(connection, profile)
            audit = {"tenant_id": self.settings.tenant_id, "profile_id": profile["id"],
                "owner_id": profile["owner_id"], "actor_id": principal.actor_id,
                "actor_owner": principal.owner, "old": {key: old[key] for key in fields},
                "new": {key: new[key] for key in fields},
                "old_version": version, "new_version": version + 1,
                "old_snapshot": old["snapshot"], "new_snapshot": new["snapshot"]}
            connection.execute(insert(outbox).values(id=event_id,
                event_type="operator.profile_price_updated", aggregate_id=profile["id"],
                payload=audit, created_at=now))
            return {"profile": new, "changed": True, "audit_event_id": event_id}

    def audits(self, connection, limit=100):
        rows = connection.execute(select(outbox).where(
            outbox.c.event_type == "operator.profile_price_updated",
            outbox.c.payload["tenant_id"].as_string() == self.settings.tenant_id
        ).order_by(outbox.c.created_at.desc(), outbox.c.id.desc()).limit(limit)).mappings()
        fields = ("profile_id", "actor_id", "actor_owner", "old", "new", "old_snapshot", "new_snapshot",
                  "old_version", "new_version")
        return [{"id": row["id"], "created_at": row["created_at"],
                 **{key: copy.deepcopy(row["payload"][key]) for key in fields}} for row in rows]
