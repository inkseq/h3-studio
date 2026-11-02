"""Preflight and accepted-batch execution on the original ledger authority."""

from __future__ import annotations

import copy

from sqlalchemy import (
    select,
)

from .capabilities import VERSION, version_matches
from .content_policy import ContentPolicyError
from .credits import CreditError
from .quick_chat_state import (
    QuickChatError,
    fields,
    objects,
)
from .repository import (
    BudgetExceeded,
    Conflict,
    NotFound,
    Scope,
    attempts,
    canonical,
    jobs,
    request_hash,
)

ADMISSION_REFRESH_CODES = frozenset(
    {
        "execution_policy_changed_or_unavailable",
        "capacity_approval_unavailable",
        "configuration_output_delivery_conflict",
        "job_reservation_requires_reconciliation",
        "upstream_stop_unconfirmed",
        "shot_version_conflict",
        "plan_expired",
        "budget_exceeded",
        "quick_chat_execution_required",
        "plan_not_available",
        "storyboard_defaults_changed",
        "chapter_timeline_changed",
        "preflight_stale",
    }
)


class QuickChatExecutionMixin:
    def _projection(self, principal, session, revision, item):
        scope = Scope(self.tenant, principal.owner, "__projects")
        record = self.repo.get_document(
            scope, "project", session["payload"]["project_id"]
        )
        from .source_snapshot import source_snapshot

        if (
            record["payload"].get("integration_kind") != "quick_chat"
            or source_snapshot(record["payload"], item["shot_id"])
            != item["projection_hash"]
        ):
            raise QuickChatError(
                "projection_changed", "执行投影与保存版本不一致；没有提交或覆盖任务。"
            )
        return record["payload"]

    def preflight(self, principal, session_id, revision_id, body, key):
        fields(
            body,
            {
                "capabilities_version",
                "revision_hash",
                "item_ids",
                "retry_of_execution_id",
            },
            {"capabilities_version", "revision_hash"},
        )
        if not version_matches(body["capabilities_version"], VERSION):
            raise QuickChatError(
                "preflight_stale", "生成能力已更新，请重新加载后预检。"
            )
        session = self._access(principal, session_id, "projects:read", "jobs:write")
        revision = self.get_revision(principal, session_id, revision_id)
        if body["revision_hash"] != revision["input_hash"]:
            raise QuickChatError("revision_hash_conflict", "任务卡内容校验不匹配。")
        selected = body.get("item_ids", [i["id"] for i in revision["items"]])
        if (
            not isinstance(selected, list)
            or not selected
            or len(set(selected)) != len(selected)
            or set(selected) - {i["id"] for i in revision["items"]}
        ):
            raise QuickChatError(
                "invalid_item_selection", "请选择这个版本中的不同生成项。", 422
            )
        namespace = "preflight:" + revision_id
        with self.repo.transaction() as conn:
            session = self._access(
                principal,
                session_id,
                "projects:read",
                "jobs:write",
                conn=conn,
                lock=True,
            )
            prior = self._replay(conn, principal, namespace, key, body)
            if prior:
                record = self._get(conn, principal, prior, session_id, "preflight")
            else:
                from .universal_trash import assert_take_active

                for item_id in selected:
                    assert_take_active(
                        conn, self.tenant, principal.owner, session_id, item_id
                    )
                existing = (
                    conn.execute(
                        select(objects).where(
                            objects.c.tenant == self.tenant,
                            objects.c.owner == principal.owner,
                            objects.c.kind == "submission",
                            objects.c.business_key == revision_id,
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing:
                    for item_id in selected:
                        live = self._get(conn, principal, item_id, session_id, "item")
                        self._safe_unexecuted_or_failed(
                            conn, principal, live, allow_failed=True
                        )
                record = self._new(
                    conn,
                    principal,
                    session_id,
                    "preflight",
                    {
                        "revision_id": revision_id,
                        "revision_hash": revision["input_hash"],
                        "status": "checking",
                        "items": [
                            {
                                "item_id": i["id"],
                                "index": i["index"],
                                "seed": i["seed"],
                                "plan": None,
                                "error_code": None,
                            }
                            for i in revision["items"]
                            if i["id"] in selected
                        ],
                        **(
                            {
                                "composer_snapshot": self._composer_snapshot(
                                    session["payload"]["bindings"],
                                    inputs=revision["inputs"],
                                    recipe_id=revision["recipe_id"],
                                    deployment_profile_id=revision.get(
                                        'deployment_profile_id'
                                    ),
                                )
                            }
                            if revision.get("composer_snapshot") is None
                            else {}
                        ),
                        "retry_of_execution_id": body.get("retry_of_execution_id"),
                        "expires_at": None,
                    },
                    parent=revision_id,
                )
                self._remember(conn, principal, namespace, key, body, record["id"])
        if self.hooks is None:
            raise QuickChatError(
                "execution_adapter_unavailable", "执行预检尚未配置。", 503
            )
        for index, entry in enumerate(record["payload"]["items"]):
            if entry["plan"] or entry["error_code"]:
                continue
            item = next(i for i in revision["items"] if i["id"] == entry["item_id"])
            try:
                project = self._projection(principal, session, revision, item)
                plan = self.hooks.preflight(principal, project, item["shot_id"])
                digest = request_hash(
                    {
                        "effective_request": plan.get("effective_request"),
                        "output_spec": plan.get("output_spec"),
                        "request_hash": plan.get("request_hash"),
                        "plan_id": plan["plan_id"],
                    }
                )
                value = {
                    **entry,
                    "plan": canonical(plan),
                    "resolved_execution_hash": digest,
                }
            except (
                ValueError,
                Conflict,
                BudgetExceeded,
                QuickChatError,
                ContentPolicyError,
                CreditError,
            ) as exc:
                from .generation_diagnostics import preflight_diagnostic

                value = {**entry, **preflight_diagnostic(exc)}
                if isinstance(exc, QuickChatError):
                    value.update(error_code=exc.code, error_message=exc.message)
                elif isinstance(exc, CreditError):
                    value.update(error_code=exc.code, error_message=exc.detail)
            with self.repo.transaction() as conn:
                current = self._get(
                    conn, principal, record["id"], session_id, "preflight", lock=True
                )
                items = copy.deepcopy(current["payload"]["items"])
                if items[index]["plan"] is None and not items[index]["error_code"]:
                    items[index] = value
                    self._put(conn, current, {**current["payload"], "items": items})
        with self.repo.transaction() as conn:
            session = self._access(
                principal, session_id, "jobs:write", conn=conn, lock=True
            )
            current = self._get(
                conn, principal, record["id"], session_id, "preflight", lock=True
            )
            p = current["payload"]
            if p["status"] == "checking":
                plans = [i["plan"] for i in p["items"] if i["plan"]]
                ready = len(plans) == len(p["items"]) and all(
                    v["status"] == "ready" for v in plans
                )
                runtime_values = []
                from .repository import plans as saved_plans

                for public_plan in plans:
                    saved = conn.execute(
                        select(saved_plans.c.execution_plan).where(
                            saved_plans.c.id == public_plan["plan_id"],
                            saved_plans.c.tenant_id == self.tenant,
                            saved_plans.c.owner_id == principal.owner,
                        )
                    ).scalar_one_or_none()
                    runtime = (
                        saved.get("expected_runtime_s")
                        if saved and saved.get("quote_known") is True
                        else None
                    )
                    runtime_values.append(
                        runtime
                        if type(runtime) in (int, float) and 0 <= runtime < float("inf")
                        else None
                    )
                machine_seconds = (
                    sum(runtime_values)
                    if len(runtime_values) == len(p["items"])
                    and all(v is not None for v in runtime_values)
                    else None
                )
                p = {
                    **p,
                    "status": "ready" if ready else "blocked",
                    "expires_at": min(
                        (v["expires_at"] for v in plans), default=self.repo.clock()
                    ),
                    "estimate_available": len(plans) == len(p["items"])
                    and all(
                        v.get("estimate", {}).get("cost_microusd") is not None
                        for v in plans
                    ),
                    "estimate": {
                        "currency": "USD",
                        "cost_microusd": sum(
                            (v.get("estimate", {}).get("cost_microusd") or 0)
                            for v in plans
                        ),
                        "kind": "budget_reservation",
                        "final_bill": False,
                        "machine_seconds": machine_seconds,
                        "runtime_kind": "planned_machine_allowance",
                        "eta_seconds": None,
                    },
                }
                self._put(conn, current, p)
                self._event(
                    conn, principal, session, "preflight.completed", current["id"]
                )
        return self.get_preflight(principal, session_id, record["id"])

    def get_preflight(self, principal, session_id, ident):
        # Retain the original scope requirement without treating a historical
        # GET as authoring on an adopted, read-only legacy session.
        session = self._access(principal, session_id, "projects:read")
        if not principal.allows(session["payload"]["project_id"], "jobs:write"):
            raise NotFound("quick_chat_session_not_found")
        with self.repo.engine.connect() as conn:
            row = self._get(conn, principal, ident, session_id, "preflight")
        stale = (
            row["payload"].get("expires_at") is not None
            and row["payload"]["expires_at"] <= self.repo.clock()
        )
        return {
            **row["payload"],
            "id": ident,
            "created_at": row["created_at"],
            "stale": stale,
            "stale_reason": "expired" if stale else None,
        }

    def _check_preflight(
        self, conn, principal, session_id, ident, revision_id, item_ids
    ):
        row = self._get(conn, principal, ident, session_id, "preflight")
        p = row["payload"]
        if (
            p["revision_id"] != revision_id
            or p["status"] != "ready"
            or p["expires_at"] <= self.repo.clock()
            or {i["item_id"] for i in p["items"]} != set(item_ids)
        ):
            raise QuickChatError(
                "preflight_stale", "请对指定生成项重新预检并确认费用。"
            )
        return p

    def _execution(self, conn, principal, session_id, item, plan_id, *, retry_of=None):
        business = "retry:" + retry_of if retry_of else "initial:" + item["id"]
        execution = self._new(
            conn,
            principal,
            session_id,
            "execution",
            {
                "item_id": item["id"],
                "plan_id": plan_id,
                "job_id": None,
                "status": "pending_admission",
                "error_code": None,
                "retry_of_execution_id": retry_of,
                "actor_id": principal.actor_id,
            },
            parent=item["id"],
            business=business,
        )
        self._put(
            conn,
            item,
            {
                **item["payload"],
                "current_execution_id": execution["id"],
                "cancel_requested": False,
            },
        )
        return execution

    def submit(self, principal, session_id, revision_id, body, key):
        fields(
            body,
            {"preflight_id", "revision_hash", "confirmed"},
            {"preflight_id", "revision_hash", "confirmed"},
        )
        if body["confirmed"] is not True:
            raise QuickChatError(
                "confirmation_required", "请明确确认这份预检和整批费用。", 422
            )
        revision = self.get_revision(principal, session_id, revision_id)
        if body["revision_hash"] != revision["input_hash"]:
            raise QuickChatError("revision_hash_conflict", "任务卡校验不匹配。")
        namespace = "submit:" + revision_id
        with self.repo.transaction() as conn:
            session = self._access(
                principal,
                session_id,
                "projects:read",
                "jobs:write",
                conn=conn,
                lock=True,
            )
            prior = self._replay(conn, principal, namespace, key, body)
            existing = (
                conn.execute(
                    select(objects).where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.kind == "submission",
                        objects.c.business_key == revision_id,
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                submission = dict(existing)
                if not prior:
                    self._remember(
                        conn, principal, namespace, key, body, submission["id"]
                    )
            else:
                from .universal_trash import assert_take_active

                for item in revision["items"]:
                    assert_take_active(
                        conn, self.tenant, principal.owner, session_id, item["id"]
                    )
                p = self._check_preflight(
                    conn,
                    principal,
                    session_id,
                    body["preflight_id"],
                    revision_id,
                    [i["id"] for i in revision["items"]],
                )
                submission = self._new(
                    conn,
                    principal,
                    session_id,
                    "submission",
                    {
                        "revision_id": revision_id,
                        "preflight_id": body["preflight_id"],
                        "actor_id": principal.actor_id,
                        "cancel_requested": False,
                        **(
                            {"composer_snapshot": copy.deepcopy(p["composer_snapshot"])}
                            if p.get("composer_snapshot") is not None
                            else {}
                        ),
                    },
                    parent=revision_id,
                    business=revision_id,
                )
                for entry in revision["items"]:
                    item = self._new(
                        conn,
                        principal,
                        session_id,
                        "item",
                        {
                            **entry,
                            "revision_id": revision_id,
                            "submission_id": submission["id"],
                            "current_execution_id": None,
                        },
                        ident=entry["id"],
                        parent=submission["id"],
                    )
                    plan = next(
                        v["plan"] for v in p["items"] if v["item_id"] == item["id"]
                    )
                    self._execution(conn, principal, session_id, item, plan["plan_id"])
                self._remember(conn, principal, namespace, key, body, submission["id"])
                self._event(
                    conn, principal, session, "submission.confirmed", submission["id"]
                )
        if not existing or prior:
            self._admit(principal, session_id, submission["id"])
        else:
            self._reset_accepted_composer(principal, session_id, submission["id"])
        return self.get_submission(principal, session_id, submission["id"])

    def _admit(
        self,
        principal,
        session_id,
        submission_id,
        selected=None,
        *,
        allow_blocked=False,
    ):
        self._access(principal, session_id, "jobs:write")
        if self.hooks is None:
            raise QuickChatError(
                "execution_adapter_unavailable", "执行入口尚未配置；确认记录保留。", 503
            )
        with self.repo.engine.connect() as conn:
            submission = self._get(
                conn, principal, submission_id, session_id, "submission"
            )
            items = [
                dict(r)
                for r in conn.execute(
                    select(objects).where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.session_id == session_id,
                        objects.c.kind == "item",
                        objects.c.parent_id == submission_id,
                    )
                ).mappings()
            ]
            pending_plans = []
            for item in items:
                if selected is not None and item["id"] not in selected:
                    continue
                execution = self._get(
                    conn,
                    principal,
                    item["payload"]["current_execution_id"],
                    session_id,
                    "execution",
                )
                if execution["payload"]["status"] in {
                    "pending_admission",
                    "admission_blocked",
                }:
                    job_id = execution["payload"].get("job_id")
                    if job_id:
                        status = conn.execute(
                            select(jobs.c.status).where(
                                jobs.c.id == job_id,
                                jobs.c.tenant_id == self.tenant,
                                jobs.c.owner_id == principal.owner,
                            )
                        ).scalar_one()
                        if status not in {"planned", "blocked"}:
                            continue  # reconcile the accepted original, no new grant
                    pending_plans.append(execution["payload"]["plan_id"])
        from .spending_limits import check_submission_confirmation

        check = check_submission_confirmation(
            self.repo,
            self.tenant,
            principal,
            [] if submission["payload"]["cancel_requested"] else pending_plans,
        )
        required = bool(check)
        if required:
            with self.repo.transaction() as conn:
                current = self._get(
                    conn, principal, submission_id, session_id, "submission", lock=True
                )
                self._put(
                    conn,
                    current,
                    {**current["payload"], "spending_confirmation_required": True},
                )
        required = required or submission["payload"].get(
            "spending_confirmation_required", False
        )
        for item in items:
            if selected is not None and item["id"] not in selected:
                continue
            # Cancellation is a durable obligation, checked on both sides of
            # job creation. The queue CAS handles a cancel/enqueue race once
            # the job is linked; never mark a linked job falsely cancelled.
            with self.repo.transaction() as conn:
                latest_submission = self._get(
                    conn, principal, submission_id, session_id, "submission", lock=True
                )
                item = self._get(
                    conn, principal, item["id"], session_id, "item", lock=True
                )
                execution = self._get(
                    conn,
                    principal,
                    item["payload"]["current_execution_id"],
                    session_id,
                    "execution",
                    lock=True,
                )
                cancel_requested = latest_submission["payload"][
                    "cancel_requested"
                ] or item["payload"].get("cancel_requested", False)
                if cancel_requested and not execution["payload"]["job_id"]:
                    self._put(
                        conn, execution, {**execution["payload"], "status": "cancelled"}
                    )
                    continue
            if execution["payload"]["status"] not in (
                {"pending_admission", "admission_blocked"}
                if allow_blocked
                else {"pending_admission"}
            ):
                continue
            try:
                p = execution["payload"]
                job = (
                    self.hooks.create_planned(principal, p["plan_id"], execution["id"])
                    if not p["job_id"]
                    else self.repo.get_job_for_owner(
                        self.tenant, principal.owner, p["job_id"]
                    )
                )
                with self.repo.transaction() as conn:
                    latest = self._get(
                        conn,
                        principal,
                        submission_id,
                        session_id,
                        "submission",
                        lock=True,
                    )
                    current_item = self._get(
                        conn, principal, item["id"], session_id, "item", lock=True
                    )
                    current = self._get(
                        conn,
                        principal,
                        execution["id"],
                        session_id,
                        "execution",
                        lock=True,
                    )
                    if current["payload"]["job_id"] not in (None, job["id"]):
                        raise QuickChatError(
                            "execution_identity_conflict", "已保存的任务身份不一致。"
                        )
                    self._put(
                        conn, current, {**current["payload"], "job_id": job["id"]}
                    )
                    cancel_requested = latest["payload"][
                        "cancel_requested"
                    ] or current_item["payload"].get("cancel_requested", False)
                if cancel_requested:
                    job = self.hooks.cancel(principal, job["id"])
                elif job["status"] in {"planned", "blocked"}:
                    try:
                        job = self.hooks.enqueue(
                            principal,
                            job,
                            confirmation_required=required,
                            spending_settings_version=check.version,
                        )
                    except Conflict:
                        job = self.repo.get_job_for_owner(
                            self.tenant, principal.owner, job["id"]
                        )
                        if job["status"] in {"planned", "blocked"}:
                            raise
                with self.repo.transaction() as conn:
                    current = self._get(
                        conn,
                        principal,
                        execution["id"],
                        session_id,
                        "execution",
                        lock=True,
                    )
                    self._put(
                        conn,
                        current,
                        {
                            **current["payload"],
                            "status": "admitted",
                            "error_code": None,
                        },
                    )
            except (
                Conflict,
                BudgetExceeded,
                ValueError,
                QuickChatError,
                ContentPolicyError,
                CreditError,
            ) as error:
                from .spending_limits import SpendingConfirmationRequired

                if isinstance(error, SpendingConfirmationRequired):
                    raise
                spending_codes = {
                    "user_daily_limit_reached",
                    "connection_daily_limit_reached",
                    "spending_usage_unconfirmed",
                    "connection_paused",
                    "connection_unavailable",
                    "connection_permission_denied",
                }
                code = (
                    error.code
                    if isinstance(
                        error, (QuickChatError, ContentPolicyError, CreditError)
                    )
                    else str(error)
                    if str(error) in spending_codes
                    else "budget_exceeded"
                    if isinstance(error, BudgetExceeded)
                    else "admission_blocked"
                )
                from .agent_management import record_refusal

                record_refusal(
                    self.repo.engine, self.tenant, principal, code, self.repo.clock()
                )
                with self.repo.transaction() as conn:
                    current = self._get(
                        conn,
                        principal,
                        execution["id"],
                        session_id,
                        "execution",
                        lock=True,
                    )
                    self._put(
                        conn,
                        current,
                        {
                            **current["payload"],
                            "status": "admission_blocked",
                            "error_code": code,
                        },
                    )
        self._reset_accepted_composer(principal, session_id, submission_id)

    def _reset_accepted_composer(self, principal, session_id, submission_id):
        """One durable reset after queue acceptance, never from a read/preflight.

        An interrupted enqueue is reconciled by replaying the original command;
        a receipt/batch alone does not prove that a video job was accepted.
        Session-first locking matches normal authoring/submission lock order.
        """
        with self.repo.transaction() as conn:
            session = self._access(
                principal, session_id, "jobs:write", conn=conn, lock=True
            )
            submission = self._get(
                conn, principal, submission_id, session_id, "submission", lock=True
            )
            if submission["payload"].get("composer_reset"):
                return
            revision = self._get(
                conn,
                principal,
                submission["payload"]["revision_id"],
                session_id,
                "revision",
            )
            snapshot = submission["payload"].get(
                "composer_snapshot", revision["payload"].get("composer_snapshot")
            )
            if snapshot is None:
                # A submission prepared before origin snapshots cannot safely
                # establish which later edits it owns. Never retrofit a read.
                return
            item_ids = select(objects.c.id).where(
                objects.c.tenant == self.tenant,
                objects.c.owner == principal.owner,
                objects.c.session_id == session_id,
                objects.c.kind == "item",
                objects.c.parent_id == submission_id,
            )
            executions = (
                conn.execute(
                    select(objects.c.payload).where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.session_id == session_id,
                        objects.c.kind == "execution",
                        objects.c.parent_id.in_(item_ids),
                    )
                )
                .scalars()
                .all()
            )
            job_ids = [p["job_id"] for p in executions if p.get("job_id")]
            if not job_ids:
                return
            accepted = conn.execute(
                select(jobs.c.id).where(
                    jobs.c.tenant_id == self.tenant,
                    jobs.c.owner_id == principal.owner,
                    jobs.c.project_id == session["payload"]["project_id"],
                    jobs.c.id.in_(job_ids),
                    jobs.c.status.in_(
                        (
                            "queued",
                            "waiting_capacity",
                            "claimed",
                            "submitting",
                            "submission_unknown",
                            "running",
                            "collecting",
                            "succeeded",
                            "failed",
                            "cancel_requested",
                            "recovery_hold",
                        )
                    ),
                )
            ).first()
            if accepted is None:
                return
            captured = {b["binding_id"]: b for b in snapshot["bindings"]}
            values, cleared, preserved = [], [], []
            for binding in session["payload"]["bindings"]:
                ident = binding["binding_id"]
                if binding.get("enabled") and binding == captured.get(ident):
                    values.append(
                        {**binding, "enabled": False, "version": binding["version"] + 1}
                    )
                    cleared.append(ident)
                else:
                    values.append(binding)
                    if binding.get("enabled"):
                        preserved.append(ident)
            receipt = {
                "status": "accepted",
                "submission_id": submission_id,
                "revision_id": revision["id"],
                "turn_id": revision["payload"].get("turn_id"),
                "session_version": session["version"] + 1,
                "cleared_binding_ids": cleared,
                "preserved_binding_ids": preserved,
            }
            self._put(
                conn,
                session,
                {**session["payload"], "bindings": values, "composer_reset": receipt},
                bump=True,
            )
            self._put(
                conn, submission, {**submission["payload"], "composer_reset": receipt}
            )
            self._event(
                conn, principal, session, "submission.composer_reset", submission_id
            )

    def get_submission(self, principal, session_id, submission_id):
        self._access(principal, session_id, "projects:read", "jobs:read")
        with self.repo.engine.connect() as conn:
            row = self._get(conn, principal, submission_id, session_id, "submission")
            children = [
                dict(r)
                for r in conn.execute(
                    select(objects).where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.session_id == session_id,
                        objects.c.kind == "item",
                        objects.c.parent_id == submission_id,
                    )
                ).mappings()
            ]
            executions = [
                dict(r)
                for r in conn.execute(
                    select(objects).where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.session_id == session_id,
                        objects.c.kind == "execution",
                        objects.c.parent_id.in_([c["id"] for c in children]),
                    )
                ).mappings()
            ]
        values = []
        for child in sorted(children, key=lambda r: r["payload"]["index"]):
            history = [
                {**r["payload"], "id": r["id"], "created_at": r["created_at"]}
                for r in executions
                if r["parent_id"] == child["id"]
            ]
            current = next(
                e
                for e in history
                if e["id"] == child["payload"]["current_execution_id"]
            )
            job = (
                self.hooks.public_job(principal, current["job_id"])
                if current["job_id"] and self.hooks
                else None
            )
            status = job["status"] if job else current["status"]
            if current["status"] in {"unknown", "recovery_hold"}:
                status = current["status"]
            if job and status in {"planned", "blocked"} and current["error_code"]:
                status = "admission_blocked"
            retryable, resumable = False, False
            if principal.allows(
                self._access(principal, session_id)["payload"]["project_id"],
                "jobs:write",
            ):
                try:
                    with self.repo.engine.connect() as conn:
                        safe_execution, safe_job = self._safe_unexecuted_or_failed(
                            conn, principal, child, allow_failed=True, lock=False
                        )
                    retryable = bool(
                        safe_job and safe_job["status"] in {"failed", "cancelled"}
                    )
                    resumable = (
                        not row["payload"]["cancel_requested"]
                        and not child["payload"].get("cancel_requested", False)
                        and safe_execution["payload"]["status"]
                        in {"pending_admission", "admission_blocked"}
                        and (
                            safe_job is None
                            or safe_job["status"] in {"planned", "blocked"}
                        )
                    )
                except (QuickChatError, Conflict, NotFound):
                    pass
            values.append(
                {
                    **child["payload"],
                    "id": child["id"],
                    "status": status,
                    "error_code": (job or {}).get("error_code")
                    or current["error_code"],
                    "job_id": current["job_id"],
                    "job": job,
                    "executions": history,
                    "retryable": retryable,
                    "resume_admission_available": resumable,
                }
            )
        states = [v["status"] for v in values]
        terminal = {"succeeded", "failed", "cancelled"}
        status = (
            "completed"
            if states and all(s == "succeeded" for s in states)
            else (
                "finished_with_issues"
                if states and all(s in terminal for s in states)
                else "active"
            )
        )
        return {
            **row["payload"],
            "id": submission_id,
            "items": values,
            "status": status,
            "counts": {s: states.count(s) for s in sorted(set(states))},
            "created_at": row["created_at"],
            "updated_at": max(
                [
                    row["updated_at"],
                    *(
                        (i["job"] or {}).get("updated_at", row["updated_at"])
                        for i in values
                    ),
                ]
            ),
            "web_url": "/quick-chat?session="
            + session_id
            + "&submission="
            + submission_id,
        }

    def _safe_unexecuted_or_failed(
        self, conn, principal, item, *, allow_failed=False, lock=True
    ):
        from .universal_trash import assert_take_active

        assert_take_active(
            conn, self.tenant, principal.owner, item["session_id"], item["id"]
        )
        execution = self._get(
            conn,
            principal,
            item["payload"]["current_execution_id"],
            item["session_id"],
            "execution",
            lock=lock,
        )
        p = execution["payload"]
        if p["status"] in {"unknown", "recovery_hold"}:
            raise QuickChatError(
                "upstream_stop_unconfirmed",
                "恢复后的执行须先对账；不会根据缺失job ID再次生成。",
            )
        if not p["job_id"]:
            if p["status"] not in {
                "pending_admission",
                "admission_blocked",
                "cancelled",
            }:
                raise QuickChatError(
                    "upstream_stop_unconfirmed", "此项执行状态需核验；不会再次生成。"
                )
            return execution, None
        job = self.repo._job(
            conn,
            p["job_id"],
            Scope(
                self.tenant,
                principal.owner,
                self._get(
                    conn, principal, item["session_id"], item["session_id"], "session"
                )["payload"]["project_id"],
            ),
            lock=lock,
        )
        history = (
            conn.execute(select(attempts).where(attempts.c.job_id == job["id"]))
            .mappings()
            .all()
        )
        current_id = job.get("current_attempt_id")
        current = next((a for a in history if a["id"] == current_id), None)
        if current_id is not None and (
            current is None
            or job["attempt_no"] < 1
            or current["number"] != job["attempt_no"]
        ):
            # An orphan reference remains an obligation even when recovered
            # summary counters are empty or another historical row is stopped.
            raise QuickChatError(
                "upstream_stop_unconfirmed", "当前执行身份须先核验；不会再次生成。"
            )
        safe_unsubmitted = (
            job["status"] in {"planned", "blocked", "cancelled"}
            and not job["attempt_no"]
            and not history
        )
        safe_failed = (
            allow_failed
            and job["status"] in {"failed", "cancelled"}
            and len(history) >= job["attempt_no"]
            and all(a["upstream_stopped"] == 1 for a in history)
        )
        if (
            job.get("lease_worker_id")
            or job.get("lease_expires_at") is not None
            or not (safe_unsubmitted or safe_failed)
        ):
            raise QuickChatError(
                "upstream_stop_unconfirmed", "只能恢复未执行项或已证明停止的失败项。"
            )
        return execution, job

    def retry(self, principal, session_id, submission_id, item_id, body, key):
        fields(
            body,
            {"retry_of_execution_id", "fresh_preflight_id", "confirmed"},
            {"retry_of_execution_id", "fresh_preflight_id", "confirmed"},
        )
        if body["confirmed"] is not True:
            raise QuickChatError(
                "confirmation_required", "请确认此份重试预留费用。", 422
            )
        namespace = "retry:" + item_id
        with self.repo.transaction() as conn:
            session = self._access(
                principal,
                session_id,
                "projects:read",
                "jobs:write",
                conn=conn,
                lock=True,
            )
            submission = self._get(
                conn, principal, submission_id, session_id, "submission", lock=True
            )
            item = self._get(conn, principal, item_id, session_id, "item", lock=True)
            if item["parent_id"] != submission_id:
                raise NotFound("item_not_found")
            prior = self._replay(conn, principal, namespace, key, body)
            source = self._get(
                conn, principal, body["retry_of_execution_id"], session_id, "execution"
            )
            if source["parent_id"] != item_id:
                raise NotFound("execution_not_found")
            existing = (
                conn.execute(
                    select(objects).where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.kind == "execution",
                        objects.c.business_key == "retry:" + source["id"],
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if not prior:
                    self._remember(
                        conn, principal, namespace, key, body, existing["id"]
                    )
            else:
                if item["payload"]["current_execution_id"] != source["id"]:
                    raise QuickChatError(
                        "version_conflict", "此份已经有新的执行，请加载当前状态。"
                    )
                if submission["payload"]["cancel_requested"]:
                    raise QuickChatError(
                        "submission_cancelled",
                        "整批已取消，请另建卡片版本；不会恢复已取消批次。",
                    )
                execution, job = self._safe_unexecuted_or_failed(
                    conn, principal, item, allow_failed=True
                )
                if job is None or job["status"] not in {"failed", "cancelled"}:
                    raise QuickChatError(
                        "use_resume_admission", "未准入项请恢复原项，不重新生成。"
                    )
                preflight = self._check_preflight(
                    conn,
                    principal,
                    session_id,
                    body["fresh_preflight_id"],
                    item["payload"]["revision_id"],
                    [item_id],
                )
                if preflight.get("retry_of_execution_id") != source["id"]:
                    raise QuickChatError(
                        "preflight_stale", "此预检不属于要重试的原执行。"
                    )
                new = self._execution(
                    conn,
                    principal,
                    session_id,
                    item,
                    preflight["items"][0]["plan"]["plan_id"],
                    retry_of=execution["id"],
                )
                self._remember(conn, principal, namespace, key, body, new["id"])
                self._event(
                    conn, principal, session, "item.retry_requested", submission_id
                )
        self._admit(principal, session_id, submission_id, [item_id])
        return self.get_submission(principal, session_id, submission_id)

    def resume_admission(self, principal, session_id, submission_id, body, key):
        fields(
            body,
            {"item_ids", "fresh_preflight_id", "confirmed"},
            {"item_ids", "fresh_preflight_id", "confirmed"},
        )
        if (
            body["confirmed"] is not True
            or not isinstance(body["item_ids"], list)
            or not body["item_ids"]
            or len(set(body["item_ids"])) != len(body["item_ids"])
        ):
            raise QuickChatError(
                "invalid_item_selection", "请明确确认要恢复的不同未准入项。", 422
            )
        refresh = []
        namespace = "resume:" + submission_id
        with self.repo.transaction() as conn:
            session = self._access(
                principal,
                session_id,
                "projects:read",
                "jobs:write",
                conn=conn,
                lock=True,
            )
            submission = self._get(
                conn, principal, submission_id, session_id, "submission", lock=True
            )
            if submission["payload"]["cancel_requested"]:
                raise QuickChatError(
                    "cancel_requested", "已取消批次不能恢复准入；请创建新版本。"
                )
            prior = self._replay(conn, principal, namespace, key, body)
            if not prior:
                preflight = self._check_preflight(
                    conn,
                    principal,
                    session_id,
                    body["fresh_preflight_id"],
                    submission["payload"]["revision_id"],
                    body["item_ids"],
                )
                for item_id in body["item_ids"]:
                    item = self._get(
                        conn, principal, item_id, session_id, "item", lock=True
                    )
                    if item["parent_id"] != submission_id:
                        raise NotFound("item_not_found")
                    execution, job = self._safe_unexecuted_or_failed(
                        conn, principal, item
                    )
                    if item["payload"].get("cancel_requested", False):
                        raise QuickChatError(
                            "cancel_requested", "此项已取消，未恢复准入。"
                        )
                    if job and job["status"] not in {"planned", "blocked"}:
                        raise QuickChatError(
                            "use_failed_item_retry", "已取消项请使用单份重试。"
                        )
                    plan_id = next(
                        i["plan"]["plan_id"]
                        for i in preflight["items"]
                        if i["item_id"] == item_id
                    )
                    payload = {
                        **execution["payload"],
                        "resume_plan_id": plan_id,
                        "status": "pending_admission",
                        "error_code": None,
                    }
                    if not job:
                        payload["plan_id"] = plan_id
                        payload["resume_plan_id"] = None
                    elif execution["payload"].get("resume_plan_id"):
                        raise QuickChatError(
                            "admission_resume_active",
                            "原恢复命令尚未对账，请先查看或恢复该命令；不会叠加另一份计划。",
                        )
                    self._put(conn, execution, payload)
                self._remember(conn, principal, namespace, key, body, submission_id)
                self._event(
                    conn,
                    principal,
                    session,
                    "submission.admission_resumed",
                    submission_id,
                )
            for item_id in body["item_ids"]:
                item = self._get(conn, principal, item_id, session_id, "item")
                execution = self._get(
                    conn,
                    principal,
                    item["payload"]["current_execution_id"],
                    session_id,
                    "execution",
                )
                if (
                    execution["payload"].get("resume_plan_id")
                    and execution["payload"]["job_id"]
                ):
                    refresh.append(execution)
        blocked = set()
        for execution in refresh:
            if not self.hooks or self.hooks.refresh_planned is None:
                raise QuickChatError(
                    "preflight_stale", "此原计划已过期，安全刷新尚未配置；任务保留。"
                )
            p = execution["payload"]
            try:
                self.hooks.refresh_planned(principal, p["job_id"], p["resume_plan_id"])
            except (
                Conflict,
                BudgetExceeded,
                ValueError,
                QuickChatError,
                ContentPolicyError,
                CreditError,
            ) as error:
                # Only finite service codes are public; arbitrary exception
                # text may contain source inputs, paths or provider details.
                code = (
                    error.code
                    if isinstance(
                        error, (QuickChatError, ContentPolicyError, CreditError)
                    )
                    else (error.args[0] if len(error.args) == 1 else None)
                )
                if not isinstance(error, (ContentPolicyError, CreditError)) and (
                    not isinstance(code, str) or code not in ADMISSION_REFRESH_CODES
                ):
                    code = "admission_refresh_blocked"
                with self.repo.transaction() as conn:
                    current = self._get(
                        conn,
                        principal,
                        execution["id"],
                        session_id,
                        "execution",
                        lock=True,
                    )
                    if current["payload"].get("resume_plan_id") == p["resume_plan_id"]:
                        self._put(
                            conn,
                            current,
                            {
                                **current["payload"],
                                "resume_plan_id": None,
                                "status": "admission_blocked",
                                "error_code": code,
                            },
                        )
                blocked.add(p["item_id"])
                continue
            with self.repo.transaction() as conn:
                current = self._get(
                    conn, principal, execution["id"], session_id, "execution", lock=True
                )
                if current["payload"].get("resume_plan_id") == p["resume_plan_id"]:
                    self._put(
                        conn,
                        current,
                        {
                            **current["payload"],
                            "plan_id": p["resume_plan_id"],
                            "resume_plan_id": None,
                        },
                    )
        self._admit(
            principal,
            session_id,
            submission_id,
            [i for i in body["item_ids"] if i not in blocked],
        )
        return self.get_submission(principal, session_id, submission_id)

    def cancel(self, principal, session_id, submission_id, body, key):
        fields(body, {"item_ids"})
        with self.repo.transaction() as conn:
            session = self._access(
                principal, session_id, "jobs:write", conn=conn, lock=True
            )
            submission = self._get(
                conn, principal, submission_id, session_id, "submission", lock=True
            )
            items = [
                dict(r)
                for r in conn.execute(
                    select(objects).where(
                        objects.c.tenant == self.tenant,
                        objects.c.owner == principal.owner,
                        objects.c.session_id == session_id,
                        objects.c.kind == "item",
                        objects.c.parent_id == submission_id,
                    )
                ).mappings()
            ]
            selected = body.get("item_ids", [i["id"] for i in items])
            if (
                not isinstance(selected, list)
                or not selected
                or set(selected) - {i["id"] for i in items}
            ):
                raise QuickChatError(
                    "invalid_item_selection", "取消项不属于此批次。", 422
                )
            prior = self._replay(conn, principal, "cancel:" + submission_id, key, body)
            if not prior:
                if set(selected) == {i["id"] for i in items}:
                    self._put(
                        conn,
                        submission,
                        {**submission["payload"], "cancel_requested": True},
                    )
                for item in items:
                    if item["id"] in selected:
                        self._put(
                            conn, item, {**item["payload"], "cancel_requested": True}
                        )
                self._remember(
                    conn, principal, "cancel:" + submission_id, key, body, submission_id
                )
                self._event(
                    conn,
                    principal,
                    session,
                    "submission.cancel_requested",
                    submission_id,
                )
            executions = [
                self._get(
                    conn,
                    principal,
                    i["payload"]["current_execution_id"],
                    session_id,
                    "execution",
                )
                for i in items
                if i["id"] in selected
            ]
        for execution in executions:
            if execution["payload"]["job_id"]:
                self.hooks.cancel(principal, execution["payload"]["job_id"])
            else:
                with self.repo.transaction() as conn:
                    current = self._get(
                        conn,
                        principal,
                        execution["id"],
                        session_id,
                        "execution",
                        lock=True,
                    )
                    self._put(
                        conn, current, {**current["payload"], "status": "cancelled"}
                    )
        return self.get_submission(principal, session_id, submission_id)
