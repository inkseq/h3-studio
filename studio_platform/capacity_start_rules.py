"""Audited prospective lookahead settings; no runtime or provider caller.

An administrator may prepare settings, but saving cannot activate autoscaling.
The eventual execution integration must independently preserve admission gates.
"""

import copy
import uuid

from sqlalchemy import insert, select, update

from .identity import account_identity, lock_account_gate
from .operator_capacity import operator_policy, require
from .repository import outbox, request_hash

DEFAULTS = {"job_seconds": 30, "boot_seconds": 600, "minimum_jobs": 20}
BOUNDS = {
    "job_seconds": (1, 86400),
    "boot_seconds": (1, 86400),
    "minimum_jobs": (1, 10000),
}
KIND = "start_rules_set"
EVENT = "operator.start_rules_updated"


def validate_value(value):
    require(
        type(value) is dict and set(value) == set(DEFAULTS), "start_rules_invalid", 422
    )
    for field, (minimum, maximum) in BOUNDS.items():
        require(
            type(value[field]) is int and minimum <= value[field] <= maximum,
            "start_rules_invalid",
            422,
        )
    return copy.deepcopy(value)


def _revision(value):
    return value is None or (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


class StartRules:
    def __init__(self, workspace):
        self.workspace = workspace
        self.repo = workspace.repo
        self.tenant = workspace.settings.tenant_id

    def _target(self, profile_id):
        require(isinstance(profile_id, str), "start_rules_profile_invalid", 422)
        matches = [
            p
            for p in self.workspace.service.config.get("profiles", [])
            if p.get("id") == profile_id
        ]
        require(len(matches) == 1, "start_rules_profile_invalid", 422)
        profile = matches[0]
        binding = {key: profile[key] for key in ("pool", "configuration_id", "mode")}
        identity = "rules:" + request_hash({"tenant": self.tenant, **binding})[:24]
        return identity, binding

    def _load(self, connection, identity, binding):
        row = (
            connection.execute(
                select(operator_policy).where(operator_policy.c.id == identity)
            )
            .mappings()
            .first()
        )
        if row is None:
            return {"revision": None, "version": 0, "value": copy.deepcopy(DEFAULTS)}, 0
        payload = row["payload"]
        require(
            type(payload) is dict
            and set(payload) == {"tenant_id", "binding", "value"}
            and payload["tenant_id"] == self.tenant
            and payload["binding"] == binding
            and type(row["version"]) is int
            and row["version"] >= 1,
            "start_rules_unavailable",
            503,
        )
        value = validate_value(payload["value"])
        return {
            "revision": request_hash({"version": row["version"], "payload": payload}),
            "version": row["version"],
            "value": value,
        }, row["version"]

    @staticmethod
    def _public(binding, stored):
        return {
            **copy.deepcopy(stored),
            "binding": copy.deepcopy(binding),
            "execution_connected": False,
            "applied": False,
            "keep_alive_seconds": stored["value"]["boot_seconds"],
        }

    def read(self, principal, profile_id):
        self.workspace._authorize(principal, admin=True)
        identity, binding = self._target(profile_id)
        with self.repo.engine.connect() as connection:
            stored, _ = self._load(connection, identity, binding)
        return self._public(binding, stored)

    def _body(self, body, *, write=False):
        fields = {"profile_id", "expected_revision", "value"}
        if write:
            fields |= {"reason", "confirmation_digest"}
        require(
            type(body) is dict
            and set(body) == fields
            and _revision(body["expected_revision"]),
            "start_rules_invalid",
            422,
        )
        value = validate_value(body["value"])
        if write:
            reason = body["reason"]
            require(
                isinstance(reason, str)
                and reason == reason.strip()
                and 1 <= len(reason) <= 500
                and not any(ord(c) < 32 or ord(c) == 127 for c in reason)
                and isinstance(body["confirmation_digest"], str)
                and _revision(body["confirmation_digest"]),
                "start_rules_invalid",
                422,
            )
        return value

    def _preview(self, connection, principal, body, identity, binding, value):
        old, version = self._load(connection, identity, binding)
        require(old["revision"] == body["expected_revision"], "start_rules_conflict")
        digest = request_hash(
            {
                "actor": principal.actor_id,
                "tenant": self.tenant,
                "config": self.workspace.service.fingerprint,
                "binding": binding,
                "revision": old["revision"],
                "value": value,
            }
        )
        return {
            "before": self._public(binding, old),
            "value": value,
            "keep_alive_seconds": value["boot_seconds"],
            "confirmation_digest": digest,
            "execution_connected": False,
            "applied": False,
        }, version

    def preview(self, principal, body):
        self.workspace._authorize(principal, admin=True)
        value = self._body(body)
        identity, binding = self._target(body["profile_id"])
        with self.repo.engine.connect() as connection:
            return self._preview(connection, principal, body, identity, binding, value)[
                0
            ]

    def write(self, principal, body, key):
        self.workspace._authorize(principal, admin=True)
        value = self._body(body, write=True)
        identity, binding = self._target(body["profile_id"])
        with self.repo.transaction() as connection:
            lock_account_gate(connection, self.tenant)
            self.repo._lock_capacity(connection)
            canonical = account_identity(connection, self.tenant, principal.owner)
            require(
                canonical is None
                or (
                    canonical.get("status") == "active"
                    and canonical.get("role") == "admin"
                ),
                "admin_required",
                403,
            )
            actor, digest, prior = self.workspace._journal(
                connection, principal, key, KIND, body
            )
            if prior:
                return copy.deepcopy(prior["payload"])
            preview, version = self._preview(
                connection, principal, body, identity, binding, value
            )
            require(
                body["confirmation_digest"] == preview["confirmation_digest"],
                "start_rules_confirmation_changed",
            )
            stored = {
                "version": version + 1,
                "payload": {
                    "tenant_id": self.tenant,
                    "binding": binding,
                    "value": value,
                },
                "actor": principal.actor_id,
                "updated_at": self.repo.clock(),
            }
            if version:
                result = connection.execute(
                    update(operator_policy)
                    .where(
                        operator_policy.c.id == identity,
                        operator_policy.c.version == version,
                    )
                    .values(**stored)
                )
                require(result.rowcount == 1, "start_rules_conflict")
            else:
                connection.execute(
                    insert(operator_policy).values(id=identity, **stored)
                )
            current, _ = self._load(connection, identity, binding)
            response = self._public(binding, current)
            command = self.workspace._command(
                connection, actor, key, digest, KIND, response
            )
            connection.execute(
                insert(outbox).values(
                    id=str(uuid.uuid4()),
                    event_type=EVENT,
                    aggregate_id=identity,
                    payload={
                        "tenant_id": self.tenant,
                        "binding": binding,
                        "actor_id": principal.actor_id,
                        "actor_owner": principal.owner,
                        "reason": body["reason"],
                        "command_id": command,
                        "before": preview["before"],
                        "after": response,
                    },
                    created_at=self.repo.clock(),
                )
            )
            return response

    def history(self, principal, profile_id):
        self.workspace._authorize(principal, admin=True)
        identity, _ = self._target(profile_id)
        with self.repo.engine.connect() as connection:
            rows = connection.execute(
                select(outbox.c.id, outbox.c.created_at, outbox.c.payload)
                .where(
                    outbox.c.event_type == EVENT,
                    outbox.c.aggregate_id == identity,
                    outbox.c.payload["tenant_id"].as_string() == self.tenant,
                )
                .order_by(outbox.c.created_at.desc(), outbox.c.id.desc())
                .limit(50)
            ).mappings()
            return {
                "items": [
                    {"id": row["id"], "at": row["created_at"], **row["payload"]}
                    for row in rows
                ],
                "limit": 50,
            }


def register_routes(app, workspace):
    from fastapi import Header, Request
    from fastapi.responses import JSONResponse

    rules = StartRules(workspace)
    prefix = "/v1/operator/dstack/admin/start-rules"

    def principal(request):
        return getattr(request.state, "principal", None)

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.get(prefix)
    def read(request: Request, profile_id: str):
        return response(rules.read(principal(request), profile_id))

    @app.post(prefix + "/preview")
    def preview(request: Request, body: dict):
        return response(rules.preview(principal(request), body))

    @app.put(prefix)
    def write(
        request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")
    ):
        return response(rules.write(principal(request), body, key))

    @app.get(prefix + "/history")
    def history(request: Request, profile_id: str):
        return response(rules.history(principal(request), profile_id))
