"""Original jobs ledger methods on the composed Repository transaction owner."""

from __future__ import annotations
from contextlib import nullcontext
import re
import uuid
from sqlalchemy import (
    and_,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from .providers.registry import REAL_GPU_BACKENDS

from .repository_schema import (
    NotFound,
    Conflict,
    InvalidTransition,
    request_hash,
    money,
    identifier,
    plans,
    budget_accounts,
    jobs,
    attempts,
    budget_reservations,
    artifacts,
    instance_intents,
    capacity_gate,
    registered_workers,
    capacity_waiters,
    _allowed_ids,
    _pagination,
)


class JobLedger:
    def _require_pool_not_stopping(
        self, connection, pool, execution, compiled, *, scope=None
    ):
        """Close the preflight/shutdown race while holding the global gate.

        Already committed jobs return idempotently before this check. A new
        stale warm-capacity plan receives an explicit conflict and must be
        preflighted against a fresh cold-start approval; it is never silently
        queued onto a destroyed instance or changed into a different purchase.
        """
        if (
            execution.get("backend") not in REAL_GPU_BACKENDS
            or execution.get("enabled") is not True
            or execution.get("admission_state") == "waiting_capacity"
        ):
            return
        stopping = set(
            connection.execute(
                select(
                    instance_intents.c.provider, instance_intents.c.provider_instance_id
                ).where(
                    instance_intents.c.pool == pool,
                    instance_intents.c.state.in_(
                        ("draining", "destroying", "destroyed")
                    ),
                    instance_intents.c.provider_instance_id.is_not(None),
                )
            ).tuples()
        )
        if not stopping:
            return
        if execution.get("pool") == pool and execution.get("demand_start") is not None:
            from .dstack_demand import cold_admission_current

            if cold_admission_current(self, connection, scope, compiled, execution):
                return
        workers = connection.execute(
            select(registered_workers).where(registered_workers.c.pool == pool)
        ).mappings()
        now = self.clock()
        for worker in workers:
            spec = worker["spec"]
            if (
                worker["state"] in ("ready", "leased", "busy", "reconciling")
                and not worker["drain_requested"]
                and worker["expires_at"] > now
                and (worker["provider"], worker["instance_id"]) not in stopping
                and spec.get("backend") == execution.get("backend")
                and (
                    execution.get("backend") != "wangp-worker"
                    or spec.get("engine_manifest_digest")
                    == execution.get("engine_manifest_digest")
                )
                and spec.get("configuration_id") == execution.get("configuration_id")
                and spec.get("model_id")
                == compiled.get("request", compiled).get("model")
                and compiled.get("recipe_id") in spec.get("recipe_ids", ())
            ):
                return
        raise Conflict("capacity_drain_repreflight")

    def _check_generation_policy(
        self, connection, scope, plan, *, admitting=True, idempotency_key=None
    ):
        """Check new work only, under the caller's original account gate."""
        from .content_policy import (
            ContentPolicyError,
            require_generation_terms,
            verify_input,
        )

        compiled, execution = plan["request"], plan["execution_plan"]
        if idempotency_key is not None and "film_source_ref" in compiled:
            from .film_runs import accepted_policy_admission

            if accepted_policy_admission(
                connection, self, scope, plan, idempotency_key
            ):
                return  # Only the exact child of an already accepted Film run.
        verify_input(
            connection,
            scope.tenant_id,
            scope.owner_id,
            compiled,
            compiled.get("content_moderation"),
        )
        if admitting:
            current = require_generation_terms(
                connection, scope.tenant_id, scope.owner_id, execution, compiled
            )
            if current != execution.get("terms_acceptance"):
                raise ContentPolicyError("terms_version_changed", 409)

    def create_job(
        self,
        scope,
        plan_id,
        idempotency_key,
        *,
        budget_account_ids=(),
        initial_status="queued",
        spending_actor=None,
        confirmation_required=False,
        spending_settings_version=None,
    ):
        identifier(idempotency_key)
        if initial_status not in ("queued", "planned", "blocked", "waiting_capacity"):
            raise ValueError("invalid_initial_status")
        namespace = and_(
            self._scope(jobs, scope),
            jobs.c.actor_id == scope.actor_id,
            jobs.c.idempotency_key == idempotency_key,
        )
        try:
            with self.transaction() as connection:
                from .identity import account_status, lock_account_gate

                lock_account_gate(connection)
                account = account_status(connection, scope.tenant_id, scope.owner_id)
                if account != "active":
                    raise Conflict(
                        "account_suspended"
                        if account == "suspended"
                        else "account_unavailable"
                    )
                self._content_write_guard(connection, scope)
                # Same lock as approvals/registration and idle shutdown. Even
                # planned jobs preserve a frozen configuration identity that
                # must not race a first cold approval with different delivery.
                # Legacy/mock ledgers may have no physical capacity gate.
                self._locked(
                    connection,
                    select(capacity_gate).where(capacity_gate.c.id == "global"),
                )
                plan = self._locked(
                    connection,
                    select(plans).where(
                        plans.c.id == plan_id, self._scope(plans, scope)
                    ),
                )
                if plan is None:
                    raise NotFound("plan_not_found")
                self._content_write_guard(connection, scope, plan["request"])
                # Creation semantics include the immutable chosen plan, not just the prompt.
                digest = request_hash(
                    {
                        "request_hash": plan["request_hash"],
                        "plan_hash": plan["plan_hash"],
                        "estimated_cost_microusd": plan["estimated_cost_microusd"],
                    }
                )
                existing = (
                    connection.execute(select(jobs).where(namespace)).mappings().first()
                )
                if existing:
                    if existing["request_hash"] != digest:
                        raise Conflict("idempotency_conflict")
                    return {**dict(existing), "created": False}
                self._check_generation_policy(
                    connection,
                    scope,
                    plan,
                    admitting=initial_status in {"queued", "waiting_capacity"},
                    idempotency_key=idempotency_key,
                )
                from .storyboard_versions import check_admission_source

                check_admission_source(connection, self, scope, plan["request"])
                if plan["expires_at"] <= self.clock():
                    raise Conflict("plan_expired")
                execution = plan["execution_plan"]
                if (
                    execution.get("admission_state") == "waiting_capacity"
                    and initial_status == "queued"
                ):
                    raise Conflict("capacity_plan_requires_waiting")
                pool = identifier(execution.get("pool", "h3-base-ref"))
                if initial_status == "queued":
                    self._require_pool_not_stopping(
                        connection, pool, execution, plan["request"], scope=scope
                    )
                runtime = float(execution.get("expected_runtime_s", 600))
                if not 0 < runtime <= 86400:
                    raise ValueError("invalid_expected_runtime")
                row = dict(
                    id=str(uuid.uuid4()),
                    tenant_id=scope.tenant_id,
                    owner_id=scope.owner_id,
                    project_id=scope.project_id,
                    actor_id=scope.actor_id,
                    idempotency_key=idempotency_key,
                    request_hash=digest,
                    plan_id=plan_id,
                    request=plan["request"],
                    execution_plan=execution,
                    status=initial_status,
                    pool=pool,
                    expected_runtime_s=runtime,
                    estimated_cost_microusd=plan["estimated_cost_microusd"],
                    created_at=self.clock(),
                    updated_at=self.clock(),
                    not_before=self.clock(),
                    attempt_no=0,
                    fence=0,
                )
                if initial_status in ("queued", "waiting_capacity"):
                    from .supplier_spending import admit_job

                    snapshot = admit_job(
                        connection,
                        scope,
                        execution,
                        self.clock(),
                        estimated_cost_microusd=row["estimated_cost_microusd"],
                        job_id=row["id"],
                    )
                    if snapshot is not None:
                        execution = {**execution, "capacity_policy_snapshot": snapshot}
                        row["execution_plan"] = execution
                connection.execute(insert(jobs).values(**row))
                if initial_status in ("queued", "waiting_capacity"):
                    role_map = self._reserve(
                        connection,
                        scope,
                        budget_account_ids,
                        "job",
                        row["id"],
                        row["estimated_cost_microusd"],
                        spending_actor=spending_actor,
                        confirmation_required=confirmation_required,
                        spending_settings_version=spending_settings_version,
                    )
                    from .credits import hold as hold_credits

                    held = hold_credits(connection, self, scope, row, spending_actor)
                    from .credit_admission import require_hold

                    require_hold(connection, scope, row, held, role_map)
                if initial_status == "waiting_capacity":
                    from .capacity import admit_waiter

                    admit_waiter(self, connection, scope, row, plan)
                if execution.get("backend") == "wangp-worker":
                    from .control import require_delivery_configuration

                    self._lock_capacity(connection)
                    require_delivery_configuration(
                        connection,
                        "wangp-worker",
                        execution.get("configuration_id"),
                        execution.get("output_delivery", ""),
                    )
                self._emit(
                    connection,
                    "job.created",
                    row["id"],
                    {"job_id": row["id"], "status": initial_status},
                )
                return {**self._job(connection, row["id"]), "created": True}
        except IntegrityError:
            # Another process may have committed the same request while this transaction waited.
            with self.engine.connect() as connection:
                existing = (
                    connection.execute(select(jobs).where(namespace)).mappings().first()
                )
            if existing and existing["request_hash"] == digest:
                return {**dict(existing), "created": False}
            raise Conflict("idempotency_conflict") from None

    def enqueue(
        self,
        scope,
        job_id,
        *,
        budget_account_ids=(),
        spending_actor=None,
        confirmation_required=False,
        spending_settings_version=None,
    ):
        with self.transaction() as connection:
            from .identity import account_status, lock_account_gate

            lock_account_gate(connection)
            state = account_status(connection, scope.tenant_id, scope.owner_id)
            if state != "active":
                raise Conflict(
                    "account_suspended"
                    if state == "suspended"
                    else "account_unavailable"
                )
            self._content_write_guard(connection, scope)
            self._locked(
                connection, select(capacity_gate).where(capacity_gate.c.id == "global")
            )
            job = self._job(connection, job_id, scope, lock=True)
            if job["status"] not in ("planned", "blocked") or job["attempt_no"]:
                raise InvalidTransition("job_not_admissible")
            plan = (
                connection.execute(select(plans).where(plans.c.id == job["plan_id"]))
                .mappings()
                .one()
            )
            self._content_write_guard(connection, scope, plan["request"])
            self._check_generation_policy(
                connection, scope, plan, idempotency_key=job["idempotency_key"]
            )
            from .storyboard_versions import check_admission_source

            check_admission_source(connection, self, scope, plan["request"])
            if plan["expires_at"] <= self.clock():
                raise Conflict("plan_expired")
            execution = plan["execution_plan"]
            if execution.get("backend") == "wangp-worker":
                from .control import require_delivery_configuration

                self._lock_capacity(connection)
                require_delivery_configuration(
                    connection,
                    "wangp-worker",
                    execution.get("configuration_id"),
                    execution.get("output_delivery", ""),
                )
            self._require_pool_not_stopping(
                connection,
                job["pool"],
                plan["execution_plan"],
                plan["request"],
                scope=scope,
            )
            snapshot = job["execution_plan"].get("capacity_policy_snapshot")
            if snapshot is None:
                from .supplier_spending import admit_job

                snapshot = admit_job(
                    connection,
                    scope,
                    execution,
                    self.clock(),
                    estimated_cost_microusd=job["estimated_cost_microusd"],
                    job_id=job_id,
                )
            else:
                # A previously admitted job keeps its original policy. Enqueue
                # is not a new purchase or authority to reinterpret that hold.
                from .capacity_settings import validate_snapshot

                validate_snapshot(snapshot)
            if snapshot is not None:
                job = {
                    **job,
                    "execution_plan": {
                        **job["execution_plan"],
                        "capacity_policy_snapshot": snapshot,
                    },
                }
            role_map = self._reserve(
                connection,
                scope,
                budget_account_ids,
                "job",
                job_id,
                job["estimated_cost_microusd"],
                spending_actor=spending_actor,
                confirmation_required=confirmation_required,
                spending_settings_version=spending_settings_version,
            )
            from .credits import hold as hold_credits

            held = hold_credits(connection, self, scope, job, spending_actor)
            from .credit_admission import require_hold

            require_hold(connection, scope, job, held, role_map)
            status = (
                "waiting_capacity"
                if plan["execution_plan"].get("admission_state") == "waiting_capacity"
                else "queued"
            )
            if status == "waiting_capacity":
                from .capacity import admit_waiter

                admit_waiter(self, connection, scope, job, plan)
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    status=status,
                    updated_at=self.clock(),
                    execution_plan=job["execution_plan"],
                )
            )
            self._emit(connection, "job." + status, job_id, {"job_id": job_id})
            return self._job(connection, job_id)

    def refresh_unadmitted_plan(self, scope, job_id, plan_id):
        """Replace an expired plan only for an inert Quick Chat execution.

        No budget reservation, attempt, worker lease, or upstream obligation may
        exist. The original job ID/key remains; this does not perform admission.
        """
        with self.transaction() as connection:
            self._content_write_guard(connection, scope)
            # Match create/enqueue lock order before changing a frozen plan binding.
            self._locked(
                connection, select(capacity_gate).where(capacity_gate.c.id == "global")
            )
            job = self._job(connection, job_id, scope, lock=True)
            if (
                job["actor_id"] != "quick-chat-execution"
                or job["status"] not in {"planned", "blocked"}
                or job["attempt_no"]
                or job["current_attempt_id"]
                or job["lease_worker_id"]
                or job["lease_expires_at"]
            ):
                raise Conflict("upstream_stop_unconfirmed")
            # Summary counters can disagree with recovered historical rows.
            # An old attempt is still an execution obligation until reconciled;
            # never refresh its request merely because the current lease is empty.
            if connection.execute(
                select(attempts.c.id).where(attempts.c.job_id == job_id).limit(1)
            ).first():
                raise Conflict("upstream_stop_unconfirmed")
            if connection.execute(
                select(budget_reservations.c.id).where(
                    budget_reservations.c.reference_type == "job",
                    budget_reservations.c.reference_id == job_id,
                )
            ).first():
                raise Conflict("job_reservation_requires_reconciliation")
            plan = self._locked(
                connection,
                select(plans).where(plans.c.id == plan_id, self._scope(plans, scope)),
            )
            if plan is None:
                raise NotFound("plan_not_found")
            if plan["expires_at"] <= self.clock():
                raise Conflict("plan_expired")
            old, new = job["request"], plan["request"]
            self._content_write_guard(connection, scope, new)
            if (
                old.get("server_source_hash") != new.get("server_source_hash")
                or old.get("client_ref") != new.get("client_ref")
                or old.get("recipe_id") != new.get("recipe_id")
            ):
                raise Conflict("shot_version_conflict")
            if plan_id == job["plan_id"]:
                return job
            execution = plan["execution_plan"]
            if execution.get("backend") == "wangp-worker":
                from .control import require_delivery_configuration

                require_delivery_configuration(
                    connection,
                    "wangp-worker",
                    execution.get("configuration_id"),
                    execution.get("output_delivery", ""),
                )
            runtime = float(execution.get("expected_runtime_s", 600))
            if not 0 < runtime <= 86400:
                raise ValueError("invalid_expected_runtime")
            digest = request_hash(
                {
                    "request_hash": plan["request_hash"],
                    "plan_hash": plan["plan_hash"],
                    "estimated_cost_microusd": plan["estimated_cost_microusd"],
                }
            )
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    plan_id=plan_id,
                    request=new,
                    execution_plan=execution,
                    request_hash=digest,
                    estimated_cost_microusd=plan["estimated_cost_microusd"],
                    status="planned",
                    expected_runtime_s=runtime,
                    pool=identifier(execution.get("pool", "h3-base-ref")),
                    updated_at=self.clock(),
                    error_code=None,
                )
            )
            self._emit(
                connection,
                "job.plan_refreshed",
                job_id,
                {
                    "job_id": job_id,
                    "plan_id": plan_id,
                    "previous_plan_id": job["plan_id"],
                },
            )
            return self._job(connection, job_id)

    def get_job(self, scope, job_id):
        with self.engine.connect() as connection:
            return self._job(connection, job_id, scope)

    def lookup_job_by_idempotency(self, scope, idempotency_key):
        """Owned retry lookup before mutable shot/asset prevalidation; no creation."""
        identifier(idempotency_key)
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(jobs).where(
                        self._scope(jobs, scope),
                        jobs.c.actor_id == scope.actor_id,
                        jobs.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            return None if row is None else dict(row)

    def settle_completed_job(
        self, scope, job_id, *, actual_cost_microusd, connection=None
    ):
        """Trusted billing reconciliation; terminal output availability is independent.

        None is never interpreted as free. Pending charges retain their original
        amount for Settings projections even after the terminal hold is released. Repeated identical settlement is harmless; conflicting totals
        require explicit accounting correction rather than a silent overwrite.
        """
        cost = money(actual_cost_microusd)
        with (
            self.transaction()
            if connection is None
            else nullcontext(connection) as connection
        ):
            job = self._job(connection, job_id, scope, lock=True)
            if job["status"] not in ("succeeded", "failed", "cancelled"):
                raise InvalidTransition("job_not_terminal")
            result = dict(job["result"] or {})
            if result.get("billing_status") == "settled":
                if result.get("actual_cost_microusd") != cost:
                    raise Conflict("budget_already_settled")
                return job
            self._settle(connection, "job", job_id, cost)
            result.update(billing_status="settled", actual_cost_microusd=cost)
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(result=result, updated_at=self.clock())
            )
            if job["current_attempt_id"]:
                connection.execute(
                    update(attempts)
                    .where(attempts.c.id == job["current_attempt_id"])
                    .values(actual_cost_microusd=cost, updated_at=self.clock())
                )
            self._emit(connection, "job.billing_settled", job_id, {"job_id": job_id})
            return self._job(connection, job_id)

    def get_job_for_owner(self, tenant_id, owner_id, job_id):
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(jobs).where(
                        jobs.c.id == job_id,
                        jobs.c.tenant_id == tenant_id,
                        jobs.c.owner_id == owner_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound("job_not_found")
            return dict(row)

    def list_jobs(self, scope, *, limit=100, offset=0, summary=False):
        return self.list_jobs_for_owner(
            scope.tenant_id,
            scope.owner_id,
            project_id=scope.project_id,
            limit=limit,
            offset=offset,
            summary=summary,
        )

    def list_jobs_for_owner(
        self,
        tenant_id,
        owner_id,
        *,
        project_id=None,
        project_ids=None,
        limit=100,
        offset=0,
        summary=False,
    ):
        _pagination(limit, offset)
        if type(summary) is not bool:
            raise ValueError("invalid_job_summary_option")
        permitted_projects = _allowed_ids(project_ids)
        if permitted_projects == ():
            return []
        statement = select(*self._job_summary_columns()) if summary else select(jobs)
        statement = statement.where(
            jobs.c.tenant_id == tenant_id, jobs.c.owner_id == owner_id
        )
        if project_id is not None:
            statement = statement.where(jobs.c.project_id == project_id)
        if permitted_projects is not None:
            statement = statement.where(jobs.c.project_id.in_(permitted_projects))
        with self.engine.connect() as connection:
            values = [
                dict(r)
                for r in connection.execute(
                    statement.order_by(jobs.c.created_at.desc(), jobs.c.id)
                    .limit(limit)
                    .offset(offset)
                ).mappings()
            ]
        if summary:
            self._decode_job_summaries(values)
        return values

    def _job_summary_columns(self):
        # Preserve the public job DTO without decoding hidden assets/source
        # snapshots. effective_request remains intact by API contract.
        columns = [
            column
            for column in jobs.c
            if column.name not in ("request", "execution_plan")
        ]
        for field in ("client_ref", "recipe_id", "request", "simulation"):
            columns.append(jobs.c.request[field].label("_summary_" + field))
            present = (
                func.json_type(jobs.c.request, "$." + field)
                if self.engine.dialect.name == "sqlite"
                else func.json_typeof(jobs.c.request[field])
            )
            columns.append(present.label("_type_" + field))
        columns.append(jobs.c.execution_plan["backend"].label("_summary_backend"))
        columns.append(
            jobs.c.execution_plan["delivery_spec"].label("_summary_delivery_spec")
        )
        present = (
            func.json_type(jobs.c.execution_plan, "$.delivery_spec")
            if self.engine.dialect.name == "sqlite"
            else func.json_typeof(jobs.c.execution_plan["delivery_spec"])
        )
        columns.append(present.label("_type_delivery_spec"))
        return columns

    def _decode_job_summaries(self, values):
        for row in values:
            row["request"] = {}
            for field in ("client_ref", "recipe_id", "request", "simulation"):
                value = row.pop("_summary_" + field)
                kind = row.pop("_type_" + field)
                if kind is not None:
                    if self.engine.dialect.name == "sqlite" and kind in (
                        "true",
                        "false",
                    ):
                        value = kind == "true"
                    row["request"][field] = value
            row["execution_plan"] = {"backend": row.pop("_summary_backend")}
            delivery = row.pop("_summary_delivery_spec")
            if row.pop("_type_delivery_spec") is not None:
                row["execution_plan"]["delivery_spec"] = delivery

    @staticmethod
    def _job_batch_predicate(tenant_id, owner_id, job_projects, *, maximum):
        identifier(tenant_id), identifier(owner_id)
        if not isinstance(job_projects, dict) or len(job_projects) > maximum:
            raise ValueError("invalid_owned_job_batch")
        groups = {}
        for job_id, project_id in job_projects.items():
            identifier(job_id), identifier(project_id)
            groups.setdefault(project_id, []).append(job_id)
        # Group by project so 1000 IDs from ten small batch summaries do not
        # exceed SQLite expression-depth limits with a 1000-branch OR tree.
        return (
            and_(
                jobs.c.tenant_id == tenant_id,
                jobs.c.owner_id == owner_id,
                or_(
                    *(
                        and_(jobs.c.project_id == project_id, jobs.c.id.in_(ids))
                        for project_id, ids in groups.items()
                    )
                ),
            )
            if groups
            else None
        )

    def get_job_statuses_for_owner(self, tenant_id, owner_id, job_projects):
        """At most 1000 owned job/project pairs; scalar status only, no snapshots.

        The caller separately authorizes each project's operation. An empty
        mapping is an empty result, and every requested exact association must
        exist; foreign IDs never become a partial, leaking response.
        """
        permitted = self._job_batch_predicate(
            tenant_id, owner_id, job_projects, maximum=1000
        )
        if permitted is None:
            return {}
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(jobs.c.id, jobs.c.status).where(permitted)
            ).all()
        result = {row.id: row.status for row in rows}
        if set(result) != set(job_projects):
            raise NotFound("job_not_found")
        return result

    def get_job_summaries_for_owner(self, tenant_id, owner_id, job_projects):
        """At most 100 exact owned pairs; public DTO projection in one query."""
        permitted = self._job_batch_predicate(
            tenant_id, owner_id, job_projects, maximum=100
        )
        if permitted is None:
            return {}
        with self.engine.connect() as connection:
            values = [
                dict(row)
                for row in connection.execute(
                    select(*self._job_summary_columns()).where(permitted)
                ).mappings()
            ]
        if {row["id"] for row in values} != set(job_projects):
            raise NotFound("job_not_found")
        self._decode_job_summaries(values)
        return {row["id"]: row for row in values}

    def list_artifacts(self, scope, job_id):
        with self.engine.connect() as connection:
            if (
                connection.execute(
                    select(jobs.c.id).where(
                        jobs.c.id == job_id, self._scope(jobs, scope)
                    )
                ).first()
                is None
            ):
                raise NotFound("job_not_found")
            return [
                dict(r)
                for r in connection.execute(
                    select(artifacts)
                    .where(artifacts.c.job_id == job_id)
                    .order_by(artifacts.c.created_at, artifacts.c.id)
                ).mappings()
            ]

    def list_artifacts_for_jobs(self, tenant_id, owner_id, job_projects):
        """At most 100 explicitly authorized owned job/project pairs, scalar check.

        Caller must independently authorize project operations. This method also
        enforces exact tenant/owner/project associations; a supplied guessed ID
        cannot smuggle another account's artifacts into a list response.
        """
        identifier(tenant_id), identifier(owner_id)
        if not isinstance(job_projects, dict) or len(job_projects) > 100:
            raise ValueError("invalid_artifact_job_batch")
        if not job_projects:
            return {}
        for job_id, project_id in job_projects.items():
            identifier(job_id), identifier(project_id)
        permitted = and_(
            jobs.c.tenant_id == tenant_id,
            jobs.c.owner_id == owner_id,
            or_(
                *(
                    and_(jobs.c.id == job_id, jobs.c.project_id == project_id)
                    for job_id, project_id in job_projects.items()
                )
            ),
        )
        result = {job_id: [] for job_id in job_projects}
        with self.engine.connect() as connection:
            present = {
                row.id for row in connection.execute(select(jobs.c.id).where(permitted))
            }
            if present != set(job_projects):
                raise NotFound("job_not_found")
            for row in connection.execute(
                select(artifacts)
                .join(jobs, artifacts.c.job_id == jobs.c.id)
                .where(permitted)
                .order_by(artifacts.c.created_at, artifacts.c.id)
            ).mappings():
                result[row["job_id"]].append(dict(row))
        return result

    def request_cancel(self, scope, job_id, *, cancellation=None):
        from .job_cancellation import metadata_for

        with self.transaction() as connection:
            job = self._job(connection, job_id, scope, lock=True)
            value = (
                cancellation
                if cancellation is not None
                else metadata_for(
                    "owner", "owner_request_cancelled", scope.actor_id, self.clock()
                )
            )
            return self._cancel_locked(connection, job, value)

    def _cancel_locked(self, connection, job, cancellation):
        from .job_cancellation import validate_metadata

        job_id = job["id"]
        value = validate_metadata(cancellation)
        if (
            job["status"]
            not in {"succeeded", "failed", "cancelled", "cancel_requested"}
            and job.get("cancellation") is None
        ):
            connection.execute(
                update(jobs).where(jobs.c.id == job_id).values(cancellation=value)
            )
            job = dict(job, cancellation=value)
        if (
            job["status"] == "failed"
            and job["error_code"] == "capacity_approval_expired_or_revoked"
            and job["attempt_no"] == 0
            and job["current_attempt_id"] is None
        ):
            # A later privileged preparation recovery must honour a user
            # cancelling this failed, never-submitted request. Historical
            # versions returned without leaving evidence in this window.
            result = dict(job["result"] or {})
            if result.get("recovery_cancel_requested") is not True:
                result["recovery_cancel_requested"] = True
                connection.execute(
                    update(jobs)
                    .where(jobs.c.id == job_id)
                    .values(result=result, updated_at=self.clock())
                )
                self._emit(
                    connection,
                    "job.cancel_requested",
                    job_id,
                    {"job_id": job_id, "status": "failed", "upstream_stopped": True},
                )
                return self._job(connection, job_id)
        if job["status"] in ("succeeded", "failed", "cancelled", "cancel_requested"):
            return job
        if job["status"] == "recovery_hold":
            # Restored attempts may still be running/billable upstream.
            # A browser cancellation must not escape quarantine into the
            # ordinary reconcile queue or pretend zero-cost cancellation.
            result = dict(job["result"] or {})
            if result.get("recovery_cancel_requested") is True:
                return job
            result["recovery_cancel_requested"] = True
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(result=result, updated_at=self.clock())
            )
            self._emit(
                connection,
                "job.cancel_requested",
                job_id,
                {
                    "job_id": job_id,
                    "status": "recovery_hold",
                    "upstream_stopped": False,
                },
            )
            return self._job(connection, job_id)
        from .capacity_reclaim import cancel_before_replacement

        reclaimed_cancel = cancel_before_replacement(self, connection, job)
        if reclaimed_cancel is not None:
            return reclaimed_cancel
        immediate = job["status"] in (
            "planned",
            "blocked",
            "queued",
            "claimed",
            "waiting_capacity",
        )
        if (
            immediate
            and job["status"] != "waiting_capacity"
            and (job["current_attempt_id"] or job["attempt_no"])
        ):
            previous = connection.execute(
                select(
                    attempts.c.submission_started_at, attempts.c.upstream_task_id
                ).where(
                    attempts.c.id == job["current_attempt_id"],
                    attempts.c.job_id == job_id,
                )
            ).first()
            if (
                previous is None
                or previous.submission_started_at is not None
                or previous.upstream_task_id is not None
            ):
                # A malformed/manual status change must not turn a paid or
                # uncertain attempt into a free cancellation. Keep the
                # attempt/invoice evidence and require reconciliation, as
                # generate-claim does for the same unsafe requeue window.
                result = dict(job["result"] or {})
                result["recovery_cancel_requested"] = True
                connection.execute(
                    update(jobs)
                    .where(jobs.c.id == job_id)
                    .values(
                        status="recovery_hold",
                        error_code="cancellation_requires_reconciliation",
                        cancel_from_status=job["status"],
                        result=result,
                        fence=job["fence"] + 1,
                        lease_worker_id=None,
                        lease_expires_at=None,
                        updated_at=self.clock(),
                    )
                )
                self._emit(
                    connection,
                    "job.cancel_requested",
                    job_id,
                    {
                        "job_id": job_id,
                        "status": "recovery_hold",
                        "upstream_stopped": False,
                    },
                )
                return self._job(connection, job_id)
        new_status = "cancelled" if immediate else "cancel_requested"
        values = dict(
            status=new_status, cancel_from_status=job["status"], updated_at=self.clock()
        )
        if immediate:
            if job["status"] == "waiting_capacity":
                if job["attempt_no"] or job["current_attempt_id"]:
                    raise Conflict("capacity_waiter_has_attempt_requires_review")
            values.update(
                fence=job["fence"] + 1, lease_worker_id=None, lease_expires_at=None
            )
            self._settle(connection, "job", job_id, 0)
            if job["status"] == "waiting_capacity":
                # The scaler links waiters after locking shared budgets.
                # Use budget-before-waiter here too, even when task and
                # instance happen to use the same account.
                connection.execute(
                    update(capacity_waiters)
                    .where(capacity_waiters.c.job_id == job_id)
                    .values(state="cancelled")
                )
            if job["current_attempt_id"]:
                connection.execute(
                    update(attempts)
                    .where(attempts.c.id == job["current_attempt_id"])
                    .values(
                        status="cancelled",
                        updated_at=self.clock(),
                        actual_cost_microusd=0,
                    )
                )
        connection.execute(update(jobs).where(jobs.c.id == job_id).values(**values))
        if immediate:
            from .credits import release as release_credits

            release_credits(connection, self, job, reason="cancelled_before_submission")
        self._emit(
            connection,
            "job.cancel_requested",
            job_id,
            {"job_id": job_id, "status": new_status, "upstream_stopped": immediate},
        )
        return self._job(connection, job_id)

    def cancel_queued_jobs(
        self, connection, *, tenant_id, selections, actor_id, reason_code
    ):
        """Trusted human-admin caller owns authorization, capacity lock and replay.

        Lock and validate the entire selection before cancelling any row. A
        worker winning a claim or an old submitted attempt refuses the batch.
        """
        from .job_cancellation import (
            OPERATOR_REASONS,
            cancel_eligibility,
            cancel_snapshot,
            metadata_for,
        )

        identifier(tenant_id), identifier(actor_id)
        if (
            not isinstance(selections, list)
            or not 1 <= len(selections) <= 100
            or not isinstance(reason_code, str)
            or reason_code not in OPERATOR_REASONS
        ):
            raise ValueError("invalid_queued_job_cancellation")
        chosen = {}
        for value in selections:
            if not isinstance(value, dict) or set(value) != {"id", "expected_snapshot"}:
                raise ValueError("invalid_queued_job_cancellation")
            job_id = identifier(value["id"])
            digest = value["expected_snapshot"]
            if (
                job_id in chosen
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise ValueError("invalid_queued_job_cancellation")
            chosen[job_id] = digest
        locked = []
        for job_id in sorted(chosen):
            row = self._locked(
                connection,
                select(jobs).where(jobs.c.id == job_id, jobs.c.tenant_id == tenant_id),
            )
            if row is None:
                raise NotFound("job_not_found")
            if cancel_snapshot(row) != chosen[job_id]:
                raise Conflict("job_cancel_snapshot_changed")
            if not cancel_eligibility(connection, row)["cancellable"]:
                raise Conflict("job_not_safely_cancellable")
            locked.append(dict(row))
        value = metadata_for("operator", reason_code, actor_id, self.clock())
        # Jobs are already locked. Pre-lock the union in one global order,
        # rather than obtaining each job's overlapping accounts piecemeal.
        # Ordinary settlement locks its job before reservations/accounts too.
        accounts = connection.execute(
            select(budget_reservations.c.account_id)
            .where(
                budget_reservations.c.reference_type == "job",
                budget_reservations.c.reference_id.in_(chosen),
                budget_reservations.c.state == "reserved",
            )
            .distinct()
            .order_by(budget_reservations.c.account_id)
        ).scalars()
        for account_id in list(accounts):
            if (
                self._locked(
                    connection,
                    select(budget_accounts).where(budget_accounts.c.id == account_id),
                )
                is None
            ):
                raise NotFound("budget_not_found")
        return [self._cancel_locked(connection, row, value) for row in locked]

    def get_job_cancellation_fields(
        self, tenant_id, owner_id, job_projects, *, expected_states=None
    ):
        """At most 100 owned pairs; one consistent, bounded status/proof snapshot."""
        from .job_cancellation import cancellation_columns, cancellation_fields

        permitted = self._job_batch_predicate(
            tenant_id, owner_id, job_projects, maximum=100
        )
        if permitted is None:
            return {}
        if expected_states is not None and (
            not isinstance(expected_states, dict)
            or set(expected_states) != set(job_projects)
            or any(
                not isinstance(value, dict)
                or set(value) != {"status", "updated_at", "error_code"}
                for value in expected_states.values()
            )
        ):
            raise ValueError("invalid_job_cancellation_snapshot")
        with self._budget_read() as connection:
            rows = list(
                connection.execute(
                    select(*cancellation_columns()).where(permitted)
                ).mappings()
            )
            if {row["id"] for row in rows} != set(job_projects):
                raise NotFound("job_not_found")
            history = {row["id"]: [] for row in rows}
            for item in connection.execute(
                select(attempts)
                .join(jobs, attempts.c.job_id == jobs.c.id)
                .where(permitted)
                .order_by(attempts.c.number)
            ).mappings():
                history[item["job_id"]].append(item)
            reservations = {row["id"]: [] for row in rows}
            for item in connection.execute(
                select(budget_reservations)
                .join(jobs, budget_reservations.c.reference_id == jobs.c.id)
                .where(permitted, budget_reservations.c.reference_type == "job")
            ).mappings():
                reservations[item["reference_id"]].append(item)
            result = {}
            for row in rows:
                expected = (
                    expected_states[row["id"]] if expected_states is not None else None
                )
                if expected is not None and any(
                    row[key] != expected[key] for key in expected
                ):
                    # The original job DTO was read before this proof snapshot.
                    # Do not mix a newer reason/retry grant into its older phase.
                    result[row["id"]] = {
                        "cancelled_by": None,
                        "reason_code": None,
                        "message": None,
                        "retry_advice": {
                            "safe_to_retry": False,
                            "charged": None,
                            "upstream_still_running": None,
                        },
                    }
                else:
                    result[row["id"]] = cancellation_fields(
                        connection,
                        row,
                        history=history[row["id"]],
                        reservations=reservations[row["id"]],
                    )
            return result
