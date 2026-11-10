"""Release maintenance notice (#453): tell users before a release window.

One persisted document per tenant, written by an active admin (CAS on its
version, admin audit row) or by the release helper through this module's CLI
(`python -m studio_platform.maintenance_notice on|off`), also audited.

While active the site stays readable and accepted work keeps running. Only new
paid admissions are refused with `maintenance_in_progress` and the expected end
time. Anything malformed reads as inactive, so a bad row never blocks work.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
import uuid

from sqlalchemy import insert, inspect, select, update

from .identity_schema import admin_audit, users
from .repository import Conflict, documents

OWNER = PROJECT = "__maintenance"
KIND = "maintenance_notice"
ID = "active"
SEVERITIES = ("info", "critical")
MAX_MINUTES = 24 * 60
RELEASE_ACTOR = "release-runner"
INACTIVE = {
    "active": False,
    "message": None,
    "started_at": None,
    "expected_end_at": None,
    "severity": "info",
}
# /api/auth/config is polled by every open tab; a short cache keeps that cheap.
CACHE_SECONDS = 5.0
_cache = {}
_cache_lock = threading.Lock()


class MaintenanceNoticeError(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)


class MaintenanceInProgress(Conflict):
    """New paid work is refused during a release window; nothing was charged."""

    code = "maintenance_in_progress"

    def __init__(self, expected_end_at):
        self.expected_end_at = expected_end_at
        super().__init__(self.code)


def _where(tenant):
    return (
        documents.c.tenant_id == tenant,
        documents.c.owner_id == OWNER,
        documents.c.project_id == PROJECT,
        documents.c.kind == KIND,
        documents.c.document_id == ID,
    )


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _valid(value):
    return (
        type(value) is dict
        and set(value) == set(INACTIVE)
        and type(value["active"]) is bool
        and (value["message"] is None or isinstance(value["message"], str))
        and value["severity"] in SEVERITIES
        and (
            not value["active"]
            or _finite(value["started_at"])
            and _finite(value["expected_end_at"])
        )
    )


def read(connection, tenant):
    row = (
        connection.execute(
            select(documents.c.payload, documents.c.version).where(*_where(tenant))
        )
        .mappings()
        .first()
    )
    if row is None:
        return {**INACTIVE, "version": 0}
    value = row["payload"] if _valid(row["payload"]) else INACTIVE
    return {**value, "version": row["version"]}


def public(value):
    """Anonymous view for /api/auth/config; the version stays internal."""
    return {key: value[key] for key in INACTIVE}


def current(engine, tenant, *, clock=time.monotonic):
    """Cached read for hot paths (config poll, admission check)."""
    now = clock()
    with _cache_lock:
        hit = _cache.get((id(engine), tenant))
        if hit is not None and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    with engine.connect() as connection:
        value = read(connection, tenant)
    with _cache_lock:
        _cache[(id(engine), tenant)] = (now, value)
    return value


def forget_cache():
    with _cache_lock:
        _cache.clear()


def require_open(connection, tenant):
    """Refuse new paid admission while the notice is active (fresh read)."""
    value = read(connection, tenant)
    if value["active"]:
        raise MaintenanceInProgress(value["expected_end_at"])


def _parse(body, now):
    if (
        type(body) is not dict
        or "active" not in body
        or type(body["active"]) is not bool
        or not set(body)
        <= {"expected_version", "active", "message", "minutes", "severity", "reason"}
    ):
        raise MaintenanceNoticeError("maintenance_notice_invalid")
    message = body.get("message")
    severity = body.get("severity", "info")
    minutes = body.get("minutes")
    if message is not None and (
        not isinstance(message, str)
        or len(message.strip()) > 300
        or any(ord(c) < 32 for c in message)
    ):
        raise MaintenanceNoticeError("maintenance_notice_invalid")
    if severity not in SEVERITIES:
        raise MaintenanceNoticeError("maintenance_notice_invalid")
    if not body["active"]:
        return dict(INACTIVE)
    if type(minutes) is not int or not 1 <= minutes <= MAX_MINUTES:
        raise MaintenanceNoticeError("maintenance_notice_invalid")
    return {
        "active": True,
        "message": (message or "").strip() or None,
        "started_at": now,
        "expected_end_at": now + minutes * 60,
        "severity": severity,
    }


def _store(connection, tenant, old, value, now):
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
        return
    changed = connection.execute(
        update(documents)
        .where(*_where(tenant), documents.c.version == old["version"])
        .values(payload=value, version=old["version"] + 1, updated_at=now)
    )
    if changed.rowcount != 1:
        raise MaintenanceNoticeError("maintenance_notice_changed", 409)


def _audit(connection, tenant, actor_id, actor_name, old, value, reason, now):
    if not inspect(connection).has_table(admin_audit.name):
        return
    connection.execute(
        insert(admin_audit).values(
            id=str(uuid.uuid4()),
            tenant=tenant,
            actor_user_id=actor_id,
            target_user_id=None,
            action="maintenance.notice",
            reason=reason,
            before=public(old),
            after={**public(value), "actor": actor_name, "target": "Release notice"},
            created_at=now,
        )
    )


def write(connection, tenant, principal, body, now):
    """Admin path: CAS on expected_version, audited."""
    value = _parse(body, now)
    if type(body.get("expected_version")) is not int:
        raise MaintenanceNoticeError("maintenance_notice_invalid")
    if not inspect(connection).has_table(users.name):
        raise MaintenanceNoticeError("admin_identity_required", 409)
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
        raise MaintenanceNoticeError("admin_required", 403)
    old = read(connection, tenant)
    if body["expected_version"] != old["version"]:
        raise MaintenanceNoticeError("maintenance_notice_changed", 409)
    reason = (body.get("reason") or "").strip()[:500] or (
        "Release notice on" if value["active"] else "Release notice off"
    )
    _store(connection, tenant, old, value, now)
    _audit(
        connection,
        tenant,
        actor["id"],
        actor["display_name"] or actor["owner"],
        old,
        value,
        reason,
        now,
    )
    forget_cache()
    return read(connection, tenant)


def release_write(connection, tenant, body, now):
    """Release helper path: last write wins (no CAS), audited as the runner."""
    value = _parse(body, now)
    old = read(connection, tenant)
    if not value["active"] and not old["active"]:
        return old
    reason = (body.get("reason") or "").strip()[:500] or (
        "Release started" if value["active"] else "Release finished"
    )
    _store(connection, tenant, old, value, now)
    _audit(connection, tenant, RELEASE_ACTOR, RELEASE_ACTOR, old, value, reason, now)
    forget_cache()
    return read(connection, tenant)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Release maintenance notice")
    parser.add_argument("state", choices=("on", "off"))
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument("--message")
    parser.add_argument("--severity", choices=SEVERITIES, default="info")
    args = parser.parse_args(argv)
    from .repository import Repository
    from .settings import Settings

    settings = Settings.from_environment()
    repo = Repository(settings.database_url)
    body = {"active": args.state == "on"}
    if body["active"]:
        body.update(minutes=args.minutes, severity=args.severity)
        if args.message:
            body["message"] = args.message
    try:
        with repo.transaction() as connection:
            value = release_write(connection, settings.tenant_id, body, repo.clock())
    finally:
        repo.close()
    print(json.dumps({"maintenance_notice": public(value)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
