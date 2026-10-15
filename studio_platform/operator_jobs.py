"""Human-admin queue reads and an atomic original-ledger cancel command."""
from __future__ import annotations

import copy
import re
import uuid

from fastapi import Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, insert, select

from .auth import Principal
from .job_cancellation import cancel_eligibility, cancel_snapshot, cancellation_columns
from .dstack_operator import _actor
from .operator_capacity import OperatorError, operator_commands
from .repository import Conflict, dispatch_receipts, jobs, request_hash


REASONS = (
    {"code": "operator_queue_cleared", "label": "管理员清理队列"},
    {"code": "operator_request_cancelled", "label": "管理员取消请求"},
    {"code": "capacity_unavailable", "label": "暂时没有可用算力"},
)
KIND = "job_bulk_cancel"
SNAPSHOT = re.compile(r"^[a-f0-9]{64}$")
ACTIVE_STATUSES = ("planned", "blocked", "queued", "waiting_capacity", "claimed", "submitting",
    "submission_unknown", "running", "collecting", "cancel_requested", "recovery_hold")


class OperatorJobs:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings

    def _admin(self, principal):
        if not isinstance(principal, Principal) or principal.machine is not False:
            raise OperatorError("operator_jobs_human_required", 403)
        if principal.owner not in self.settings.operator_admin_owners:
            raise OperatorError("operator_admin_required", 403)

    def list(self, principal, *, limit=100, after_id=None):
        self._admin(principal)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OperatorError("operator_jobs_limit_invalid", 422)
        with self.repo.engine.connect() as connection:
            after = ""
            if after_id is not None:
                row = connection.execute(select(jobs.c.id).where(jobs.c.id == after_id,
                    jobs.c.tenant_id == self.settings.tenant_id)).first()
                if not row:
                    raise OperatorError("operator_jobs_cursor_unavailable", 410)
                after = after_id
            rows = list(connection.execute(select(*cancellation_columns(), jobs.c.owner_id, jobs.c.project_id,
                jobs.c.request_hash, jobs.c.created_at, jobs.c.updated_at).where(jobs.c.tenant_id == self.settings.tenant_id,
                jobs.c.status.in_(ACTIVE_STATUSES), jobs.c.id > after)
                .order_by(jobs.c.id).limit(limit + 1)).mappings())
            latest = {}
            if rows:
                sequences = select(dispatch_receipts.c.job_id, func.max(dispatch_receipts.c.sequence).label("sequence")).where(
                    dispatch_receipts.c.job_id.in_([row["id"] for row in rows[:limit]])).group_by(dispatch_receipts.c.job_id).subquery()
                latest = {receipt["job_id"]: receipt for receipt in connection.execute(select(dispatch_receipts).join(
                    sequences, (sequences.c.job_id == dispatch_receipts.c.job_id) &
                        (sequences.c.sequence == dispatch_receipts.c.sequence))).mappings()}
            values = [{"id": row["id"], "owner_id": row["owner_id"], "project_id": row["project_id"],
                "status": row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"],
                "snapshot": cancel_snapshot(row), "diagnostic": self._diagnostic(row, latest.get(row["id"])),
                **cancel_eligibility(connection, row)} for row in rows[:limit]]
        return {"operator": {"account": principal.owner, "admin": True}, "jobs": values,
            "next_after_id": rows[limit-1]["id"] if len(rows) > limit else None,
            "reason_codes": list(REASONS), "observed_at": self.repo.clock()}

    def _diagnostic(self, job, receipt):
        """Existing admin view only; no prompts, broker responses or retry grant."""
        from .generation_diagnostics import failure_reason
        reason = {"cancel_requested": "job_cancel_pending", "recovery_hold": "original_attempt_requires_reconciliation",
            "submission_unknown": "upstream_status_unknown", "collecting": "collection_pending",
            "waiting_capacity": "matching_capacity_unavailable", "queued": "awaiting_worker_claim"}.get(job["status"])
        delivery = None
        if receipt is not None:
            delivery = {"event_id": receipt["event_id"], "sequence": receipt["sequence"],
                "state": receipt["state"] if receipt["state"] in {"pending", "publishing", "published", "unknown", "finished", "obsolete", "blocked"} else "unknown",
                "publish_attempts": receipt["publish_attempts"], "updated_at": receipt["updated_at"],
                "reason_code": receipt["error_code"] if receipt["error_code"] in {
                    "dispatch_identity_mismatch", "dispatch_response_unknown", "dispatch_cancel_pending"} else None}
        return {"reason_code": reason, "error_code": failure_reason(job["error_code"]) if job["error_code"] else None,
            "age_seconds": max(0, self.repo.clock()-job["updated_at"]), "attempt_no": job["attempt_no"],
            "current_attempt_id": job["current_attempt_id"], "lease_worker_id": job["lease_worker_id"],
            "dispatch": delivery}

    def cancel(self, principal, body, key):
        self._admin(principal)
        if not isinstance(body, dict) or set(body) != {"jobs", "reason_code"}:
            raise OperatorError("operator_jobs_cancel_invalid", 422)
        selected = body["jobs"]
        if (not isinstance(selected, list) or not 1 <= len(selected) <= 100 or
                not isinstance(body["reason_code"], str) or
                body["reason_code"] not in {row["code"] for row in REASONS} or
                not isinstance(key, str) or not 1 <= len(key) <= 200 or key != key.strip() or
                any(ord(c) < 32 for c in key)):
            raise OperatorError("operator_jobs_cancel_invalid", 422)
        identities = []
        for row in selected:
            if (not isinstance(row, dict) or set(row) != {"id", "expected_snapshot"} or
                    not isinstance(row["id"], str) or not 1 <= len(row["id"]) <= 36 or
                    not isinstance(row["expected_snapshot"], str) or not SNAPSHOT.fullmatch(row["expected_snapshot"])):
                raise OperatorError("operator_jobs_cancel_invalid", 422)
            identities.append(row["id"])
        if len(set(identities)) != len(identities):
            raise OperatorError("operator_jobs_cancel_invalid", 422)
        actor = _actor(self.settings.tenant_id, principal.owner)
        digest = request_hash({"kind": KIND, "body": body})
        with self.repo.transaction() as connection:
            # Original operator journal serialization; no network/provider work.
            self.repo._lock_capacity(connection)
            existing = self.repo._locked(connection, select(operator_commands).where(
                operator_commands.c.actor == actor, operator_commands.c.idempotency_key == key))
            if existing:
                if existing["request_hash"] != digest:
                    raise Conflict("operator_idempotency_conflict")
                return copy.deepcopy(existing["payload"])
            cancelled = self.repo.cancel_queued_jobs(connection, tenant_id=self.settings.tenant_id,
                selections=selected, actor_id=principal.actor_id, reason_code=body["reason_code"])
            identity, now = str(uuid.uuid4()), self.repo.clock()
            result = {"command_id": identity, "state": "completed",
                "cancelled_job_ids": [row["id"] for row in cancelled], "reason_code": body["reason_code"]}
            connection.execute(insert(operator_commands).values(id=identity, actor=actor,
                idempotency_key=key, request_hash=digest, kind=KIND, state="completed",
                payload=result, reason_code=body["reason_code"], created_at=now, updated_at=now))
            self.repo._emit(connection, "operator.jobs_cancelled", identity,
                {"tenant_id": self.settings.tenant_id, "actor_id": principal.actor_id,
                 "reason_code": body["reason_code"], "jobs": [
                    {"job_id": row["id"], "owner_id": row["owner_id"], "project_id": row["project_id"]}
                    for row in cancelled]})
            return result


def register_routes(app):
    service = OperatorJobs(app.state.repository, app.state.settings)
    app.state.operator_jobs = service

    @app.get("/v1/operator/admin/jobs")
    def queue(request: Request, limit: int = Query(100, ge=1, le=100),
              after_id: str | None = Query(None, max_length=36)):
        return JSONResponse(service.list(request.state.principal, limit=limit, after_id=after_id),
            headers={"Cache-Control": "no-store"})

    @app.post("/v1/operator/admin/jobs/cancel")
    def cancel(request: Request, body: dict, key: str = Header(..., alias="Idempotency-Key")):
        return JSONResponse(service.cancel(request.state.principal, body, key),
            headers={"Cache-Control": "no-store"})

    return service
