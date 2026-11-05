"""Admin switches for voice (#997). Both default OFF.

`voice_enabled` shows voices and allows generating. When customer charging is on,
voice reserves the character quote and captures only delivered audio unless
`free_while_testing` is on, which production refuses outright. Existing CAS and audit
shape are unchanged; enabling voice is still an explicit administrative decision.
"""

import uuid

from sqlalchemy import insert, inspect, select, update

from .identity_schema import admin_audit, users
from .repository import documents

OWNER = PROJECT = "__speech"
KIND = "speech_settings"
ID = "active"
FIELDS = ("voice_enabled", "free_while_testing")
DEFAULT = {"voice_enabled": False, "free_while_testing": False}


class SpeechSettingsError(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)


def _where(tenant):
    return (
        documents.c.tenant_id == tenant,
        documents.c.owner_id == OWNER,
        documents.c.project_id == PROJECT,
        documents.c.kind == KIND,
        documents.c.document_id == ID,
    )


def read(connection, tenant):
    """Current switches; anything malformed reads as OFF (fail closed)."""
    row = (
        connection.execute(
            select(
                documents.c.payload, documents.c.version, documents.c.updated_at
            ).where(*_where(tenant))
        )
        .mappings()
        .first()
    )
    if row is None:
        return {**DEFAULT, "version": 0, "updated_at": None}
    value = row["payload"]
    if type(value) is not dict or set(value) != set(FIELDS):
        value = DEFAULT
    return {
        **{field: value[field] is True for field in FIELDS},
        "version": row["version"],
        "updated_at": row["updated_at"],
    }


def write(connection, tenant, principal, body, now, *, deployment_target):
    if (
        type(body) is not dict
        or not {"expected_version", *FIELDS} <= set(body)
        or not set(body) <= {"expected_version", "reason", *FIELDS}
        or type(body["expected_version"]) is not int
        or any(type(body[field]) is not bool for field in FIELDS)
    ):
        raise SpeechSettingsError("speech_settings_invalid")
    reason = body.get("reason", "")
    if (
        not isinstance(reason, str)
        or len(reason.strip()) > 500
        or any(ord(c) < 32 for c in reason)
    ):
        raise SpeechSettingsError("speech_settings_invalid")
    if body["free_while_testing"] and deployment_target != "staging":
        raise SpeechSettingsError("speech_free_staging_only", 409)
    if not inspect(connection).has_table(users.name):
        raise SpeechSettingsError("admin_identity_required", 409)
    actor = (
        connection.execute(
            select(users).where(
                users.c.tenant == tenant, users.c.owner == principal.owner
            )
        )
        .mappings()
        .first()
    )
    if actor is None or actor["status"] != "active" or actor["role"] != "admin":
        raise SpeechSettingsError("admin_required", 403)
    old = read(connection, tenant)
    if body["expected_version"] != old["version"]:
        raise SpeechSettingsError("speech_settings_changed", 409)
    value = {field: body[field] for field in FIELDS}
    if all(value[field] == old[field] for field in FIELDS):
        return old
    if old["version"] == 0:
        connection.execute(
            insert(documents).values(
                tenant_id=tenant,
                owner_id=OWNER,
                project_id=PROJECT,
                kind=KIND,
                document_id=ID,
                payload=value,
                version=1,
                updated_at=now,
            )
        )
    else:
        changed = connection.execute(
            update(documents)
            .where(*_where(tenant), documents.c.version == old["version"])
            .values(payload=value, version=old["version"] + 1, updated_at=now)
        )
        if changed.rowcount != 1:
            raise SpeechSettingsError("speech_settings_changed", 409)
    connection.execute(
        insert(admin_audit).values(
            id=str(uuid.uuid4()),
            tenant=tenant,
            actor_user_id=actor["id"],
            target_user_id=None,
            action="speech.settings",
            reason=reason.strip() or "Voice settings changed in Admin",
            before={field: old[field] for field in FIELDS},
            after={
                **value,
                "actor": actor["display_name"] or actor["owner"],
                "target": "Voice",
            },
            created_at=now,
        )
    )
    return read(connection, tenant)
