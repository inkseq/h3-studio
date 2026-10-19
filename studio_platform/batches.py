"""Durable, resumable batch admission. Each item retains its own idempotency key.

A batch is deliberately not an all-or-nothing purchase: rejected items retain an
explanation, accepted items remain visible and cancellable, and retries only
admit previously unfinished items. Provider execution never happens here.
"""

from __future__ import annotations

import time
import uuid
from sqlalchemy import (
    Column,
    Float,
    JSON,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from fastapi import Header, HTTPException, Query, Request

from .content_policy import ContentPolicyError
from .credits import CreditError
from .repository import (
    Scope,
    Conflict,
    BudgetExceeded,
    NotFound,
    identifier,
    request_hash,
)

metadata = MetaData()
batches = Table(
    "platform_batches",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant", String(200), nullable=False),
    Column("owner", String(200), nullable=False),
    Column("project_id", String(200), nullable=False),
    Column("actor_id", String(200), nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("items", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("cancel_requested", Float),
    UniqueConstraint("tenant", "owner", "project_id", "actor_id", "idempotency_key"),
)


class BatchService:
    def __init__(self, repo, tenant):
        self.repo, self.tenant = repo, tenant
        metadata.create_all(repo.engine)

    def get(self, principal, batch_id):
        with self.repo.engine.connect() as conn:
            row = (
                conn.execute(
                    select(batches).where(
                        batches.c.id == batch_id,
                        batches.c.tenant == self.tenant,
                        batches.c.owner == principal.owner,
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            raise NotFound("batch_not_found")
        return dict(row)

    def list_page(self, principal, project_id, *, limit=10, offset=0):
        if (
            type(limit) is not int
            or type(offset) is not int
            or not 1 <= limit <= 10
            or not 0 <= offset <= 100000
        ):
            raise ValueError("分页参数无效")
        with self.repo.engine.connect() as conn:
            values = [
                dict(row)
                for row in conn.execute(
                    select(batches)
                    .where(
                        batches.c.tenant == self.tenant,
                        batches.c.owner == principal.owner,
                        batches.c.project_id == project_id,
                    )
                    .order_by(batches.c.created_at.desc(), batches.c.id)
                    .limit(limit + 1)
                    .offset(offset)
                ).mappings()
            ]
        return values[:limit], len(values) > limit

    def list(self, principal, project_id, *, limit=10, offset=0):
        return self.list_page(principal, project_id, limit=limit, offset=offset)[0]

    def reserve(self, principal, project_id, plan_ids, key):
        identifier(key)
        if (
            not isinstance(plan_ids, list)
            or not 1 <= len(plan_ids) <= 100
            or any(not isinstance(p, str) or not p or len(p) > 200 for p in plan_ids)
            or len(set(plan_ids)) != len(plan_ids)
        ):
            raise ValueError("批次需要1至100个不同的已预检计划")
        digest = request_hash({"project_id": project_id, "plan_ids": plan_ids})
        where = (
            batches.c.tenant == self.tenant,
            batches.c.owner == principal.owner,
            batches.c.project_id == project_id,
            batches.c.actor_id == principal.actor_id,
            batches.c.idempotency_key == key,
        )
        try:
            with self.repo.transaction() as conn:
                row = self.repo._locked(conn, select(batches).where(*where))
                if row is None:
                    record = dict(
                        id=str(uuid.uuid4()),
                        tenant=self.tenant,
                        owner=principal.owner,
                        project_id=project_id,
                        actor_id=principal.actor_id,
                        idempotency_key=key,
                        fingerprint=digest,
                        items=[
                            {"plan_id": p, "job_id": None, "error_code": None}
                            for p in plan_ids
                        ],
                        created_at=time.time(),
                        cancel_requested=None,
                    )
                    conn.execute(insert(batches).values(**record))
                    return record
                if row["fingerprint"] != digest:
                    raise Conflict("batch_idempotency_conflict")
                return dict(row)
        except IntegrityError:
            with self.repo.engine.connect() as conn:
                row = conn.execute(select(batches).where(*where)).mappings().first()
            if row and row["fingerprint"] == digest:
                return dict(row)
            raise Conflict("batch_idempotency_conflict") from None

    def record_item(self, principal, batch_id, index, value):
        with self.repo.transaction() as conn:
            row = self.repo._locked(
                conn,
                select(batches).where(
                    batches.c.id == batch_id,
                    batches.c.tenant == self.tenant,
                    batches.c.owner == principal.owner,
                ),
            )
            if not row:
                raise NotFound("batch_not_found")
            items = list(row["items"])
            previous = items[index]
            # A durable task link must survive a later budget/enqueue failure.
            # Keep its identity and expose the admission error for safe retry.
            if previous.get("job_id") and value.get("job_id") not in {
                None,
                previous["job_id"],
            }:
                raise Conflict("batch_item_identity_conflict")
            if previous.get("job_id") and value.get("error_code"):
                current = self.repo._job(
                    conn,
                    previous["job_id"],
                    Scope(self.tenant, principal.owner, row["project_id"]),
                )
                if current["status"] != "planned":
                    value = {**value, "error_code": None}
            items[index] = {**previous, **value}
            conn.execute(
                update(batches).where(batches.c.id == batch_id).values(items=items)
            )
            return bool(row["cancel_requested"])

    def cancel(self, principal, batch_id):
        with self.repo.transaction() as conn:
            row = self.repo._locked(
                conn,
                select(batches).where(
                    batches.c.id == batch_id,
                    batches.c.tenant == self.tenant,
                    batches.c.owner == principal.owner,
                ),
            )
            if not row:
                raise NotFound("batch_not_found")
            conn.execute(
                update(batches)
                .where(batches.c.id == batch_id)
                .values(cancel_requested=time.time())
            )
        return self.get(principal, batch_id)


def register_routes(app):
    repo = app.state.repository
    service = BatchService(repo, app.state.settings.tenant_id)
    app.state.batches = service

    def activate(principal, record, job_id):
        latest = service.get(principal, record["id"])
        task_scope = app.state.scope(principal, record["project_id"])
        if latest["cancel_requested"]:
            repo.request_cancel(task_scope, job_id)
            return
        job = repo.get_job(task_scope, job_id)
        if job["status"] == "planned":
            # Admission is durable before a worker can claim the task. A crash
            # between creation and linking leaves an inert, idempotent task.
            try:
                required = any(
                    item.get("confirmation_required")
                    for item in latest["items"]
                    if item.get("job_id") == job_id
                )
                version = next(
                    (
                        item.get("spending_settings_version")
                        for item in latest["items"]
                        if item.get("job_id") == job_id
                    ),
                    None,
                )
                app.state.enqueue_planned(
                    principal,
                    job,
                    confirmation_required=required,
                    spending_settings_version=version,
                )
            except Conflict:
                # A racing retry/cancel may already have transitioned this job.
                if repo.get_job(task_scope, job_id)["status"] == "planned":
                    raise

    def visible(
        principal,
        record,
        *,
        job_records=None,
        artifact_records=None,
        statuses=None,
        summary=False,
    ):
        cancellation = {}
        if not summary:
            app.state.authorized_project(principal, record["project_id"], "jobs:read")
            pairs = {
                item["job_id"]: record["project_id"]
                for item in record["items"]
                if item["job_id"]
            }
            job_records = repo.get_job_summaries_for_owner(
                service.tenant, principal.owner, pairs
            )
            cancellation = repo.get_job_cancellation_fields(
                service.tenant,
                principal.owner,
                pairs,
                expected_states={
                    ident: {
                        key: row[key] for key in ("status", "updated_at", "error_code")
                    }
                    for ident, row in job_records.items()
                },
            )
            artifact_records = repo.list_artifacts_for_jobs(
                service.tenant,
                principal.owner,
                {
                    ident: record["project_id"]
                    for ident, row in job_records.items()
                    if row["status"] == "succeeded"
                },
            )
        statuses = statuses or {
            ident: row["status"] for ident, row in (job_records or {}).items()
        }
        items = []
        for item in record["items"]:
            value = dict(item)
            if item["job_id"]:
                state = statuses[item["job_id"]]
                if not summary:
                    value["job"] = app.state.public_job(
                        job_records[item["job_id"]],
                        artifact_records=artifact_records.get(item["job_id"], []),
                        cancellation=cancellation[item["job_id"]],
                    )
                value["status"] = (
                    "admission_blocked"
                    if state == "planned" and item["error_code"]
                    else state
                )
            else:
                value["status"] = (
                    "rejected"
                    if item["error_code"]
                    else (
                        "cancelled"
                        if record["cancel_requested"]
                        else "pending_admission"
                    )
                )
            items.append(value)
        states = [v["status"] for v in items]
        if all(s in {"succeeded", "failed", "cancelled", "rejected"} for s in states):
            status = (
                "completed"
                if all(s == "succeeded" for s in states)
                else "finished_with_issues"
            )
        elif record["cancel_requested"]:
            status = "cancel_requested"
        elif any(s == "pending_admission" for s in states):
            status = "admitting"
        elif all(s in {"blocked", "admission_blocked", "rejected"} for s in states):
            status = "blocked"
        else:
            status = "active"
        return {
            "id": record["id"],
            "batch_id": record["id"],
            "client_project_id": record["project_id"],
            "status": status,
            "created_at": record["created_at"],
            "items": items,
            "cancel_requested": bool(record["cancel_requested"]),
            "counts": {s: states.count(s) for s in sorted(set(states))},
            "summary": summary,
        }

    @app.post("/v1/batches", status_code=202)
    def submit_batch(
        request: Request,
        body: dict,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        return submit_generation_batch(request.state.principal, body, idempotency_key)

    def submit_generation_batch(principal, body, idempotency_key):
        if set(body) != {"client_project_id", "plan_ids"}:
            raise HTTPException(422, "批次仅接受项目与已预检计划列表")
        project_id = body["client_project_id"]
        from .generation_admission import reject_managed

        reject_managed(
            app.state.authorized_project(principal, project_id, "jobs:write")["payload"]
        )
        # Reservation occurs before per-item validation so partial admission can
        # recover after a process crash. Ownership is still checked for every plan.
        record = service.reserve(
            principal, project_id, body["plan_ids"], idempotency_key
        )
        from .spending_limits import check_submission_confirmation

        pending_plans = []
        for item in record["items"]:
            if not item["job_id"] and not item["error_code"]:
                pending_plans.append(item["plan_id"])
            elif item["job_id"]:
                job = repo.get_job_for_owner(
                    service.tenant, principal.owner, item["job_id"]
                )
                if job["status"] in {"planned", "blocked"}:
                    pending_plans.append(item["plan_id"])
        check = check_submission_confirmation(
            repo,
            service.tenant,
            principal,
            [] if record["cancel_requested"] else pending_plans,
        )
        for index, item in enumerate(record["items"]):
            if item["plan_id"] in pending_plans:
                service.record_item(
                    principal,
                    record["id"],
                    index,
                    {
                        "spending_settings_version": check.version,
                        **({"confirmation_required": True} if check else {}),
                    },
                )
        for index, item in enumerate(record["items"]):
            if not item["job_id"] and (
                item["error_code"]
                or service.get(principal, record["id"])["cancel_requested"]
            ):
                continue
            try:
                if item["job_id"]:
                    activate(principal, record, item["job_id"])
                    service.record_item(
                        principal, record["id"], index, {"error_code": None}
                    )
                    continue
                plan = app.state.owned_plan(principal, item["plan_id"])
                if plan["project_id"] != project_id:
                    raise NotFound("plan_not_found")
                job = app.state.create_from_plan(
                    principal,
                    item["plan_id"],
                    f'batch:{record["id"]}:{index}',
                    initial_status="planned",
                )
                service.record_item(
                    principal,
                    record["id"],
                    index,
                    {"job_id": job["id"], "error_code": None},
                )
                activate(principal, record, job["id"])
            except (
                NotFound,
                Conflict,
                BudgetExceeded,
                ValueError,
                ContentPolicyError,
                CreditError,
            ) as error:
                from .agent_management import record_refusal

                record_refusal(
                    service.repo.engine,
                    app.state.settings.tenant_id,
                    principal,
                    str(error),
                    service.repo.clock(),
                )
                from .spending_limits import SpendingConfirmationRequired

                if isinstance(error, SpendingConfirmationRequired):
                    raise
                if isinstance(error, (ContentPolicyError, CreditError)):
                    code = error.code
                elif isinstance(error, BudgetExceeded):
                    code = str(error)
                    if code not in {
                        "budget_exceeded",
                        "connection_daily_limit_reached",
                        "user_daily_limit_reached",
                        "spending_usage_unconfirmed",
                    }:
                        code = "budget_exceeded"
                else:
                    code = (
                        "plan_not_available"
                        if isinstance(error, NotFound)
                        else "plan_no_longer_valid"
                    )
                service.record_item(
                    principal, record["id"], index, {"error_code": code}
                )
        return visible(principal, service.get(principal, record["id"]))

    app.state.submit_generation_batch = submit_generation_batch

    @app.get("/v1/batches")
    def list_batches(
        request: Request,
        client_project_id: str,
        limit: int = Query(10, ge=1, le=10),
        offset: int = Query(0, ge=0, le=100000),
    ):
        principal = request.state.principal
        app.state.authorized_project(principal, client_project_id, "jobs:read")
        records, has_more = service.list_page(
            principal, client_project_id, limit=limit, offset=offset
        )
        statuses = repo.get_job_statuses_for_owner(
            service.tenant,
            principal.owner,
            {
                item["job_id"]: row["project_id"]
                for row in records
                for item in row["items"]
                if item["job_id"]
            },
        )
        return {
            "batches": [
                visible(principal, row, statuses=statuses, summary=True)
                for row in records
            ],
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        }

    @app.get("/v1/batches/{batch_id}")
    def get_batch(batch_id: str, request: Request):
        return visible(
            request.state.principal, service.get(request.state.principal, batch_id)
        )

    @app.post("/v1/batches/{batch_id}/cancel")
    def cancel_batch(batch_id: str, request: Request):
        principal = request.state.principal
        current = service.get(principal, batch_id)
        app.state.authorized_project(principal, current["project_id"], "jobs:write")
        record = service.cancel(principal, batch_id)
        for item in record["items"]:
            if item["job_id"]:
                repo.request_cancel(
                    app.state.scope(principal, record["project_id"]), item["job_id"]
                )
        return visible(principal, service.get(principal, batch_id))
