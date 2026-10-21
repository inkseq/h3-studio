"""Browser revocation and bounded AuthKit checks using original document journals."""

from copy import deepcopy
from datetime import datetime, timezone
import re
import uuid

from sqlalchemy import delete, select

from .auth import (
    Principal,
    SessionUnavailable,
    WORKOS_PRIVATE_OWNER,
    WORKOS_PRIVATE_PROJECT,
    WORKOS_SESSION_FIELDS,
    WORKOS_SESSION_KIND,
    _workos_session_shape,
    sessions,
)
from .identity import lock_account_gate
from .identity_schema import users
from .repository import documents, request_hash
from .session_authority import advance, read, write

CHECK_SECONDS = 60
MAX_PAGES = 10
WORKFLOW = "inkseq.account.lifecycle.v1"


class SessionError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def _seconds(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SessionUnavailable()
    return value.astimezone(timezone.utc).timestamp()


class AccountSessions:
    def __init__(self, repo, auth, settings, workos):
        self.repo, self.auth, self.settings, self.workos = repo, auth, settings, workos
        self.tenant = settings.tenant_id

    def _binding(self, conn, row):
        value = conn.execute(
            select(documents.c.payload).where(
                *self.auth._workos_document(row["username"], row["token_hash"])
            )
        ).scalar_one_or_none()
        if (
            type(value) is not dict
            or set(value)
            != WORKOS_SESSION_FIELDS | {"owner", "session_hash", "session_created_at"}
            or value["owner"] != row["username"]
            or value["session_hash"] != row["token_hash"]
            or value["session_created_at"] != row["created"]
        ):
            return None
        candidate = {key: value[key] for key in WORKOS_SESSION_FIELDS}
        return candidate if _workos_session_shape(candidate) else None

    def _scan(self, subject, *, cursor=None):
        """Return one complete bounded page batch; never infer absence from a prefix."""
        config = self.workos.config()
        found, seen = {}, set()
        with self.workos.sdk(config) as client:
            for _ in range(MAX_PAGES):
                page = client.user_management.list_sessions(
                    subject, limit=100, after=cursor, order="desc"
                )
                if type(page.data) is not list or len(page.data) > 100:
                    raise SessionUnavailable()
                for item in page.data:
                    sid = getattr(item, "id", None)
                    status = getattr(
                        getattr(item, "status", None),
                        "value",
                        getattr(item, "status", None),
                    )
                    if (
                        not isinstance(sid, str)
                        or not re.fullmatch(r"session_[A-Za-z0-9]{10,80}", sid)
                        or getattr(item, "user_id", None) != subject
                        or sid in found
                        or status != "active"
                        or getattr(item, "ended_at", None) is not None
                    ):
                        raise SessionUnavailable()
                    found[sid] = {
                        "created_at": _seconds(item.created_at),
                        "expires_at": _seconds(item.expires_at),
                    }
                following = page.list_metadata.after
                if following is None:
                    return config, found, None
                if (
                    not isinstance(following, str)
                    or re.fullmatch(r"[A-Za-z0-9_-]{1,200}", following) is None
                    or following in seen
                    or following == cursor
                    or not page.data
                ):
                    raise SessionUnavailable()
                seen.add(following)
                cursor = following
        return config, found, cursor

    def check(self, token_hash):
        with self.repo.engine.connect() as conn:
            row = (
                conn.execute(
                    select(sessions).where(
                        sessions.c.tenant == self.tenant,
                        sessions.c.token_hash == token_hash,
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["expires"] <= self.auth.clock():
                return False
            if row["auth_mode"] != "workos":
                return True
            binding = self._binding(conn, row)
            if binding is None:
                return False
            identity = conn.execute(
                select(users.c.workos_user_id).where(
                    users.c.tenant == self.tenant,
                    users.c.owner == row["username"],
                    users.c.status == "active",
                )
            ).scalar_one_or_none()
            if identity != binding["subject"]:
                return False
            fingerprint = request_hash(
                {**binding, "created": row["created"], "owner": row["username"]}
            )
            cache = read(conn, self.tenant, "provider_check", token_hash)
            now = self.auth.clock()
            if (
                type(cache) is dict
                and cache.get("fingerprint") == fingerprint
                and type(cache.get("checked_at")) in (int, float)
                and 0 <= now - cache["checked_at"] < CHECK_SECONDS
            ):
                return True
        # No database transaction/connection is held during provider I/O.
        try:
            config, live, cursor = self._scan(binding["subject"])
            if config["client_id"] != binding["client_id"] or cursor is not None:
                raise SessionUnavailable()
        except Exception:
            raise SessionUnavailable() from None
        with self.repo.transaction() as conn:
            lock_account_gate(conn)
            current = (
                conn.execute(
                    select(sessions).where(
                        sessions.c.tenant == self.tenant,
                        sessions.c.token_hash == token_hash,
                    )
                )
                .mappings()
                .first()
            )
            immutable = (
                "token_hash",
                "username",
                "auth_mode",
                "created",
                "password_version",
            )
            if (
                current is None
                or current["expires"] <= self.auth.clock()
                or any(current[key] != row[key] for key in immutable)
                or self._binding(conn, current) != binding
            ):
                return False
            if binding["sid"] not in live:
                conn.execute(
                    delete(sessions).where(
                        sessions.c.tenant == self.tenant,
                        sessions.c.token_hash == token_hash,
                    )
                )
                return False
            if live[binding["sid"]]["expires_at"] <= self.auth.clock():
                return False
            write(
                conn,
                self.tenant,
                "provider_check",
                token_hash,
                {"fingerprint": fingerprint, "checked_at": self.auth.clock()},
                self.auth.clock(),
            )
        return True

    def revoke_all(self, principal, body, key):
        if (
            not isinstance(principal, Principal)
            or principal.machine
            or not principal.session_hash
        ):
            raise SessionError("browser_required", 403)
        if (
            type(body) is not dict
            or body
            or not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", key)
        ):
            raise SessionError("session_revocation_request_invalid", 422)
        try:
            configured_client = self.workos.config()["client_id"]
        except Exception:
            configured_client = None
        journal = request_hash({"owner": principal.owner, "key": key})
        with self.repo.transaction() as conn:
            lock_account_gate(conn, exclusive=True)
            identity = (
                conn.execute(
                    select(users).where(
                        users.c.tenant == self.tenant,
                        users.c.owner == principal.owner,
                        users.c.status == "active",
                    )
                )
                .mappings()
                .first()
            )
            row = (
                conn.execute(
                    select(sessions).where(
                        sessions.c.tenant == self.tenant,
                        sessions.c.token_hash == principal.session_hash,
                        sessions.c.username == principal.owner,
                        sessions.c.expires > self.auth.clock(),
                    )
                )
                .mappings()
                .first()
            )
            if identity is None or row is None or identity["id"] != principal.user_id:
                raise SessionError("account_unavailable", 403)
            old = read(conn, self.tenant, "revocation_key", journal)
            if old is not None:
                return self._public(
                    read(conn, self.tenant, "revocation", old["operation_id"])
                )
            binding = self._binding(conn, row) if row["auth_mode"] == "workos" else None
            now, ident = self.auth.clock(), str(uuid.uuid4())
            value = {
                "operation_id": ident,
                "owner": principal.owner,
                "user_id": identity["id"],
                "subject": identity["workos_user_id"],
                "epoch": advance(conn, self.tenant, principal.owner, now),
                "cutoff": now,
                "state": "pending" if identity["workos_user_id"] else "completed",
                "cursor": None,
                "inventory_complete": False,
                "provider_sessions": {binding["sid"]: "unrequested"} if binding else {},
                "logout_binding": binding,
                "created_at": now,
                "error_code": None,
            }
            value["client_id"] = binding["client_id"] if binding else configured_client
            write(conn, self.tenant, "revocation", ident, value, now)
            write(
                conn,
                self.tenant,
                "revocation_key",
                journal,
                {"operation_id": ident},
                now,
            )
            conn.execute(
                delete(sessions).where(
                    sessions.c.tenant == self.tenant,
                    sessions.c.username == principal.owner,
                )
            )
            # Preserve exact original #395 shape; orphan sidecars grant no access.
            conn.execute(
                delete(documents).where(
                    documents.c.tenant_id == self.tenant,
                    documents.c.owner_id == WORKOS_PRIVATE_OWNER,
                    documents.c.project_id == WORKOS_PRIVATE_PROJECT,
                    documents.c.kind == WORKOS_SESSION_KIND,
                    documents.c.payload["owner"].as_string() == principal.owner,
                )
            )
            if (
                value["state"] == "pending"
                and self.repo.generation_transport is not None
            ):
                self.repo.generation_transport.enqueue_account_operation(
                    conn, self.tenant, ident
                )
        return self._public(value)

    def _public(self, value):
        result = {key: value[key] for key in ("operation_id", "state")}
        if value.get("logout_binding"):
            url = self.workos.logout_url(value["logout_binding"])
            if url is not None:
                result["logout_url"] = url
        return result

    def _save(self, ident, change):
        with self.repo.transaction() as conn:
            lock_account_gate(conn, exclusive=True)
            value = deepcopy(read(conn, self.tenant, "revocation", ident))
            change(value)
            write(conn, self.tenant, "revocation", ident, value, self.auth.clock())
        return value

    def turn(self, tenant, ident):
        if tenant != self.tenant:
            return {"done": True, "state": "unavailable"}
        with self.repo.engine.connect() as conn:
            value = read(conn, tenant, "revocation", ident)
        if value is None or value["state"] == "completed":
            return {"done": True, "state": "completed"}
        try:
            if value["client_id"] is None:
                raise SessionUnavailable()
            config, batch, cursor = self._scan(value["subject"], cursor=value["cursor"])
            if config["client_id"] != value["client_id"]:
                raise SessionUnavailable()
            if not value["inventory_complete"]:

                def inventory(current):
                    for sid, proof in batch.items():
                        if proof["created_at"] <= current["cutoff"]:
                            current["provider_sessions"].setdefault(sid, "unrequested")
                    current.update(
                        cursor=cursor,
                        inventory_complete=cursor is None,
                        error_code=None,
                    )

                value = self._save(ident, inventory)
                if cursor is not None:
                    return {"done": False, "state": "pending", "wait_seconds": 10}
            # Once frozen, re-read a complete live inventory before every
            # bounded mutation turn. Unknown results never cause a blind POST.
            config, live, cursor = self._scan(value["subject"])
            if cursor is not None or config["client_id"] != value["client_id"]:
                raise SessionUnavailable()
            remaining = [sid for sid in value["provider_sessions"] if sid in live]
            for sid in remaining[:10]:
                self._save(
                    ident,
                    lambda current, sid=sid: current["provider_sessions"].__setitem__(
                        sid, "requested"
                    ),
                )
                try:
                    with self.workos.sdk(config) as client:
                        client.user_management.revoke_session(session_id=sid)
                except Exception:
                    pass  # Retain original requested SID; next turn proves outcome.

            def observed(current):
                for sid in current["provider_sessions"]:
                    if sid not in live:
                        current["provider_sessions"][sid] = "revoked"
                if not remaining:
                    current.update(
                        state="completed",
                        completed_at=self.auth.clock(),
                        error_code=None,
                    )

            value = self._save(ident, observed)
        except Exception:
            value = self._save(
                ident,
                lambda current: current.update(
                    error_code="session_provider_unavailable"
                ),
            )
        return {
            "done": value["state"] == "completed",
            "state": value["state"],
            "wait_seconds": 10,
        }
