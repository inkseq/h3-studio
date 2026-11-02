"""Durable platform ledger. No provider calls, credentials, or legacy database access.

PostgreSQL URLs must use ``postgresql+psycopg://``. SQLite is a local verification
backend; its write transactions are serialized with BEGIN IMMEDIATE. Callers
authenticate principals and authorize project membership before constructing Scope.
Amounts are integer micro-US dollars; estimates are not supplier invoices.
"""

from __future__ import annotations
from contextlib import contextmanager
import re
import threading
import time
import uuid
from sqlalchemy import (
    and_,
    create_engine,
    event,
    func,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.pool import StaticPool

from .repository_schema import (
    LedgerError as LedgerError,
    NotFound as NotFound,
    Conflict as Conflict,
    BudgetExceeded as BudgetExceeded,
    LeaseLost as LeaseLost,
    InvalidTransition as InvalidTransition,
    Scope as Scope,
    canonical as canonical,
    request_hash as request_hash,
    money as money,
    identifier as identifier,
    budget_snapshot as budget_snapshot,
    metadata as metadata,
    scope_columns as scope_columns,
    documents as documents,
    plans as plans,
    budget_accounts as budget_accounts,
    jobs as jobs,
    attempts as attempts,
    budget_reservations as budget_reservations,
    spending_settings as spending_settings,
    spending_origins as spending_origins,
    spending_confirmations as spending_confirmations,
    billing_adjustments as billing_adjustments,
    artifacts as artifacts,
    outbox as outbox,
    job_event_order as job_event_order,
    JOB_EVENT_LOCK as JOB_EVENT_LOCK,
    dispatch_receipts as dispatch_receipts,
    scheduler_state as scheduler_state,
    owner_usage as owner_usage,
    pool_limits as pool_limits,
    instance_intents as instance_intents,
    capacity_gate as capacity_gate,
    registered_workers as registered_workers,
    registered_devices as registered_devices,
    cpu_slots as cpu_slots,
    scaler_leaders as scaler_leaders,
    scaler_observations as scaler_observations,
    scaler_actions as scaler_actions,
    scaler_receipts as scaler_receipts,
    capacity_approvals as capacity_approvals,
    capacity_cycles as capacity_cycles,
    capacity_waiters as capacity_waiters,
    capacity_pool_members as capacity_pool_members,
    capacity_member_generations as capacity_member_generations,
    _NullLock as _NullLock,
    _allowed_ids as _allowed_ids,
    _pagination as _pagination,
)
from .repository_capacity import (
    CapacityLedger,
    manually_reviewed_inactive as manually_reviewed_inactive,
    paused_capacity_pools as paused_capacity_pools,
)
from .repository_documents import DocumentLedger
from .repository_budgets import BudgetLedger
from .repository_jobs import JobLedger


class Repository(DocumentLedger, BudgetLedger, JobLedger, CapacityLedger):
    def __init__(self, database_url, *, clock=time.time):
        self.clock = clock
        self.generation_transport = None
        self._memory_lock = threading.RLock()
        self._is_memory = database_url in ("sqlite://", "sqlite:///:memory:")
        kwargs = {"echo": False, "hide_parameters": True, "pool_pre_ping": True}
        if database_url.startswith("sqlite:"):
            kwargs["connect_args"] = {"timeout": 30, "check_same_thread": False}
            if self._is_memory:
                kwargs["poolclass"] = StaticPool
        elif not database_url.startswith("postgresql+psycopg:"):
            raise ValueError("unsupported_database_driver")
        self.engine = create_engine(database_url, **kwargs)
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def sqlite_setup(connection, _):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()

    def create_schema(self):
        """Create platform tables and apply only explicit, additive migrations.

        Never rebuild/drop a table or infer that legacy unknown capacity is free.
        New columns require an explicit migration here, not just create_all.
        """
        with self.transaction() as connection:
            if self.engine.dialect.name == "postgresql":
                # Serialize startup DDL between API/controller/worker processes.
                # This is a fixed application lock, independent of secrets/schema.
                connection.exec_driver_sql(
                    "SELECT pg_advisory_xact_lock(685939796868749721)"
                )
            metadata.create_all(connection)
            # One startup lock also covers the independently owned storage
            # metadata. Do not call engine-based helpers here: that would start
            # a nested SQLite write transaction / lose the PG advisory lock.
            from .assets import metadata as asset_metadata
            from .storage_asset_journal import _schema as asset_journal_metadata
            from .storage_multipart import _metadata as multipart_metadata
            from .artifact_writer import _schema as write_metadata

            for schema in (
                asset_metadata,
                asset_journal_metadata,
                multipart_metadata,
                write_metadata,
            ):
                schema.create_all(connection)
            intent_columns = {
                column["name"]: column
                for column in inspect(connection).get_columns(
                    "platform_instance_intents"
                )
            }
            known_columns = set(intent_columns)
            if "provider" not in known_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE platform_instance_intents ADD COLUMN provider VARCHAR(200) NOT NULL DEFAULT 'unknown'"
                )
            if (
                self.engine.dialect.name == "postgresql"
                and not intent_columns["hard_deadline"]["nullable"]
            ):
                # New continuing dstack runs have no absolute lifetime. This
                # relaxes only the column constraint; historical values remain.
                connection.exec_driver_sql(
                    "ALTER TABLE platform_instance_intents ALTER COLUMN hard_deadline DROP NOT NULL"
                )
            worker_columns = {
                column["name"]
                for column in inspect(connection).get_columns(
                    "platform_registered_workers"
                )
            }
            if "drain_requested" not in worker_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE platform_registered_workers ADD COLUMN drain_requested INTEGER NOT NULL DEFAULT 0"
                )
                connection.exec_driver_sql(
                    "UPDATE platform_registered_workers SET drain_requested=1 WHERE state='draining'"
                )
            scaler_columns = {
                column["name"]
                for column in inspect(connection).get_columns("platform_scaler_actions")
            }
            if "application_idle_since" not in scaler_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE platform_scaler_actions ADD COLUMN application_idle_since FLOAT"
                )
            reservation_columns = {
                column["name"]
                for column in inspect(connection).get_columns(
                    "platform_budget_reservations"
                )
            }
            if "settled_at" not in reservation_columns:
                # Old confirmed charges have no proven settlement date. Keep
                # them NULL rather than backfilling from reservation creation.
                connection.exec_driver_sql(
                    "ALTER TABLE platform_budget_reservations ADD COLUMN settled_at FLOAT"
                )
            job_columns = {
                column["name"]
                for column in inspect(connection).get_columns("platform_jobs")
            }
            if "cancellation" not in job_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE platform_jobs ADD COLUMN cancellation JSON"
                )
            event_columns = {
                column["name"]
                for column in inspect(connection).get_columns("platform_outbox")
            }
            if "job_event_seq" not in event_columns:
                # Historical timestamps do not establish commit order. The
                # new feed starts at this additive activation epoch.
                connection.exec_driver_sql(
                    "ALTER TABLE platform_outbox ADD COLUMN job_event_seq BIGINT"
                )
            job_event_order.create(connection, checkfirst=True)

    def close(self):
        self.engine.dispose()

    @contextmanager
    def transaction(self):
        guard = self._memory_lock if self._is_memory else _NullLock()
        with guard, self.engine.connect() as connection:
            connection.info["job_event_ids"] = []
            connection.info.pop("sixnine_demand_notified", None)
            try:
                if self.engine.dialect.name == "sqlite":
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    connection = connection.execution_options(
                        isolation_level="READ COMMITTED"
                    )
                    connection.begin()
                yield connection
                self._sequence_job_events(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.info.pop("job_event_ids", None)
                connection.info.pop("sixnine_demand_notified", None)

    def _sequence_job_events(self, connection):
        ids = connection.info.get("job_event_ids", ())
        if not ids:
            return
        # Acquire this last, after ALL business work. No job/account/worker
        # locks or provider calls follow it: commit is the next outer action.
        # Holding the lock through commit orders visibility, unlike BIGSERIAL
        # allocation at insert time. SQLite BEGIN IMMEDIATE already serializes.
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql(
                "SELECT pg_advisory_xact_lock(%s)", (JOB_EVENT_LOCK,)
            )
        sequence = (
            connection.execute(select(func.max(outbox.c.job_event_seq))).scalar() or 0
        )
        if sequence + len(ids) > 9_000_000_000_000_000_000:
            raise Conflict("job_event_sequence_exhausted")
        for offset, event_id in enumerate(ids, 1):
            record = (
                connection.execute(
                    select(outbox.c.event_type, outbox.c.payload).where(
                        outbox.c.id == event_id
                    )
                )
                .mappings()
                .one()
            )
            changes = {"job_event_seq": sequence + offset}
            if record["event_type"] == "job.failed":
                payload = record["payload"]
                state = payload.get("public_state", {})
                diagnostic = state.get("failure_diagnostic", {})
                notice_id = diagnostic.get("notice_id")
                if (
                    isinstance(notice_id, str)
                    and str(uuid.UUID(notice_id)) == notice_id
                ):
                    # Indexed lookup under the SAME commit-order lock. Only an
                    # unpublished first event gets the stable owner/kind ID.
                    # Every later failure remains an independent durable event.
                    prior = connection.execute(
                        select(outbox.c.id).where(outbox.c.id == notice_id)
                    ).first()
                    state = {
                        **state,
                        "failure_notice": {"id": notice_id, "new_kind": prior is None},
                    }
                    changes["payload"] = canonical({**payload, "public_state": state})
                    if prior is None:
                        changes["id"] = notice_id
            result = connection.execute(
                update(outbox)
                .where(outbox.c.id == event_id, outbox.c.job_event_seq.is_(None))
                .values(**changes)
            )
            if result.rowcount != 1:
                raise Conflict("job_event_sequence_unconfirmed")

    @staticmethod
    def _scope(table, scope):
        return and_(
            table.c.tenant_id == scope.tenant_id,
            table.c.owner_id == scope.owner_id,
            table.c.project_id == scope.project_id,
        )

    def _locked(self, connection, statement):
        return connection.execute(statement.with_for_update()).mappings().first()

    @staticmethod
    def _content_write_guard(connection, scope, value=None, *, project_id=None):
        from .content_lifecycle import (
            owner_reference_gate,
            assert_project_writable,
            assert_reference_payload,
        )

        owner_reference_gate(connection, scope.tenant_id, scope.owner_id)
        assert_project_writable(
            connection,
            scope.tenant_id,
            scope.owner_id,
            scope.project_id if project_id is None else project_id,
        )
        if value is not None:
            assert_reference_payload(connection, scope.tenant_id, scope.owner_id, value)

    def _job(self, connection, job_id, scope=None, *, lock=False):
        statement = select(jobs).where(jobs.c.id == job_id)
        if scope is not None:
            statement = statement.where(self._scope(jobs, scope))
        row = (
            self._locked(connection, statement)
            if lock
            else connection.execute(statement).mappings().first()
        )
        if row is None:
            raise NotFound("job_not_found")
        return dict(row)

    def _emit(self, connection, event_type, aggregate_id, payload):
        event_id = str(uuid.uuid4())
        public_event = (
            event_type.startswith("job.") and event_type != "job.dispatch_requested"
        )
        if public_event:
            if "job_event_ids" not in connection.info:
                raise Conflict("job_event_transaction_required")
            from .job_cancellation import cancellation_fields, failure_metadata

            columns = [
                jobs.c.id,
                jobs.c.status,
                jobs.c.error_code,
                jobs.c.cancellation,
                jobs.c.attempt_no,
                jobs.c.current_attempt_id,
                jobs.c.fence,
                jobs.c.lease_worker_id,
                jobs.c.lease_expires_at,
                jobs.c.result["actual_cost_microusd"].label("_actual_cost_microusd"),
            ]
            if event_type == "job.failed":
                columns += [
                    jobs.c.tenant_id,
                    jobs.c.owner_id,
                    jobs.c.request,
                    jobs.c.execution_plan,
                    jobs.c.result,
                ]
            state = (
                connection.execute(select(*columns).where(jobs.c.id == aggregate_id))
                .mappings()
                .one()
            )
            state = dict(state)
            if state["status"] == "failed" and state["cancellation"] is None:
                state["cancellation"] = failure_metadata(
                    state["error_code"], self.clock()
                )
                connection.execute(
                    update(jobs)
                    .where(jobs.c.id == aggregate_id)
                    .values(cancellation=state["cancellation"])
                )
            error = state["error_code"]
            if error is not None and (
                not isinstance(error, str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,199}", error) is None
            ):
                error = "job_error_unclassified"
            payload = {
                **payload,
                "public_state": {
                    "status": state["status"],
                    "error_code": error,
                    **cancellation_fields(connection, state),
                },
            }
            if event_type == "job.failed":
                from .generation_diagnostics import (
                    safe_failure_summary,
                    failure_notice_id,
                )

                diagnostic = safe_failure_summary(
                    state, payload.pop("failure_context", None)
                )
                diagnostic["notice_id"] = failure_notice_id(state, diagnostic)
                original_result = (
                    state["result"] if isinstance(state["result"], dict) else {}
                )
                connection.execute(
                    update(jobs)
                    .where(jobs.c.id == aggregate_id)
                    .values(
                        result=canonical(
                            {**original_result, "failure_diagnostic": diagnostic}
                        )
                    )
                )
                payload["public_state"]["failure_diagnostic"] = diagnostic
        connection.execute(
            insert(outbox).values(
                id=event_id,
                event_type=event_type,
                aggregate_id=aggregate_id,
                payload=canonical(payload),
                created_at=self.clock(),
            )
        )
        if public_event and self.generation_transport is not None:
            self.generation_transport.job_event(
                connection, aggregate_id, event_id, event_type=event_type
            )
        if (
            event_type.startswith(("job.", "dstack."))
            or event_type == "instance.reserved"
        ):
            from .demand_wakeup import notify_demand

            notify_demand(connection)
        if public_event:
            connection.info["job_event_ids"].append(event_id)
        if event_type in {
            "job.created",
            "job.queued",
            "job.lease_expired",
            "job.submission_unknown",
        }:
            self._dispatch_wakeup(connection, aggregate_id)

    def _dispatch_wakeup(self, connection, job_id):
        """Commit-time hint only; historical delivery receipts stay untouched."""
        from .demand_wakeup import notify_demand

        notify_demand(connection)

    def pending_events(self, *, limit=100):
        _pagination(limit, 0)
        with self.engine.connect() as connection:
            return [
                dict(r)
                for r in connection.execute(
                    select(outbox)
                    .where(outbox.c.delivered_at.is_(None))
                    .order_by(outbox.c.created_at, outbox.c.id)
                    .limit(limit)
                ).mappings()
            ]

    def acknowledge_event(self, event_id):
        with self.transaction() as connection:
            connection.execute(
                update(outbox)
                .where(outbox.c.id == event_id, outbox.c.delivered_at.is_(None))
                .values(delivered_at=self.clock())
            )
