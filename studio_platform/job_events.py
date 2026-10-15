"""Bounded, owner/project-authorized reads of the original job outbox.

The cursor is an authorized event UUID, never a global sequence or offset.
Only post-activation, commit-ordered events are included. Delivery/acknowledgement
does not hide an event; raw outbox payloads are never a public response.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from .repository import jobs, outbox


PUBLIC_STATUSES = frozenset({"planned", "blocked", "waiting_capacity", "queued",
    "claimed", "submitting", "submission_unknown", "running", "collecting",
    "cancel_requested", "recovery_hold", "succeeded", "failed", "cancelled"})
CURSOR_PREFIX = "je1."


def _cursor(value):
    if not isinstance(value, str) or not value.startswith(CURSOR_PREFIX):
        raise HTTPException(410, "job_event_cursor_unavailable")
    identifier = value[len(CURSOR_PREFIX):]
    try:
        if str(uuid.UUID(identifier)) != identifier:
            raise ValueError
    except (ValueError, AttributeError):
        raise HTTPException(410, "job_event_cursor_unavailable") from None
    return identifier


def _advice(value):
    value = value if isinstance(value, dict) else {}
    return {"safe_to_retry": value.get("safe_to_retry") is True,
        "charged": value.get("charged") if type(value.get("charged")) is bool else None,
        "upstream_still_running": value.get("upstream_still_running")
            if type(value.get("upstream_still_running")) is bool else None}


class JobEvents:
    def __init__(self, repo, tenant_id):
        self.repo, self.tenant_id = repo, tenant_id

    def read(self, principal, *, since=None, limit=100):
        if principal is None:
            raise HTTPException(401, "authentication_required")
        if principal.machine and "jobs:read" not in principal.scopes:
            raise HTTPException(403, "job_events_read_forbidden")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HTTPException(422, "job_events_limit_invalid")
        joined = outbox.join(jobs, outbox.c.aggregate_id == jobs.c.id)
        authorized = [jobs.c.tenant_id == self.tenant_id, jobs.c.owner_id == principal.owner,
            outbox.c.job_event_seq.is_not(None)]
        if principal.machine and not principal.all_projects:
            authorized.append(jobs.c.project_id.in_(principal.project_ids))
        with self.repo.engine.connect() as connection:
            after = 0
            if since is not None:
                identifier = _cursor(since)
                after = connection.execute(select(outbox.c.job_event_seq).select_from(joined)
                    .where(*authorized, outbox.c.id == identifier)).scalar_one_or_none()
                # Absent/foreign/expired cursor has exactly the same response.
                if after is None:
                    raise HTTPException(410, "job_event_cursor_unavailable")
            records = list(connection.execute(select(outbox.c.id, outbox.c.event_type,
                outbox.c.aggregate_id, outbox.c.payload, outbox.c.created_at, jobs.c.project_id)
                .select_from(joined).where(*authorized, outbox.c.job_event_seq > after)
                .order_by(outbox.c.job_event_seq).limit(limit + 1)).mappings())
        values = []
        for row in records[:limit]:
            payload = row["payload"] if isinstance(row["payload"], dict) else {}
            state = payload.get("public_state")
            if not isinstance(state, dict) or state.get("status") not in PUBLIC_STATUSES:
                # A corrupt/missing snapshot needs a same-job status resync,
                # rather than guessing history from the current row.
                raise HTTPException(503, "job_event_snapshot_unavailable")
            values.append({"cursor": CURSOR_PREFIX + row["id"], "event_id": row["id"],
                "event_type": row["event_type"], "job_id": row["aggregate_id"],
                "project_id": row["project_id"], "occurred_at": row["created_at"],
                "status": state["status"], "error_code": state.get("error_code"),
                "cancelled_by": state.get("cancelled_by"), "reason_code": state.get("reason_code"),
                "message": state.get("message"), "retry_advice": _advice(state.get("retry_advice"))})
            if isinstance(state.get("failure_diagnostic"), dict):
                values[-1]["failure_diagnostic"] = state["failure_diagnostic"]
            if isinstance(state.get("failure_notice"), dict):
                values[-1]["failure_notice"] = state["failure_notice"]
        return {"events": values, "next_cursor": values[-1]["cursor"] if values else since,
            "has_more": len(records) > limit, "poll_after_seconds": 5,
            "history": "post_activation", "observed_at": self.repo.clock()}


def register_routes(app):
    service = JobEvents(app.state.repository, app.state.settings.tenant_id)
    app.state.job_events = service

    @app.get("/v1/job-events")
    def events(request: Request, since: str | None = Query(None, max_length=40),
               limit: int = Query(100, ge=1, le=100)):
        return JSONResponse(service.read(request.state.principal, since=since, limit=limit),
            headers={"Cache-Control": "no-store"})

    return service
