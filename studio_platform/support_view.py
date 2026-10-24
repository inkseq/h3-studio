"""Read-only support view: a human admin sees one user's account as they do (owner, 2026-10-25).

A view is started with a reason, lasts at most 30 minutes, is bound to the admin who
started it and is written to the existing admin audit log. While a request carries the
view id, the server swaps the principal to the target account and refuses every write.
No new tables: views live in the generic documents store under the admin's scope.
"""

from __future__ import annotations

import re
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import insert, select, update

from .auth import Principal
from .identity_schema import admin_audit, users
from .repository import documents

KIND = "support_view"
PROJECT = "__support_views"
TTL_SECONDS = 1800
VIEW_ID = re.compile(r"[0-9a-f]{32}\Z")
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Paths a support view never reaches, even read-only: sign-in, credentials, admin tools.
BLOCKED_PREFIXES = (
    "/api/auth/",
    "/v1/admin/",
    "/v1/operator/",
    "/v1/api-keys",
    "/v1/agent-connect",
)


class SupportViewError(Exception):
    def __init__(self, code, status):
        super().__init__(code)
        self.code, self.status = code, status


def _admin_identity(connection, settings, principal):
    if principal is None or principal.machine or principal.support_viewer:
        raise SupportViewError("support_view_admin_required", 403)
    row = (
        connection.execute(
            select(users).where(
                users.c.tenant == settings.tenant_id, users.c.owner == principal.owner
            )
        )
        .mappings()
        .first()
    )
    if not row or row["role"] != "admin" or row["status"] != "active":
        raise SupportViewError("support_view_admin_required", 403)
    return dict(row)


def _load(connection, settings, admin_owner, view_id):
    if not isinstance(view_id, str) or not VIEW_ID.fullmatch(view_id):
        return None
    row = (
        connection.execute(
            select(documents.c.payload, documents.c.version).where(
                documents.c.tenant_id == settings.tenant_id,
                documents.c.owner_id == admin_owner,
                documents.c.project_id == PROJECT,
                documents.c.kind == KIND,
                documents.c.document_id == view_id,
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _audit(connection, settings, actor, target, action, reason, now, view_id):
    connection.execute(
        insert(admin_audit).values(
            id=str(uuid.uuid4()),
            tenant=settings.tenant_id,
            actor_user_id=actor["id"],
            target_user_id=target["id"],
            action=action,
            reason=reason,
            before={},
            after={
                "view_id": view_id,
                "actor": actor.get("display_name") or actor["owner"],
                "target": target.get("email")
                or target.get("display_name")
                or target["owner"],
            },
            created_at=now,
        )
    )


def start(repo, settings, principal, body):
    if not isinstance(body, dict) or set(body) != {"user_id", "reason"}:
        raise SupportViewError("support_view_invalid", 422)
    reason = body["reason"].strip() if isinstance(body["reason"], str) else ""
    if not 3 <= len(reason) <= 300 or not isinstance(body["user_id"], str):
        raise SupportViewError("support_view_reason_required", 422)
    now = repo.clock()
    with repo.engine.begin() as connection:
        actor = _admin_identity(connection, settings, principal)
        target = (
            connection.execute(
                select(users).where(
                    users.c.tenant == settings.tenant_id, users.c.id == body["user_id"]
                )
            )
            .mappings()
            .first()
        )
        if not target:
            raise SupportViewError("support_view_user_not_found", 404)
        target = dict(target)
        if target["role"] == "admin" or target["owner"] == principal.owner:
            # Admins are not viewed through this path; no privilege is ever borrowed.
            raise SupportViewError("support_view_target_not_allowed", 403)
        view_id = uuid.uuid4().hex
        payload = {
            "target_owner": target["owner"],
            "target_user_id": target["id"],
            "admin_owner": principal.owner,
            "reason": reason,
            "started_at": now,
            "expires_at": now + TTL_SECONDS,
            "ended_at": None,
        }
        connection.execute(
            insert(documents).values(
                tenant_id=settings.tenant_id,
                owner_id=principal.owner,
                project_id=PROJECT,
                kind=KIND,
                document_id=view_id,
                version=1,
                payload=payload,
                updated_at=now,
            )
        )
        _audit(
            connection,
            settings,
            actor,
            target,
            "support view started (read-only)",
            reason,
            now,
            view_id,
        )
    return {
        "id": view_id,
        "owner": target["owner"],
        "display_name": target.get("display_name") or target["owner"],
        "expires_at": payload["expires_at"],
        "read_only": True,
    }


def end(repo, settings, principal, view_id):
    now = repo.clock()
    with repo.engine.begin() as connection:
        actor = _admin_identity(connection, settings, principal)
        row = _load(connection, settings, principal.owner, view_id)
        if not row:
            raise SupportViewError("support_view_not_found", 404)
        payload = dict(row["payload"])
        if payload.get("ended_at") is None:
            payload["ended_at"] = now
            connection.execute(
                update(documents)
                .where(
                    documents.c.tenant_id == settings.tenant_id,
                    documents.c.owner_id == principal.owner,
                    documents.c.project_id == PROJECT,
                    documents.c.kind == KIND,
                    documents.c.document_id == view_id,
                    documents.c.version == row["version"],
                )
                .values(payload=payload, version=row["version"] + 1, updated_at=now)
            )
            target = {"id": payload["target_user_id"], "owner": payload["target_owner"]}
            _audit(
                connection,
                settings,
                actor,
                target,
                "support view ended",
                payload["reason"],
                now,
                view_id,
            )
    return {"id": view_id, "ended": True}


def resolve(repo, settings, principal, view_id):
    """The target principal for a live view started by this admin, else None."""
    if principal is None or principal.machine or principal.support_viewer:
        return None
    with repo.engine.connect() as connection:
        row = _load(connection, settings, principal.owner, view_id)
        if not row:
            return None
        payload = row["payload"]
        now = repo.clock()
        if payload.get("ended_at") is not None or not now < payload.get(
            "expires_at", 0
        ):
            return None
        try:
            _admin_identity(connection, settings, principal)
        except SupportViewError:
            return None
        target = (
            connection.execute(
                select(users).where(
                    users.c.tenant == settings.tenant_id,
                    users.c.id == payload["target_user_id"],
                    users.c.owner == payload["target_owner"],
                )
            )
            .mappings()
            .first()
        )
    if not target or target["role"] == "admin":
        return None
    return Principal(
        owner=target["owner"],
        actor_id="support:" + principal.actor_id,
        user_id=target["id"],
        role=target["role"],
        status=target["status"],
        identity_source="canonical",
        support_viewer=principal.owner,
    )


def refusal(method, path):
    """Why a support-view request is refused, or None when it may proceed."""
    if method not in READ_METHODS:
        return "support_view_read_only"
    if path.startswith(BLOCKED_PREFIXES):
        return "support_view_path_blocked"
    return None


def register_routes(app):
    def _run(call, status=200):
        try:
            return JSONResponse(
                call(), status_code=status, headers={"Cache-Control": "no-store"}
            )
        except SupportViewError as error:
            return JSONResponse(
                {"code": error.code, "detail": error.code},
                status_code=error.status,
                headers={"Cache-Control": "no-store"},
            )

    @app.post("/v1/admin/support-views")
    def start_view(request: Request, body: dict):
        return _run(
            lambda: start(
                app.state.repository, app.state.settings, request.state.principal, body
            ),
            201,
        )

    @app.post("/v1/admin/support-views/{view_id}/end")
    def end_view(view_id: str, request: Request):
        return _run(
            lambda: end(
                app.state.repository,
                app.state.settings,
                request.state.principal,
                view_id,
            )
        )
