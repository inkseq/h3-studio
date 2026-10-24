"""Admin switch for story-chat card pictures (#422). Default OFF.

Owner decision 2026-10-23: production admins choose whether card pictures are available.
One persisted document per tenant, changed only by an active admin identity, CAS on its
version and an admin audit row. The reason is optional (owner, 2026-10-23); a blank one is
recorded as a plain on/off note. Price and charging remain the #463 controls.
"""

from __future__ import annotations

import uuid

from sqlalchemy import insert, inspect, select, update

from .identity_schema import admin_audit, users
from .repository import documents

OWNER = PROJECT = "__story_chat"
KIND = "story_chat_settings"
ID = "active"
DEFAULT = {"pictures_enabled": False}


class StoryChatSettingsError(Exception):
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
    """Current switch; anything malformed reads as OFF (fail closed)."""
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
    enabled = (
        type(value) is dict
        and set(value) == {"pictures_enabled"}
        and value["pictures_enabled"] is True
    )
    return {
        "pictures_enabled": enabled,
        "version": row["version"],
        "updated_at": row["updated_at"],
    }


def write(connection, tenant, principal, body, now):
    if (
        type(body) is not dict
        or not {"expected_version", "pictures_enabled"} <= set(body)
        or not set(body) <= {"expected_version", "pictures_enabled", "reason"}
        or type(body["expected_version"]) is not int
        or type(body["pictures_enabled"]) is not bool
    ):
        raise StoryChatSettingsError("story_chat_settings_invalid")
    reason = body.get("reason", "")
    if (
        not isinstance(reason, str)
        or len(reason.strip()) > 500
        or any(ord(c) < 32 for c in reason)
    ):
        raise StoryChatSettingsError("story_chat_settings_invalid")
    reason = reason.strip() or (
        "Pictures turned on in Admin"
        if body["pictures_enabled"]
        else "Pictures turned off in Admin"
    )
    if not inspect(connection).has_table(users.name):
        raise StoryChatSettingsError("admin_identity_required", 409)
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
        raise StoryChatSettingsError("admin_required", 403)
    old = read(connection, tenant)
    if body["expected_version"] != old["version"]:
        raise StoryChatSettingsError("story_chat_settings_changed", 409)
    value = {"pictures_enabled": body["pictures_enabled"]}
    if value["pictures_enabled"] == old["pictures_enabled"]:
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
            raise StoryChatSettingsError("story_chat_settings_changed", 409)
    connection.execute(
        insert(admin_audit).values(
            id=str(uuid.uuid4()),
            tenant=tenant,
            actor_user_id=actor["id"],
            target_user_id=None,
            action="story_chat.pictures",
            reason=reason,
            before={"pictures_enabled": old["pictures_enabled"]},
            after={
                "pictures_enabled": value["pictures_enabled"],
                "actor": actor["display_name"] or actor["owner"],
                "target": "Story chat pictures",
            },
            created_at=now,
        )
    )
    return read(connection, tenant)
