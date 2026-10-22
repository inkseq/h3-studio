"""Price-only overlays on the original operator policy and audit tables.

No provider calls, credential reads, runtime mutations or new accounting ledger.
Callers authorize the owner/admin and serialize writes with the capacity gate.
"""

import copy
import uuid

from sqlalchemy import insert, select, update

from .operator_capacity import OperatorError, operator_policy, require
from .repository import outbox, request_hash


def mode_price_ceiling(profile, policy):
    """Persisted operating limit; legacy rows retain their protected profile."""
    if not policy["adopted"]:
        return profile["max_price_microusd"]
    mode = profile.get("mode")
    key = mode if isinstance(mode, str) and mode in {"fl", "ref"} else "other"
    return policy["value"]["mode_hourly_ceiling_microusd"][key]


def enforce_price_limit(requested, allowed, limit, code):
    require(
        type(requested) is int
        and requested > 0
        and type(allowed) is int
        and allowed > 0
        and limit in {"profile", "mode", "admin"},
        "dstack_price_policy_invalid",
        422,
    )
    if requested > allowed:
        raise OperatorError(
            code,
            422,
            details={
                "limit": limit,
                "requested_microusd": requested,
                "allowed_microusd": allowed,
            },
        )


class OperatorPrices:
    def __init__(self, repo, settings, *, admin_defaults=None):
        self.repo, self.settings = repo, settings
        self.admin_defaults = admin_defaults

    def _key(self, profile):
        return (
            "price:"
            + request_hash(
                {"tenant": self.settings.tenant_id, "profile": profile["id"]}
            )[:24]
        )

    def read(self, connection, profile):
        row = (
            connection.execute(
                select(operator_policy).where(
                    operator_policy.c.id == self._key(profile)
                )
            )
            .mappings()
            .first()
        )
        binding = request_hash(profile)
        value = {
            "tenant_id": self.settings.tenant_id,
            "profile_id": profile["id"],
            "owner_id": profile["owner_id"],
            "profile_binding": binding,
            "max_price_microusd": profile["max_price_microusd"],
            "price_ceiling_microusd": profile["max_price_microusd"],
        }
        version = 0
        if row:
            stored = row["payload"]
            require(
                isinstance(stored, dict)
                and set(stored) == set(value)
                and all(
                    stored[key] == value[key]
                    for key in (
                        "tenant_id",
                        "profile_id",
                        "owner_id",
                        "profile_binding",
                    )
                ),
                "dstack_price_policy_binding_changed",
            )
            value, version = copy.deepcopy(stored), row["version"]
        require(
            type(version) is int
            and version >= (1 if row else 0)
            and all(
                type(value[key]) is int and 1 <= value[key] <= 9_000_000_000_000
                for key in ("max_price_microusd", "price_ceiling_microusd")
            )
            and value["max_price_microusd"] <= value["price_ceiling_microusd"],
            "dstack_price_policy_invalid",
        )
        return (
            {
                "id": profile["id"],
                "owner_id": profile["owner_id"],
                "max_price_microusd": value["max_price_microusd"],
                "price_ceiling_microusd": value["price_ceiling_microusd"],
                "snapshot": request_hash({"version": version, "value": value}),
            },
            value,
            version,
        )

    @staticmethod
    def _validate(body):
        require(
            isinstance(body, dict)
            and {"expected_snapshot", "max_price_microusd", "price_ceiling_microusd"}
            <= set(body)
            and not set(body)
            - {
                "expected_snapshot",
                "max_price_microusd",
                "price_ceiling_microusd",
                "reason",
                "expected_settings_version",
                "confirmation_digest",
            }
            and isinstance(body["expected_snapshot"], str)
            and len(body["expected_snapshot"]) == 64
            and all(
                type(body[key]) is int and 1 <= body[key] <= 9_000_000_000_000
                for key in ("max_price_microusd", "price_ceiling_microusd")
            )
            and body["max_price_microusd"] <= body["price_ceiling_microusd"],
            "dstack_price_policy_invalid",
            422,
        )

    def _preview(self, connection, profile, body):
        from .capacity_settings import confirmation_digest, consequences, read_policy

        self._validate(body)
        old, _, _ = self.read(connection, profile)
        require(
            old["snapshot"] == body["expected_snapshot"], "dstack_price_policy_conflict"
        )
        policy = read_policy(
            connection, self.settings.tenant_id, admin_defaults=self.admin_defaults
        )
        require(
            type(body.get("expected_settings_version")) is int
            and body["expected_settings_version"] == policy["version"],
            "spending_settings_changed",
        )
        fields = ("max_price_microusd", "price_ceiling_microusd")
        changes = {key: body[key] for key in fields}
        changed = [key for key in fields if old[key] != body[key]]
        require(
            all(100_000 <= body[key] <= 5_000_000 for key in changed),
            "dstack_price_policy_invalid",
            422,
        )
        profiles = {profile["id"]: {**old, "mode": profile["mode"]}}
        proposed = {profile["id"]: {**profiles[profile["id"]], **changes}}
        effects = (
            consequences(policy["value"], changed, proposed)
            if policy["adopted"]
            else [{"code": "setting_next_admission_only", "fields": changed}]
        )
        return {
            "expected_snapshot": old["snapshot"],
            "expected_settings_version": policy["version"],
            "consequence_scope": "selected_profile_repeated",
            "profile_ids": [profile["id"]],
            "changes": changes,
            "changed_fields": changed,
            "consequences": effects,
            "confirmation_required": bool(changed),
            "confirmation_digest": confirmation_digest(
                kind="profile_price",
                expected_version=policy["version"],
                changes=changes,
                profile_snapshots=profiles,
                consequences=effects,
            ),
        }

    def preview(self, profile, body):
        with self.repo.engine.connect() as connection:
            return self._preview(connection, profile, body)

    def update(self, profile, body, principal):
        from .capacity_settings import read_policy
        from .spending_limits import ADMIN_OWNER, lock_owner

        self._validate(body)
        require(
            "reason" not in body
            or isinstance(body["reason"], str)
            and 1 <= len(body["reason"]) <= 500
            and bool(body["reason"].strip()),
            "dstack_price_policy_invalid",
            422,
        )
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            lock_owner(connection, self.settings.tenant_id, ADMIN_OWNER)
            old, value, version = self.read(connection, profile)
            require(
                old["snapshot"] == body["expected_snapshot"],
                "dstack_price_policy_conflict",
            )
            fields = ("max_price_microusd", "price_ceiling_microusd")
            if all(old[key] == body[key] for key in fields):
                return {"profile": old, "changed": False, "audit_event_id": None}
            policy = read_policy(
                connection, self.settings.tenant_id, admin_defaults=self.admin_defaults
            )
            confirmation = None
            if policy["adopted"] or "expected_settings_version" in body:
                require(
                    isinstance(body.get("reason"), str)
                    and bool(body["reason"].strip())
                    and isinstance(body.get("confirmation_digest"), str),
                    "spending_confirmation_required",
                )
                confirmation = self._preview(connection, profile, body)
                require(
                    body["confirmation_digest"] == confirmation["confirmation_digest"],
                    "spending_confirmation_changed",
                )
            value.update({key: body[key] for key in fields})
            now, event_id = self.repo.clock(), str(uuid.uuid4())
            stored = {
                "version": version + 1,
                "payload": value,
                "actor": principal.actor_id,
                "updated_at": now,
            }
            if version:
                connection.execute(
                    update(operator_policy)
                    .where(operator_policy.c.id == self._key(profile))
                    .values(**stored)
                )
            else:
                connection.execute(
                    insert(operator_policy).values(id=self._key(profile), **stored)
                )
            new, _, _ = self.read(connection, profile)
            audit = {
                "tenant_id": self.settings.tenant_id,
                "profile_id": profile["id"],
                "owner_id": profile["owner_id"],
                "actor_id": principal.actor_id,
                "actor_owner": principal.owner,
                "old": {key: old[key] for key in fields},
                "new": {key: new[key] for key in fields},
                "old_version": version,
                "new_version": version + 1,
                "old_snapshot": old["snapshot"],
                "new_snapshot": new["snapshot"],
            }
            if "reason" in body:
                audit["reason"] = body["reason"]
            if confirmation is not None:
                audit["confirmation"] = confirmation
            connection.execute(
                insert(outbox).values(
                    id=event_id,
                    event_type="operator.profile_price_updated",
                    aggregate_id=profile["id"],
                    payload=audit,
                    created_at=now,
                )
            )
            return {"profile": new, "changed": True, "audit_event_id": event_id}

    def audits(self, connection, limit=100):
        rows = connection.execute(
            select(outbox)
            .where(
                outbox.c.event_type == "operator.profile_price_updated",
                outbox.c.payload["tenant_id"].as_string() == self.settings.tenant_id,
            )
            .order_by(outbox.c.created_at.desc(), outbox.c.id.desc())
            .limit(limit)
        ).mappings()
        fields = (
            "profile_id",
            "actor_id",
            "actor_owner",
            "old",
            "new",
            "old_snapshot",
            "new_snapshot",
            "old_version",
            "new_version",
        )
        return [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                **{key: copy.deepcopy(row["payload"][key]) for key in fields},
                **(
                    {"reason": row["payload"]["reason"]}
                    if "reason" in row["payload"]
                    else {}
                ),
                **(
                    {"confirmation": copy.deepcopy(row["payload"]["confirmation"])}
                    if "confirmation" in row["payload"]
                    else {}
                ),
            }
            for row in rows
        ]
