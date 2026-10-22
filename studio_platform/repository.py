"""Durable platform ledger. No provider calls, credentials, or legacy database access.

PostgreSQL URLs must use ``postgresql+psycopg://``. SQLite is a local verification
backend; its write transactions are serialized with BEGIN IMMEDIATE. Callers
authenticate principals and authorize project membership before constructing Scope.
Amounts are integer micro-US dollars; estimates are not supplier invoices.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import threading
import time
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    and_,
    create_engine,
    event,
    func,
    insert,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from .budget_policy import effective_budget_limit, fixed_cap_retired


class LedgerError(Exception):
    """Safe, stable business error; never includes database URLs or payloads."""


class NotFound(LedgerError):
    pass


class Conflict(LedgerError):
    pass


class BudgetExceeded(LedgerError):
    pass


class LeaseLost(Conflict):
    pass


class InvalidTransition(Conflict):
    pass


@dataclass(frozen=True)
class Scope:
    tenant_id: str
    owner_id: str
    project_id: str
    actor_id: str = "browser"

    def __post_init__(self):
        for value in (self.tenant_id, self.owner_id, self.project_id, self.actor_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ValueError("invalid_scope")


def canonical(value):
    """Freeze JSON values and reject NaN, non-JSON objects, and oversized payloads."""
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > 8 * 1024 * 1024:
        raise ValueError("document_too_large")
    return json.loads(encoded)


def request_hash(value):
    encoded = json.dumps(
        canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def money(value):
    if type(value) is not int or not 0 <= value <= 9_000_000_000_000_000:
        raise ValueError("invalid_microusd")
    return value


def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("invalid_identifier")
    return value


def budget_snapshot(row):
    """CAS identity of the original account, including committed balances."""
    value = {
        name: row[name]
        for name in (
            "id",
            "tenant_id",
            "owner_id",
            "project_id",
            "limit_microusd",
            "spent_microusd",
            "reserved_microusd",
        )
    }
    for name in ("id", "tenant_id"):
        identifier(value[name])
    for name in ("owner_id", "project_id"):
        if value[name] is not None:
            identifier(value[name])
    for name in ("limit_microusd", "spent_microusd", "reserved_microusd"):
        money(value[name])
    return request_hash(value)


metadata = MetaData()


def scope_columns():
    return [
        Column("tenant_id", String(200), nullable=False),
        Column("owner_id", String(200), nullable=False),
        Column("project_id", String(200), nullable=False),
    ]


documents = Table(
    "platform_documents",
    metadata,
    *scope_columns(),
    Column("kind", String(200), nullable=False),
    Column("document_id", String(200), nullable=False),
    Column("version", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("tenant_id", "owner_id", "project_id", "kind", "document_id"),
)
plans = Table(
    "platform_plans",
    metadata,
    Column("id", String(36), primary_key=True),
    *scope_columns(),
    Column("request", JSON, nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("execution_plan", JSON, nullable=False),
    Column("plan_hash", String(64), nullable=False),
    Column("estimated_cost_microusd", BigInteger, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
)
budget_accounts = Table(
    "platform_budget_accounts",
    metadata,
    Column("id", String(200), primary_key=True),
    Column("tenant_id", String(200), nullable=False),
    Column("owner_id", String(200)),
    Column("project_id", String(200)),
    Column("limit_microusd", BigInteger, nullable=False),
    Column("reserved_microusd", BigInteger, nullable=False, default=0),
    Column("spent_microusd", BigInteger, nullable=False, default=0),
    CheckConstraint(
        "reserved_microusd >= 0 AND spent_microusd >= 0 AND limit_microusd >= 0"
    ),
)
jobs = Table(
    "platform_jobs",
    metadata,
    Column("id", String(36), primary_key=True),
    *scope_columns(),
    Column("actor_id", String(200), nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("plan_id", String(36), ForeignKey("platform_plans.id"), nullable=False),
    Column("request", JSON, nullable=False),
    Column("execution_plan", JSON, nullable=False),
    Column("status", String(40), nullable=False),
    Column("pool", String(200), nullable=False),
    Column("expected_runtime_s", Float, nullable=False),
    Column("estimated_cost_microusd", BigInteger, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("not_before", Float, nullable=False),
    Column("attempt_no", Integer, nullable=False, default=0),
    Column("current_attempt_id", String(36)),
    Column("fence", BigInteger, nullable=False, default=0),
    Column("lease_worker_id", String(200)),
    Column("lease_expires_at", Float),
    Column("cancel_from_status", String(40)),
    Column("error_code", String(200)),
    Column("cancellation", JSON),
    Column("result", JSON),
    UniqueConstraint(
        "tenant_id", "owner_id", "project_id", "actor_id", "idempotency_key"
    ),
)
Index(
    "platform_jobs_ready",
    jobs.c.pool,
    jobs.c.status,
    jobs.c.not_before,
    jobs.c.created_at,
)
attempts = Table(
    "platform_attempts",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("job_id", String(36), ForeignKey("platform_jobs.id"), nullable=False),
    Column("number", Integer, nullable=False),
    Column("status", String(40), nullable=False),
    Column("fence", BigInteger, nullable=False),
    Column("worker_id", String(200), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("submission_started_at", Float),
    Column("upstream_task_id", String(200)),
    Column("upstream_stopped", Integer, nullable=False, default=0),
    Column("collection_failures", Integer, nullable=False, default=0),
    Column("error_code", String(200)),
    Column("actual_cost_microusd", BigInteger),
    UniqueConstraint("job_id", "number"),
)
budget_reservations = Table(
    "platform_budget_reservations",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "account_id",
        String(200),
        ForeignKey("platform_budget_accounts.id"),
        nullable=False,
    ),
    Column("reference_type", String(30), nullable=False),
    Column("reference_id", String(200), nullable=False),
    Column("amount_microusd", BigInteger, nullable=False),
    Column("state", String(30), nullable=False),
    Column("actual_cost_microusd", BigInteger),
    Column("created_at", Float, nullable=False),
    Column("settled_at", Float),
    UniqueConstraint("account_id", "reference_type", "reference_id"),
)
spending_settings = Table(
    "platform_spending_settings",
    metadata,
    Column("tenant_id", String(200), primary_key=True),
    Column("owner_id", String(200), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("value", JSON, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("actor_id", String(200), nullable=False),
)
spending_origins = Table(
    "platform_spending_origins",
    metadata,
    Column("reference_type", String(30), primary_key=True),
    Column("reference_id", String(200), primary_key=True),
    Column("tenant_id", String(200), nullable=False),
    Column("owner_id", String(200), nullable=False),
    Column("actor_id", String(200), nullable=False),
    Column("created_at", Float, nullable=False),
)
spending_confirmations = Table(
    "platform_spending_confirmations",
    metadata,
    Column("tenant_id", String(200), primary_key=True),
    Column("owner_id", String(200), primary_key=True),
    Column("plan_id", String(36), primary_key=True),
    Column("actor_id", String(200), primary_key=True),
    Column("plan_hash", String(64), nullable=False),
    Column("settings_version", Integer, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("consumed_reference", String(200)),
)
billing_adjustments = Table(
    "platform_billing_adjustments",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(200), nullable=False),
    Column("owner_id", String(200), nullable=False),
    Column("reference_type", String(30), nullable=False),
    Column("reference_id", String(200), nullable=False),
    Column("kind", String(30), nullable=False),
    Column("action_id", String(36), nullable=False),
    Column("actor_id", String(200), nullable=False),
    Column("reason", String(1000), nullable=False),
    Column("evidence_sha256", String(64), nullable=False),
    Column("evidence_note", String(2000), nullable=False),
    Column("basis_exposure_microusd", BigInteger, nullable=False),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("tenant_id", "owner_id", "reference_type", "reference_id", "kind"),
)
artifacts = Table(
    "platform_artifacts",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("job_id", String(36), ForeignKey("platform_jobs.id"), nullable=False),
    Column(
        "attempt_id", String(36), ForeignKey("platform_attempts.id"), nullable=False
    ),
    Column("metadata", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
)
outbox = Table(
    "platform_outbox",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("event_type", String(100), nullable=False),
    Column("aggregate_id", String(200), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("delivered_at", Float),
    Column("job_event_seq", BigInteger),
)
job_event_order = Index(
    "platform_outbox_job_event_order", outbox.c.job_event_seq, unique=True
)
JOB_EVENT_LOCK = 685939796868749722
dispatch_receipts = Table(
    "platform_dispatch_receipts",
    metadata,
    Column("event_id", String(36), ForeignKey("platform_outbox.id"), primary_key=True),
    Column("job_id", String(36), ForeignKey("platform_jobs.id"), nullable=False),
    Column("state", String(30), nullable=False),
    Column("publisher_id", String(200)),
    Column("lease_expires_at", Float),
    Column("external_run_id", String(200)),
    Column("publish_attempts", Integer, nullable=False, default=0),
    Column("sequence", Integer, nullable=False),
    Column("not_before", Float, nullable=False, default=0),
    Column("updated_at", Float, nullable=False),
    Column("error_code", String(100)),
    UniqueConstraint("job_id", "sequence"),
)
scheduler_state = Table(
    "platform_scheduler_state",
    metadata,
    Column("pool", String(200), primary_key=True),
    Column("last_owner", String(500)),
)
owner_usage = Table(
    "platform_owner_usage",
    metadata,
    Column("pool", String(200), nullable=False),
    Column("owner_key", String(500), nullable=False),
    Column("work_s", Float, nullable=False),
    UniqueConstraint("pool", "owner_key"),
)
pool_limits = Table(
    "platform_pool_limits",
    metadata,
    Column("pool", String(200), primary_key=True),
    Column("max_instances", Integer, nullable=False),
    Column("max_physical_gpus", Integer, nullable=False),
)
instance_intents = Table(
    "platform_instance_intents",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("intent_key", String(200), nullable=False),
    Column("pool", String(200), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("state", String(40), nullable=False),
    Column("physical_gpus", Integer, nullable=False),
    Column("slots", Integer, nullable=False),
    Column("reserved_cost_microusd", BigInteger, nullable=False),
    Column("provider_instance_id", String(200)),
    Column("hard_deadline", Float),
    Column(
        "provider",
        String(200),
        nullable=False,
        default="unknown",
        server_default="unknown",
    ),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("pool", "intent_key"),
)
capacity_gate = Table(
    "platform_capacity_gate",
    metadata,
    Column("id", String(30), primary_key=True),
    Column("max_instances", Integer, nullable=False),
    Column("max_physical_gpus", Integer, nullable=False),
)
registered_workers = Table(
    "platform_registered_workers",
    metadata,
    Column("id", String(200), primary_key=True),
    Column("pool", String(200), nullable=False),
    Column("provider", String(200), nullable=False),
    Column("instance_id", String(200), nullable=False),
    Column("spec", JSON, nullable=False),
    Column("spec_hash", String(64), nullable=False),
    Column("state", String(40), nullable=False),
    Column("current_job_id", String(36)),
    Column("drain_requested", Integer, nullable=False, default=0, server_default="0"),
    Column("fence", BigInteger, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
registered_devices = Table(
    "platform_registered_devices",
    metadata,
    Column("provider", String(200), nullable=False),
    Column("instance_id", String(200), nullable=False),
    Column("gpu_id", String(200), nullable=False),
    Column(
        "worker_id",
        String(200),
        ForeignKey("platform_registered_workers.id"),
        nullable=False,
    ),
    Column("state", String(30), nullable=False),
    UniqueConstraint("provider", "instance_id", "gpu_id"),
)
cpu_slots = Table(
    "platform_cpu_slots",
    metadata,
    Column("instance_id", String(200), primary_key=True),
    Column(
        "worker_id",
        String(200),
        ForeignKey("platform_registered_workers.id"),
        nullable=False,
    ),
    Column("state", String(30), nullable=False),
)
scaler_leaders = Table(
    "platform_scaler_leaders",
    metadata,
    Column("pool", String(200), primary_key=True),
    Column("leader_id", String(200), nullable=False),
    Column("fence", BigInteger, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("policy_hash", String(64)),
    Column("consecutive_breaches", Integer, nullable=False),
    Column("last_scale_at", Float),
    Column("sequence", BigInteger, nullable=False),
    Column("last_observed_at", Float),
)
scaler_observations = Table(
    "platform_scaler_observations",
    metadata,
    Column("pool", String(200), primary_key=True),
    Column("sequence", BigInteger, primary_key=True),
    Column("observed_at", Float, nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("recommendation", JSON, nullable=False),
)
scaler_actions = Table(
    "platform_scaler_actions",
    metadata,
    Column(
        "intent_id",
        String(36),
        ForeignKey("platform_instance_intents.id"),
        primary_key=True,
    ),
    Column("pool", String(200), nullable=False),
    Column("launch_spec", JSON, nullable=False),
    Column("create_started_at", Float),
    Column("destroy_started_at", Float),
    Column("last_observation", JSON),
    Column("last_observed_at", Float),
    Column("application_idle_since", Float),
)
scaler_receipts = Table(
    "platform_scaler_receipts",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "intent_id",
        String(36),
        ForeignKey("platform_instance_intents.id"),
        nullable=False,
    ),
    Column("operation", String(30), nullable=False),
    Column("observed_at", Float, nullable=False),
    Column("facts", JSON, nullable=False),
)
capacity_approvals = Table(
    "platform_capacity_approvals",
    metadata,
    Column("id", String(200), primary_key=True),
    Column("tenant_id", String(200), nullable=False),
    Column("pool", String(200), nullable=False),
    Column("configuration_id", String(200), nullable=False),
    Column("approval_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("enabled", Integer, nullable=False, default=0),
    Column("expires_at", Float, nullable=False),
    Column("created_at", Float, nullable=False),
)
capacity_cycles = Table(
    "platform_capacity_cycles",
    metadata,
    Column(
        "approval_id",
        String(200),
        ForeignKey("platform_capacity_approvals.id"),
        primary_key=True,
    ),
    Column(
        "intent_id",
        String(36),
        ForeignKey("platform_instance_intents.id"),
        unique=True,
        nullable=False,
    ),
    Column("created_at", Float, nullable=False),
)
capacity_waiters = Table(
    "platform_capacity_waiters",
    metadata,
    Column("job_id", String(36), ForeignKey("platform_jobs.id"), primary_key=True),
    Column(
        "approval_id",
        String(200),
        ForeignKey("platform_capacity_approvals.id"),
        nullable=False,
    ),
    Column("approval_hash", String(64), nullable=False),
    Column("deadline", Float, nullable=False),
    Column("intent_id", String(36), ForeignKey("platform_instance_intents.id")),
    Column("state", String(40), nullable=False),
    Column("created_at", Float, nullable=False),
)
Index(
    "platform_capacity_waiters_pending",
    capacity_waiters.c.approval_id,
    capacity_waiters.c.state,
    capacity_waiters.c.created_at,
)

# Opt-in pool approvals only. Membership is immutable, not another rental ledger.
capacity_pool_members = Table(
    "platform_capacity_pool_members",
    metadata,
    Column(
        "approval_id",
        String(200),
        ForeignKey("platform_capacity_approvals.id"),
        primary_key=True,
    ),
    Column("member_id", String(200), primary_key=True),
    Column("approval_hash", String(64), nullable=False),
    Column(
        "intent_id",
        String(36),
        ForeignKey("platform_instance_intents.id"),
        unique=True,
        nullable=False,
    ),
    Column("created_at", Float, nullable=False),
)

# Additive ancestry only: generation zero remains in capacity_pool_members.
# The greatest validated ordinal is the current binding, including unknown rent.
capacity_member_generations = Table(
    "platform_capacity_member_generations",
    metadata,
    Column(
        "approval_id",
        String(200),
        ForeignKey("platform_capacity_approvals.id"),
        primary_key=True,
    ),
    Column("member_id", String(200), primary_key=True),
    Column("generation", Integer, primary_key=True),
    Column("approval_hash", String(64), nullable=False),
    Column(
        "previous_intent_id",
        String(36),
        ForeignKey("platform_instance_intents.id"),
        unique=True,
        nullable=False,
    ),
    Column(
        "intent_id",
        String(36),
        ForeignKey("platform_instance_intents.id"),
        unique=True,
        nullable=False,
    ),
    Column("created_at", Float, nullable=False),
    CheckConstraint("generation > 0 AND generation <= 16"),
)


class Repository:
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

    def put_document(self, scope, kind, document_id, payload, *, expected_version=None):
        kind, document_id, payload = (
            identifier(kind),
            identifier(document_id),
            canonical(payload),
        )
        where = and_(
            self._scope(documents, scope),
            documents.c.kind == kind,
            documents.c.document_id == document_id,
        )
        try:
            with self.transaction() as connection:
                self._content_write_guard(
                    connection,
                    scope,
                    payload,
                    project_id=document_id
                    if scope.project_id == "__projects" and kind == "project"
                    else scope.project_id,
                )
                row = self._locked(connection, select(documents).where(where))
                if row is None:
                    if expected_version not in (None, 0):
                        raise Conflict("document_version_conflict")
                    connection.execute(
                        insert(documents).values(
                            tenant_id=scope.tenant_id,
                            owner_id=scope.owner_id,
                            project_id=scope.project_id,
                            kind=kind,
                            document_id=document_id,
                            payload=payload,
                            version=1,
                            updated_at=self.clock(),
                        )
                    )
                else:
                    # Blind overwrites of existing documents are intentionally disallowed.
                    if expected_version != row["version"]:
                        raise Conflict("document_version_conflict")
                    connection.execute(
                        update(documents)
                        .where(where)
                        .values(
                            payload=payload,
                            version=row["version"] + 1,
                            updated_at=self.clock(),
                        )
                    )
                return dict(
                    connection.execute(select(documents).where(where)).mappings().one()
                )
        except IntegrityError:
            raise Conflict("document_version_conflict") from None

    def get_document(self, scope, kind, document_id):
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(documents).where(
                        self._scope(documents, scope),
                        documents.c.kind == kind,
                        documents.c.document_id == document_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound("document_not_found")
            return dict(row)

    def list_documents(
        self,
        scope,
        kind,
        *,
        limit=100,
        offset=0,
        allowed_ids=None,
        summary=False,
        exclude_managed=False,
        story_chat=False,
    ):
        """Project catalogs can read metadata without materializing every draft.

        Summary rows preserve the document envelope and expose only payload.title.
        Full documents remain available through get_document/default list usage.
        """
        _pagination(limit, offset)
        if type(summary) is not bool or (summary and kind != "project"):
            raise ValueError("invalid_document_summary")
        allowed = _allowed_ids(allowed_ids)
        if allowed == ():
            return []
        columns = [column for column in documents.c if column.name != "payload"]
        statement = (
            select(*columns, documents.c.payload["title"].as_string().label("_title"))
            if summary
            else select(documents)
        )
        statement = statement.where(
            self._scope(documents, scope), documents.c.kind == kind
        )
        if exclude_managed:
            # Purges retain document identities for audit/history even when an
            # older trash marker is absent. Filter the tombstone before paging.
            statement = statement.where(
                documents.c.payload["content_deleted"].as_boolean().is_not(True)
            )
            trash = documents.alias("project_trash")
            statement = statement.where(
                ~documents.c.document_id.in_(
                    select(trash.c.payload["target"]["id"].as_string()).where(
                        trash.c.tenant_id == scope.tenant_id,
                        trash.c.owner_id == scope.owner_id,
                        trash.c.project_id == "__trash",
                        trash.c.kind == "trash_item",
                        trash.c.payload["target"]["kind"].as_string() == "project",
                        trash.c.payload["state"]
                        .as_string()
                        .in_(("trashed", "purging", "purged")),
                    )
                )
            )
            marker = documents.c.payload["integration_kind"].as_string()
            statement = statement.where(or_(marker.is_(None), marker != "quick_chat"))
        if allowed is not None:
            statement = statement.where(documents.c.document_id.in_(allowed))
        order = (documents.c.document_id,)
        if story_chat:
            # Story chats (#422): projects carrying journey.storyChat, newest first.
            if kind != "project":
                raise ValueError("invalid_document_summary")
            marker = documents.c.payload[("journey", "storyChat", "version")]
            # Typed, cast-free predicates: a malformed legacy marker (string, object,
            # bool) is simply not a story chat and must never raise in SQL.
            if self.engine.dialect.name == "postgresql":
                typed = and_(
                    func.json_typeof(marker) == "number", marker.as_string() == "1"
                )
            else:
                typed = and_(
                    func.json_type(documents.c.payload, "$.journey.storyChat.version")
                    == "integer",
                    marker.as_integer() == 1,
                )
            statement = statement.where(typed)
            order = (documents.c.updated_at.desc(), documents.c.document_id)
        with self.engine.connect() as connection:
            values = [
                dict(r)
                for r in connection.execute(
                    statement.order_by(*order).limit(limit).offset(offset)
                ).mappings()
            ]
        if summary:
            for value in values:
                value["payload"] = {"title": value.pop("_title")}
        return values

    def create_plan(
        self,
        scope,
        request,
        execution_plan,
        *,
        expires_at,
        estimated_cost_microusd=0,
        plan_id=None,
    ):
        if not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at):
            raise ValueError("invalid_plan_expiry")
        if expires_at <= self.clock():
            raise Conflict("plan_expired")
        request, execution_plan = canonical(request), canonical(execution_plan)
        if execution_plan.get("dispatch_backend", "legacy") not in {
            "legacy",
            "hatchet-v1",
        }:
            raise ValueError("invalid_dispatch_backend")
        row = dict(
            id=plan_id or str(uuid.uuid4()),
            tenant_id=scope.tenant_id,
            owner_id=scope.owner_id,
            project_id=scope.project_id,
            request=request,
            request_hash=request_hash(request),
            execution_plan=execution_plan,
            plan_hash=request_hash(execution_plan),
            estimated_cost_microusd=money(estimated_cost_microusd),
            created_at=self.clock(),
            expires_at=float(expires_at),
        )
        with self.transaction() as connection:
            self._content_write_guard(connection, scope, request)
            if plan_id is not None:
                previous = (
                    connection.execute(select(plans).where(plans.c.id == plan_id))
                    .mappings()
                    .first()
                )
                if previous:
                    if any(
                        previous[k] != row[k]
                        for k in ("tenant_id", "owner_id", "project_id", "request_hash")
                    ):
                        raise Conflict("plan_idempotency_conflict")
                    return dict(previous)
            connection.execute(insert(plans).values(**row))
        return row

    def get_plan(self, scope, plan_id):
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(plans).where(
                        plans.c.id == plan_id, self._scope(plans, scope)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound("plan_not_found")
            return dict(row)

    def configure_budget(
        self, account_id, *, tenant_id, limit_microusd, owner_id=None, project_id=None
    ):
        identifier(account_id)
        identifier(tenant_id)
        limit_microusd = money(limit_microusd)
        if project_id is not None and owner_id is None:
            raise ValueError("project_budget_requires_owner")
        with self.transaction() as connection:
            row = self._locked(
                connection,
                select(budget_accounts).where(budget_accounts.c.id == account_id),
            )
            if row:
                if (row["tenant_id"], row["owner_id"], row["project_id"]) != (
                    tenant_id,
                    owner_id,
                    project_id,
                ):
                    raise Conflict("budget_scope_conflict")
                if row["reserved_microusd"] + row["spent_microusd"] > limit_microusd:
                    raise BudgetExceeded("budget_below_committed")
                connection.execute(
                    update(budget_accounts)
                    .where(budget_accounts.c.id == account_id)
                    .values(limit_microusd=limit_microusd)
                )
            else:
                connection.execute(
                    insert(budget_accounts).values(
                        id=account_id,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        project_id=project_id,
                        limit_microusd=limit_microusd,
                        reserved_microusd=0,
                        spent_microusd=0,
                    )
                )

    def get_budget(self, account_id):
        """Trusted control-plane operation; do not expose this without authorization."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(budget_accounts).where(budget_accounts.c.id == account_id)
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound("budget_not_found")
            return dict(row)

    @contextmanager
    def _budget_read(self):
        """Account balances and their settlement dates share one read snapshot."""
        guard = self._memory_lock if self._is_memory else _NullLock()
        with guard, self.engine.connect() as connection:
            if self.engine.dialect.name == "postgresql":
                connection = connection.execution_options(
                    isolation_level="REPEATABLE READ", postgresql_readonly=True
                )
                connection.begin()
            else:
                connection.exec_driver_sql("BEGIN")
            try:
                yield connection
            finally:
                connection.rollback()

    def _budget_now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError("invalid_budget_clock")
        return float(now)

    def _budget_projection(self, connection, row, now):
        today = datetime.fromtimestamp(now, timezone.utc).date()
        start = datetime.combine(
            today - timedelta(days=6), datetime.min.time(), timezone.utc
        ).timestamp()
        confirmed = and_(
            budget_reservations.c.account_id == row["id"],
            budget_reservations.c.state.in_(("settled", "released")),
            budget_reservations.c.actual_cost_microusd.is_not(None),
        )
        # Settlement date is the confirmed ledger event, never reservation
        # creation or a guessed supplier-invoice date. Legacy NULLs stay apart.
        day = (
            func.date(
                func.timezone(
                    "UTC", func.to_timestamp(budget_reservations.c.settled_at)
                )
            )
            if connection.dialect.name == "postgresql"
            else func.date(budget_reservations.c.settled_at, "unixepoch")
        )
        daily = connection.execute(
            select(
                day.label("date"),
                func.sum(budget_reservations.c.actual_cost_microusd).label(
                    "cost_microusd"
                ),
            )
            .where(
                confirmed,
                budget_reservations.c.settled_at >= start,
                budget_reservations.c.settled_at <= now,
            )
            .group_by(day)
            .order_by(day)
        ).mappings()
        legacy = (
            connection.execute(
                select(
                    func.coalesce(
                        func.sum(budget_reservations.c.actual_cost_microusd), 0
                    ).label("cost_microusd"),
                    func.count().label("reservation_count"),
                ).where(confirmed, budget_reservations.c.settled_at.is_(None))
            )
            .mappings()
            .one()
        )
        invoice_pending = (
            connection.execute(
                select(
                    func.count().label("reservation_count"),
                    func.coalesce(
                        func.sum(budget_reservations.c.amount_microusd), 0
                    ).label("reserved_estimate_microusd"),
                ).where(
                    budget_reservations.c.account_id == row["id"],
                    budget_reservations.c.state == "invoice_pending",
                )
            )
            .mappings()
            .one()
        )
        # Read the original account layer only. Rental invoices are not
        # per-video invoices, and estimate-only jobs are never free generation.
        costs = {
            kind: {
                "confirmed_cost_microusd": 0,
                "confirmed_reservation_count": 0,
                "pending_invoice_count": 0,
                "pending_estimate_microusd": 0,
                "pending_actual_cost_microusd": None,
            }
            for kind in ("job", "instance")
        }
        groups = connection.execute(
            select(
                budget_reservations.c.reference_type,
                budget_reservations.c.state,
                func.count().label("count"),
                func.coalesce(func.sum(budget_reservations.c.amount_microusd), 0).label(
                    "estimate"
                ),
                func.coalesce(
                    func.sum(budget_reservations.c.actual_cost_microusd), 0
                ).label("actual"),
            )
            .where(
                budget_reservations.c.account_id == row["id"],
                or_(
                    budget_reservations.c.state == "invoice_pending",
                    and_(
                        budget_reservations.c.state.in_(("settled", "released")),
                        budget_reservations.c.actual_cost_microusd.is_not(None),
                    ),
                ),
            )
            .group_by(budget_reservations.c.reference_type, budget_reservations.c.state)
        ).mappings()
        for group in groups:
            cost = costs.get(group["reference_type"])
            if cost is None:
                continue
            if group["state"] == "invoice_pending":
                cost["pending_invoice_count"] += int(group["count"])
                cost["pending_estimate_microusd"] += int(group["estimate"])
            else:
                cost["confirmed_reservation_count"] += int(group["count"])
                cost["confirmed_cost_microusd"] += int(group["actual"])
        limit, spent, reserved = (
            row[name]
            for name in ("limit_microusd", "spent_microusd", "reserved_microusd")
        )
        retired = fixed_cap_retired(row)
        committed = spent + reserved
        return {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "owner_id": row["owner_id"],
            "project_id": row["project_id"],
            "basis": "cumulative",
            "currency": "USD",
            "limit_microusd": limit,
            "limit_mode": "settings" if retired else "cumulative",
            "legacy_projection_only": retired,
            "cost_breakdown": {
                "basis": "original_account_reservations",
                "account_layers_not_additive": True,
                **costs,
            },
            "invoice_pending": {
                "reservation_count": int(invoice_pending["reservation_count"]),
                "reserved_estimate_microusd": int(
                    invoice_pending["reserved_estimate_microusd"]
                ),
                "actual_cost_microusd": None,
                "included_in_settings_exposure": True,
            },
            "legacy_limit_microusd": row["limit_microusd"]
            if fixed_cap_retired(row)
            else None,
            "spent_microusd": spent,
            "reserved_microusd": reserved,
            "remaining_microusd": max(0, limit - committed),
            "usage_percent": min(100, committed * 100 / limit) if limit else 100,
            "status": "exhausted"
            if committed >= limit
            else "warning"
            if committed * 5 >= limit * 4
            else "ok",
            "snapshot": budget_snapshot(row),
            "as_of": now,
            "window": {
                "timezone": "UTC",
                "start_date": (today - timedelta(days=6)).isoformat(),
                "end_date": today.isoformat(),
            },
            "daily7": [
                {
                    "date": str(value["date"]),
                    "cost_microusd": int(value["cost_microusd"]),
                }
                for value in daily
            ],
            "unattributed_legacy": {
                "cost_microusd": int(legacy["cost_microusd"]),
                "reservation_count": int(legacy["reservation_count"]),
            },
        }

    def list_budget_projections(
        self,
        tenant_id,
        *,
        owner_id=None,
        account_ids=None,
        admin=False,
        limit=100,
        after_id=None,
    ):
        """Authorized service supplies tenant, owner and trusted configured IDs.

        A non-admin can never obtain a global or another owner's account. An
        optional trusted configured-ID selection can only narrow that scope.
        """
        identifier(tenant_id)
        if type(admin) is not bool or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_budget_pagination")
        if after_id is not None:
            identifier(after_id)
        statement = select(budget_accounts).where(
            budget_accounts.c.tenant_id == tenant_id
        )
        if not admin:
            identifier(owner_id)
            statement = statement.where(budget_accounts.c.owner_id == owner_id)
            if account_ids is not None:
                if (
                    not isinstance(account_ids, (tuple, list, set, frozenset))
                    or len(account_ids) > 128
                ):
                    raise ValueError("invalid_budget_selection")
                allowed = tuple(sorted({identifier(value) for value in account_ids}))
                statement = statement.where(budget_accounts.c.id.in_(allowed))
        if after_id is not None:
            statement = statement.where(budget_accounts.c.id > after_id)
        now = self._budget_now()
        with self._budget_read() as connection:
            rows = list(
                connection.execute(
                    statement.order_by(budget_accounts.c.id).limit(limit + 1)
                ).mappings()
            )
            values = [
                self._budget_projection(connection, row, now) for row in rows[:limit]
            ]
        return {
            "budgets": values,
            "next_after_id": rows[limit - 1]["id"] if len(rows) > limit else None,
            "as_of": now,
        }

    def compare_set_budget_limit(
        self,
        account_id,
        *,
        tenant_id,
        expected_snapshot,
        limit_microusd,
        actor_id,
        actor_owner,
    ):
        """Change only an existing original limit and audit it atomically."""
        for value in (account_id, tenant_id, actor_id, actor_owner):
            identifier(value)
        if (
            not isinstance(expected_snapshot, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_snapshot) is None
        ):
            raise ValueError("invalid_budget_snapshot")
        limit_microusd = money(limit_microusd)
        now = self._budget_now()
        with self.transaction() as connection:
            row = self._locked(
                connection,
                select(budget_accounts).where(
                    budget_accounts.c.id == account_id,
                    budget_accounts.c.tenant_id == tenant_id,
                ),
            )
            if row is None:
                raise NotFound("budget_not_found")
            old_snapshot = budget_snapshot(row)
            if old_snapshot != expected_snapshot:
                raise Conflict("budget_snapshot_changed")
            if limit_microusd < row["spent_microusd"] + row["reserved_microusd"]:
                raise BudgetExceeded("budget_below_committed")
            changed = limit_microusd != row["limit_microusd"]
            audit_event_id = None
            if changed:
                connection.execute(
                    update(budget_accounts)
                    .where(
                        budget_accounts.c.id == account_id,
                        budget_accounts.c.tenant_id == tenant_id,
                    )
                    .values(limit_microusd=limit_microusd)
                )
                new = dict(row, limit_microusd=limit_microusd)
                audit_event_id = str(uuid.uuid4())
                names = ("limit_microusd", "spent_microusd", "reserved_microusd")
                payload = {
                    "version": 1,
                    "tenant_id": tenant_id,
                    "account_id": account_id,
                    "actor": {"owner_id": actor_owner, "actor_id": actor_id},
                    "owner_id": row["owner_id"],
                    "project_id": row["project_id"],
                    "old": {name: row[name] for name in names},
                    "new": {name: new[name] for name in names},
                    "old_snapshot": old_snapshot,
                    "new_snapshot": budget_snapshot(new),
                }
                connection.execute(
                    insert(outbox).values(
                        id=audit_event_id,
                        event_type="operator.budget_limit_updated",
                        aggregate_id=account_id,
                        payload=canonical(payload),
                        created_at=now,
                    )
                )
                row = new
            return {
                "budget": self._budget_projection(connection, row, now),
                "changed": changed,
                "audit_event_id": audit_event_id,
            }

    def list_budget_audit(self, tenant_id, *, limit=100, before=None):
        identifier(tenant_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_budget_pagination")
        statement = select(outbox).where(
            outbox.c.event_type == "operator.budget_limit_updated",
            outbox.c.payload["tenant_id"].as_string() == tenant_id,
        )
        if before is not None:
            if not isinstance(before, str) or len(before) > 80:
                raise ValueError("invalid_budget_audit_cursor")
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?):([0-9a-f-]{36})", before)
            if not match:
                raise ValueError("invalid_budget_audit_cursor")
            stamp = float(match[1])
            if not math.isfinite(stamp) or str(uuid.UUID(match[2])) != match[2]:
                raise ValueError("invalid_budget_audit_cursor")
            statement = statement.where(
                or_(
                    outbox.c.created_at < stamp,
                    and_(outbox.c.created_at == stamp, outbox.c.id < match[2]),
                )
            )
        with self._budget_read() as connection:
            rows = list(
                connection.execute(
                    statement.order_by(
                        outbox.c.created_at.desc(), outbox.c.id.desc()
                    ).limit(limit + 1)
                ).mappings()
            )
        events = [
            {
                "id": row["id"],
                "account_id": row["aggregate_id"],
                "created_at": row["created_at"],
                **{
                    key: row["payload"][key]
                    for key in (
                        "actor",
                        "owner_id",
                        "project_id",
                        "old",
                        "new",
                        "old_snapshot",
                        "new_snapshot",
                    )
                },
            }
            for row in rows[:limit]
        ]
        cursor = (
            f'{rows[limit - 1]["created_at"]}:{rows[limit - 1]["id"]}'
            if len(rows) > limit
            else None
        )
        return {"events": events, "next_before": cursor}

    def _reserve(
        self,
        connection,
        scope,
        account_ids,
        reference_type,
        reference_id,
        amount,
        *,
        spending_actor=None,
        confirmation_required=False,
        spending_settings_version=None,
    ):
        from .spending_limits import admit_spending

        admit_spending(
            self,
            connection,
            scope,
            reference_type,
            reference_id,
            amount,
            actor_id=spending_actor or scope.actor_id,
            confirmation_required=confirmation_required,
            spending_settings_version=spending_settings_version,
        )
        account_ids = sorted(set(account_ids))
        role_map = {}
        if reference_type == "job":
            from .credit_admission import validate

            job = (
                connection.execute(
                    select(jobs).where(
                        jobs.c.id == reference_id,
                        self._scope(jobs, scope),
                    )
                )
                .mappings()
                .first()
            )
            # Legacy/manual accounting references can predate a job row. They
            # retain enforced USD admission; absence can never confer a role.
            if job is not None:
                role_map = validate(connection, scope, job, account_ids)
        if amount and not account_ids:
            raise BudgetExceeded("budget_not_configured")
        for account_id in account_ids:
            row = self._locked(
                connection,
                select(budget_accounts).where(budget_accounts.c.id == account_id),
            )
            if (
                not row
                or row["tenant_id"] != scope.tenant_id
                or (row["owner_id"] is not None and row["owner_id"] != scope.owner_id)
                or (
                    row["project_id"] is not None
                    and row["project_id"] != scope.project_id
                )
            ):
                raise NotFound("budget_not_found")
            if role_map.get(account_id) != "accounting" and row["spent_microusd"] + row[
                "reserved_microusd"
            ] + amount > effective_budget_limit(row):
                raise BudgetExceeded("budget_exceeded")
            connection.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == account_id)
                .values(reserved_microusd=budget_accounts.c.reserved_microusd + amount)
            )
            reservation_id = str(uuid.uuid4())
            connection.execute(
                insert(budget_reservations).values(
                    id=reservation_id,
                    account_id=account_id,
                    reference_type=reference_type,
                    reference_id=reference_id,
                    amount_microusd=amount,
                    state="reserved",
                    created_at=self.clock(),
                )
            )
            if role_map:
                from .credit_admission import store_role

                store_role(connection, reservation_id, role_map[account_id])
        return role_map

    def _settle(self, connection, reference_type, reference_id, actual_cost):
        actual_cost = money(actual_cost)
        settled_at = self._budget_now()
        # Settlement records authoritative actuals under the original lock
        # order. Admission caps must not add an owner/account lock inversion or
        # prevent late costs from being recorded truthfully.
        rows = list(
            connection.execute(
                select(budget_reservations)
                .where(
                    budget_reservations.c.reference_type == reference_type,
                    budget_reservations.c.reference_id == reference_id,
                )
                .order_by(budget_reservations.c.account_id)
                .with_for_update()
            ).mappings()
        )
        for row in rows:
            if row["state"] not in {"reserved", "invoice_pending"}:
                if row["actual_cost_microusd"] != actual_cost:
                    raise Conflict("budget_already_settled")
                continue
            if (
                row["state"] == "invoice_pending"
                and row["actual_cost_microusd"] is not None
            ):
                raise Conflict("budget_pending_invoice_invalid")
            # Account locks use a stable order for concurrent tenant/global budgets.
            self._locked(
                connection,
                select(budget_accounts).where(
                    budget_accounts.c.id == row["account_id"]
                ),
            )
            connection.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == row["account_id"])
                .values(
                    reserved_microusd=budget_accounts.c.reserved_microusd
                    - (row["amount_microusd"] if row["state"] == "reserved" else 0),
                    spent_microusd=budget_accounts.c.spent_microusd + actual_cost,
                )
            )
            connection.execute(
                update(budget_reservations)
                .where(budget_reservations.c.id == row["id"])
                .values(
                    state="released" if actual_cost == 0 else "settled",
                    actual_cost_microusd=actual_cost,
                    settled_at=settled_at,
                )
            )

    def fund_instance_window(
        self, connection, intent_id, minimum_reserved_cost_microusd, budget_account_ids
    ):
        """Extend an explicit continuing machine's original money reservation.

        This grants funded runway, not another rental, deadline or invoice. All
        accounts and reservation rows are checked before any counter changes;
        a caller catches BudgetExceeded only outside this transaction.
        """
        from .operator_capacity import operator_nodes

        minimum = money(minimum_reserved_cost_microusd)
        self._lock_capacity(connection)
        intent = self._locked(
            connection,
            select(instance_intents).where(instance_intents.c.id == intent_id),
        )
        node = self._locked(
            connection,
            select(operator_nodes).where(operator_nodes.c.intent_id == intent_id),
        )
        if not intent or not node:
            raise NotFound("instance_intent_not_found")
        payload = node["payload"]
        lifecycle = payload.get("lifecycle", {})
        request, binding = payload.get("request", {}), payload.get("dstack", {})
        if (
            payload.get("capacity_backend") != "dstack-v1"
            or lifecycle.get("version") != 2
            or request.get("lifecycle_version") != 2
            or binding.get("lifecycle_version") != 2
            or intent["provider"] not in {"vast", "runpod"}
            or intent["hard_deadline"] is not None
            or payload.get("hard_deadline") is not None
            or request.get("hard_deadline") is not None
            or request.get("max_duration_s") is not None
            or node["desired_state"] != "running"
            or intent["state"]
            not in {
                "reserved",
                "creating",
                "creation_unknown",
                "starting",
                "ready",
                "busy",
            }
        ):
            raise Conflict("instance_continuing_policy_unconfirmed")
        ids = sorted(set(budget_account_ids))
        if (
            not ids
            or len(ids) != len(budget_account_ids)
            or ids != sorted(lifecycle.get("budget_account_ids", []))
        ):
            raise BudgetExceeded("instance_budget_binding_changed")
        from .spending_limits import admit_spending

        origin = (
            connection.execute(
                select(spending_origins).where(
                    spending_origins.c.reference_type == "instance",
                    spending_origins.c.reference_id == intent_id,
                )
            )
            .mappings()
            .first()
        )
        admit_spending(
            self,
            connection,
            Scope(payload["tenant_id"], payload["owner_id"], payload["project_id"]),
            "instance",
            intent_id,
            max(0, minimum - intent["reserved_cost_microusd"]),
            actor_id=origin["actor_id"] if origin else "legacy-instance",
            record_origin=False,
        )
        accounts = []
        for account_id in ids:
            account = self._locked(
                connection,
                select(budget_accounts).where(budget_accounts.c.id == account_id),
            )
            if (
                not account
                or account["tenant_id"] != payload.get("tenant_id")
                or account["owner_id"] is not None
                and account["owner_id"] != payload.get("owner_id")
                or account["project_id"] is not None
                and account["project_id"] != payload.get("project_id")
            ):
                raise BudgetExceeded("instance_budget_binding_changed")
            accounts.append(account)
        if not any(
            account["owner_id"] == payload.get("owner_id")
            and account["owner_id"] is not None
            for account in accounts
        ):
            raise BudgetExceeded("instance_owner_budget_required")
        rows = list(
            connection.execute(
                select(budget_reservations)
                .where(
                    budget_reservations.c.reference_type == "instance",
                    budget_reservations.c.reference_id == intent_id,
                )
                .order_by(budget_reservations.c.account_id)
                .with_for_update()
            ).mappings()
        )
        current = intent["reserved_cost_microusd"]
        if (
            len(rows) != len(ids)
            or [row["account_id"] for row in rows] != ids
            or any(
                row["state"] != "reserved"
                or row["amount_microusd"] != current
                or row["actual_cost_microusd"] is not None
                for row in rows
            )
        ):
            raise BudgetExceeded("instance_reservation_unconfirmed")
        total = max(minimum, current)
        extra = total - current
        if any(
            account["reserved_microusd"] < current
            or account["spent_microusd"] + account["reserved_microusd"] + extra
            > effective_budget_limit(account)
            for account in accounts
        ):
            raise BudgetExceeded("instance_spend_limit_reached")
        if not extra:
            return current
        for account_id in ids:
            connection.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == account_id)
                .values(reserved_microusd=budget_accounts.c.reserved_microusd + extra)
            )
        for row in rows:
            connection.execute(
                update(budget_reservations)
                .where(budget_reservations.c.id == row["id"])
                .values(amount_microusd=total)
            )
        connection.execute(
            update(instance_intents)
            .where(instance_intents.c.id == intent_id)
            .values(reserved_cost_microusd=total, updated_at=self.clock())
        )
        self._emit(
            connection,
            "instance.reservation_extended",
            intent_id,
            {
                "intent_id": intent_id,
                "previous_microusd": current,
                "reserved_microusd": total,
                "additional_microusd": extra,
                "account_ids": ids,
            },
        )
        return total

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
            execution.get("backend") not in {"comfy-worker", "wangp-worker"}
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

    def approve_capacity(self, approval_id, **kwargs):
        """Trusted operator only; creates an immutable approval, not a cloud instance."""
        from .capacity import approve_capacity

        return approve_capacity(self, approval_id, **kwargs)

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

    def reserve_capacity_member(self, approval_id, member_id, *, connection=None):
        from .capacity import reserve_capacity_member

        return reserve_capacity_member(
            self, approval_id, member_id, connection=connection
        )

    def set_capacity_approval_enabled(self, approval_id, *, enabled):
        if type(enabled) is not bool:
            raise ValueError("invalid_capacity_approval_switch")
        with self.transaction() as connection:
            row = self._locked(
                connection,
                select(capacity_approvals).where(
                    capacity_approvals.c.id == approval_id
                ),
            )
            if row is None:
                raise NotFound("capacity_approval_not_found")
            if enabled and row["expires_at"] <= self.clock():
                raise Conflict("capacity_approval_expired")
            connection.execute(
                update(capacity_approvals)
                .where(capacity_approvals.c.id == approval_id)
                .values(enabled=int(enabled))
            )

    def find_capacity_approval(
        self, scope, *, pool, model_id, configuration_id, recipe_id, policy_hash
    ):
        from .capacity import find_capacity_approval

        return find_capacity_approval(
            self,
            scope,
            pool=pool,
            model_id=model_id,
            configuration_id=configuration_id,
            recipe_id=recipe_id,
            policy_hash=policy_hash,
        )

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

    def configure_pool(self, pool, *, max_instances=0, max_physical_gpus=0):
        identifier(pool)
        if (
            type(max_instances) is not int
            or type(max_physical_gpus) is not int
            or min(max_instances, max_physical_gpus) < 0
        ):
            raise ValueError("invalid_capacity_limit")
        with self.transaction() as connection:
            row = self._locked(
                connection, select(pool_limits).where(pool_limits.c.pool == pool)
            )
            if row:
                connection.execute(
                    update(pool_limits)
                    .where(pool_limits.c.pool == pool)
                    .values(
                        max_instances=max_instances, max_physical_gpus=max_physical_gpus
                    )
                )
            else:
                connection.execute(
                    insert(pool_limits).values(
                        pool=pool,
                        max_instances=max_instances,
                        max_physical_gpus=max_physical_gpus,
                    )
                )

    def configure_capacity(self, *, max_instances=0, max_physical_gpus=0):
        """Explicit approved global ceiling; does not create or release any GPU."""
        if (
            type(max_instances) is not int
            or type(max_physical_gpus) is not int
            or min(max_instances, max_physical_gpus) < 0
        ):
            raise ValueError("invalid_capacity_limit")
        with self.transaction() as connection:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            put = sqlite_insert if self.engine.dialect.name == "sqlite" else pg_insert
            connection.execute(
                put(capacity_gate)
                .values(id="global", max_instances=0, max_physical_gpus=0)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            self._locked(
                connection, select(capacity_gate).where(capacity_gate.c.id == "global")
            )
            connection.execute(
                update(capacity_gate)
                .where(capacity_gate.c.id == "global")
                .values(
                    max_instances=max_instances, max_physical_gpus=max_physical_gpus
                )
            )

    def _global_usage(self, connection):
        resources = {}
        for row in connection.execute(
            select(instance_intents).where(instance_intents.c.state != "destroyed")
        ).mappings():
            if manually_reviewed_inactive(connection, row):
                continue
            key = (
                row["provider"],
                row["provider_instance_id"] or "intent:" + row["id"],
            )
            resources[key] = resources.get(key, 0) + row["physical_gpus"]
        device_counts = {}
        for row in connection.execute(
            select(registered_devices).where(
                registered_devices.c.state != "released",
                registered_devices.c.provider != "mock",
            )
        ).mappings():
            key = (row["provider"], row["instance_id"])
            device_counts[key] = device_counts.get(key, 0) + 1
        for key, count in device_counts.items():
            resources[key] = max(resources.get(key, 0), count)
        return {
            "instances": len(resources),
            "physical_gpus": sum(resources.values()),
            "resources": resources,
        }

    def _lock_capacity(self, connection):
        row = self._locked(
            connection, select(capacity_gate).where(capacity_gate.c.id == "global")
        )
        if row is None:
            raise BudgetExceeded("global_capacity_disabled")
        return row

    def reserve_instance_intent(
        self,
        scope,
        pool,
        intent_key,
        *,
        physical_gpus=1,
        slots=1,
        reserved_cost_microusd=0,
        hard_deadline,
        budget_account_ids=(),
        dry_run=True,
        provider="unknown",
        connection=None,
        continuing=False,
        capacity_usage=None,
        active_instance_ids=None,
        capacity_policy_snapshot=None,
        capacity_provenance=None,
    ):
        """Reserve an intent, NEVER create an instance. Default dry-run makes no writes."""
        identifier(pool)
        identifier(intent_key)
        identifier(provider)
        cost = money(reserved_cost_microusd)
        if (
            type(physical_gpus) is not int
            or type(slots) is not int
            or min(physical_gpus, slots) < 1
        ):
            raise ValueError("invalid_instance_capacity")
        if type(continuing) is not bool or (
            continuing
            and (
                provider not in {"vast", "runpod"}
                or hard_deadline is not None
                or capacity_usage is None
            )
        ):
            raise ValueError("invalid_continuing_capacity")
        if not continuing and (
            not isinstance(hard_deadline, (int, float))
            or not math.isfinite(hard_deadline)
        ):
            raise ValueError("invalid_instance_deadline")
        if hard_deadline is not None and hard_deadline <= self.clock():
            raise Conflict("instance_deadline_expired")
        admitted_capacity = None
        if capacity_policy_snapshot is not None or capacity_provenance is not None:
            from .capacity_settings import validate_snapshot

            admitted_capacity = validate_snapshot(capacity_policy_snapshot)
            if (
                not isinstance(capacity_provenance, dict)
                or capacity_provenance
                != {
                    "capacity_backend": "dstack-v1",
                    "lifecycle_version": 2,
                    "tenant_id": scope.tenant_id,
                    "owner_id": scope.owner_id,
                    "project_id": scope.project_id,
                    "pool": pool,
                    "provider": provider,
                }
                or type(capacity_provenance.get("lifecycle_version")) is not int
                or not continuing
                or provider not in {"vast", "runpod"}
                or physical_gpus != 1
                or slots != 1
            ):
                raise ValueError("capacity_admission_provenance_invalid")
        request = dict(
            physical_gpus=physical_gpus,
            slots=slots,
            reserved_cost_microusd=cost,
            hard_deadline=hard_deadline,
            budget_account_ids=sorted(set(budget_account_ids)),
            scope=scope.__dict__,
            provider=provider,
        )
        if admitted_capacity is not None:
            request.update(
                capacity_policy_snapshot=capacity_policy_snapshot,
                capacity_provenance=capacity_provenance,
            )
        digest = request_hash(request)
        if dry_run:
            return {
                "dry_run": True,
                "created": False,
                "pool": pool,
                "intent_key": intent_key,
            }
        if not cost or not budget_account_ids:
            raise BudgetExceeded("instance_budget_not_configured")
        try:
            with (
                self.transaction()
                if connection is None
                else nullcontext(connection) as connection
            ):
                # All controllers serialize capacity reservations on the same durable pool row.
                global_limits = self._lock_capacity(connection)
                limits = self._locked(
                    connection, select(pool_limits).where(pool_limits.c.pool == pool)
                )
                if limits is None:
                    raise BudgetExceeded("pool_disabled")
                existing = (
                    connection.execute(
                        select(instance_intents).where(
                            instance_intents.c.pool == pool,
                            instance_intents.c.intent_key == intent_key,
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing:
                    if existing["request_hash"] != digest:
                        raise Conflict("instance_intent_conflict")
                    return {**dict(existing), "created": False, "dry_run": False}
                if admitted_capacity is not None:
                    from .capacity_settings import admission_snapshot, read_policy

                    if capacity_policy_snapshot != admission_snapshot(
                        read_policy(connection, scope.tenant_id)
                    ):
                        raise Conflict("capacity_policy_snapshot_changed")
                    # Adopted operating counts replace old positive numeric
                    # ceilings, never the explicit global/pool disabled gates.
                    if (
                        min(
                            global_limits["max_instances"],
                            global_limits["max_physical_gpus"],
                        )
                        <= 0
                    ):
                        raise BudgetExceeded("global_capacity_disabled")
                    if min(limits["max_instances"], limits["max_physical_gpus"]) <= 0:
                        raise BudgetExceeded("pool_disabled")
                global_max_instances = (
                    admitted_capacity["max_machines"]
                    if admitted_capacity is not None
                    else global_limits["max_instances"]
                )
                global_max_gpus = (
                    admitted_capacity["max_machines"]
                    if admitted_capacity is not None
                    else global_limits["max_physical_gpus"]
                )
                usage = (
                    self._global_usage(connection)
                    if capacity_usage is None
                    else capacity_usage
                )
                if any(
                    type(usage.get(k)) is not int or usage[k] < 0
                    for k in ("instances", "physical_gpus")
                ):
                    raise ValueError("invalid_capacity_observation")
                if (
                    usage["instances"] + 1 > global_max_instances
                    or usage["physical_gpus"] + physical_gpus > global_max_gpus
                ):
                    raise BudgetExceeded("global_capacity_exceeded")
                active = list(
                    connection.execute(
                        select(instance_intents).where(
                            instance_intents.c.pool == pool,
                            instance_intents.c.state != "destroyed",
                        )
                    ).mappings()
                )
                active = [
                    row
                    for row in active
                    if not manually_reviewed_inactive(connection, row)
                ]
                if active_instance_ids is not None:
                    active = [row for row in active if row["id"] in active_instance_ids]
                if len(active) + 1 > (
                    admitted_capacity["max_machines"]
                    if admitted_capacity is not None
                    else limits["max_instances"]
                ) or sum(r["physical_gpus"] for r in active) + physical_gpus > (
                    admitted_capacity["max_machines"]
                    if admitted_capacity is not None
                    else limits["max_physical_gpus"]
                ):
                    raise BudgetExceeded("instance_capacity_exceeded")
                row = dict(
                    id=str(uuid.uuid4()),
                    intent_key=intent_key,
                    pool=pool,
                    request_hash=digest,
                    state="reserved",
                    physical_gpus=physical_gpus,
                    slots=slots,
                    reserved_cost_microusd=cost,
                    provider=provider,
                    hard_deadline=float(hard_deadline)
                    if hard_deadline is not None
                    else None,
                    created_at=self.clock(),
                    updated_at=self.clock(),
                )
                connection.execute(insert(instance_intents).values(**row))
                self._reserve(
                    connection, scope, budget_account_ids, "instance", row["id"], cost
                )
                event = {"intent_id": row["id"], "pool": pool}
                if admitted_capacity is not None:
                    event.update(
                        capacity_policy_snapshot=capacity_policy_snapshot,
                        capacity_provenance=capacity_provenance,
                    )
                self._emit(
                    connection,
                    "instance.reserved",
                    row["id"],
                    event,
                )
                return {**row, "created": True, "dry_run": False}
        except IntegrityError:
            raise Conflict("instance_intent_conflict") from None

    def update_instance(
        self,
        intent_id,
        state,
        *,
        provider_instance_id=None,
        destruction_confirmed=False,
        actual_cost_microusd=None,
        connection=None,
    ):
        """Trusted controller reconciles facts; unknown/expired instances retain reservations."""
        allowed = {
            "reserved": {"creating", "destroyed"},
            "creating": {"creation_unknown", "starting", "destroyed"},
            "creation_unknown": {"starting", "destroying", "destroyed"},
            "starting": {"ready", "draining", "destroying"},
            "ready": {"busy", "draining", "destroying"},
            "busy": {"ready", "draining"},
            "draining": {"destroying"},
            "destroying": {"destroyed"},
            "destroyed": set(),
        }
        with (
            self.transaction()
            if connection is None
            else nullcontext(connection) as connection
        ):
            row = self._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == intent_id),
            )
            if row is None:
                raise NotFound("instance_intent_not_found")
            values = dict(state=state, updated_at=self.clock())
            if provider_instance_id is not None:
                identifier(provider_instance_id)
                if row["provider_instance_id"] not in (None, provider_instance_id):
                    raise Conflict("provider_instance_conflict")
                values["provider_instance_id"] = provider_instance_id
            if state == row["state"]:
                if state == "destroyed" and actual_cost_microusd is not None:
                    self._settle(
                        connection, "instance", intent_id, actual_cost_microusd
                    )
                return dict(row)
            if state not in allowed[row["state"]]:
                raise InvalidTransition("invalid_instance_transition")
            if state in ("starting", "ready", "busy") and not (
                provider_instance_id or row["provider_instance_id"]
            ):
                raise Conflict("missing_provider_instance_id")
            if state == "destroyed":
                if not destruction_confirmed:
                    raise Conflict("destruction_not_confirmed")
                if actual_cost_microusd is not None:
                    self._settle(
                        connection, "instance", intent_id, actual_cost_microusd
                    )
            connection.execute(
                update(instance_intents)
                .where(instance_intents.c.id == intent_id)
                .values(**values)
            )
            return dict(
                connection.execute(
                    select(instance_intents).where(instance_intents.c.id == intent_id)
                )
                .mappings()
                .one()
            )

    def settle_instance_cost(self, intent_id, *, actual_cost_microusd, connection=None):
        """Invoice reconciliation is independent of confirmed physical destruction."""
        money(actual_cost_microusd)
        with (
            self.transaction()
            if connection is None
            else nullcontext(connection) as connection
        ):
            row = self._locked(
                connection,
                select(instance_intents).where(instance_intents.c.id == intent_id),
            )
            if row is None:
                raise NotFound("instance_intent_not_found")
            if row["state"] != "destroyed":
                raise Conflict("instance_not_destroyed")
            self._settle(connection, "instance", intent_id, actual_cost_microusd)
            return self._instance_billing(connection, dict(row))

    @staticmethod
    def _instance_billing(connection, row):
        reservations = list(
            connection.execute(
                select(budget_reservations).where(
                    budget_reservations.c.reference_type == "instance",
                    budget_reservations.c.reference_id == row["id"],
                )
            ).mappings()
        )
        pending = any(
            r["state"] in {"reserved", "invoice_pending"} for r in reservations
        )
        actual = (
            None
            if pending or not reservations
            else reservations[0]["actual_cost_microusd"]
        )
        review = manually_reviewed_inactive(connection, row)
        return {
            **row,
            "billing_status": "pending"
            if pending
            else "settled"
            if reservations
            else "unknown",
            "actual_cost_microusd": actual,
            **({"manual_review": review} if review else {}),
        }

    def list_instance_intents(self, *, pool=None):
        statement = select(instance_intents)
        if pool is not None:
            statement = statement.where(instance_intents.c.pool == pool)
        with self.engine.connect() as connection:
            return [
                self._instance_billing(connection, dict(r))
                for r in connection.execute(
                    statement.order_by(instance_intents.c.created_at)
                ).mappings()
            ]


def manually_reviewed_inactive(connection, intent):
    """Explicit operator risk acceptance, never provider removal or settlement.

    Exact Lium/Targon account absence can be attested after a dashboard stop.
    Preserve the underlying intent state, so review cannot become physical
    removal evidence for replacement, settlement or recovery code.
    """
    if (
        intent["provider"] not in {"lium", "targon"}
        or intent["state"]
        not in {
            "creating",
            "creation_unknown",
            "starting",
            "ready",
            "busy",
            "draining",
            "destroying",
        }
        or intent["provider_instance_id"] is None
        and intent["state"] not in {"creating", "creation_unknown"}
        or intent["provider_instance_id"] is not None
        and not intent["provider_instance_id"]
    ):
        return None
    row = (
        connection.execute(
            select(scaler_receipts)
            .where(
                scaler_receipts.c.intent_id == intent["id"],
                scaler_receipts.c.operation == "manual_review",
            )
            .order_by(scaler_receipts.c.observed_at.desc(), scaler_receipts.c.id.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    facts = row["facts"] if row else None
    if (
        not isinstance(facts, dict)
        or facts.get("schema_version") != 1
        or facts.get("state") != "manually_reviewed"
        or facts.get("intent_id") != intent["id"]
        or "instance_id" not in facts
        or facts.get("instance_id") != intent["provider_instance_id"]
        or facts.get("account_absent") is not True
        or facts.get("no_continuing_charge") is not True
        or not isinstance(facts.get("actor"), str)
        or not isinstance(facts.get("operation_id"), str)
    ):
        return None
    if "provider" in facts or "original_intent_state" in facts:
        if (
            facts.get("provider") != intent["provider"]
            or facts.get("original_intent_state") != intent["state"]
        ):
            return None
    elif (
        intent["provider"] != "targon"
        or intent["state"] != "destroying"
        or intent["provider_instance_id"] is None
    ):
        # Preserve the previously accepted Targon-only receipt format. It must
        # not become new Lium or active-state authority by omission.
        return None
    return {**facts, "observed_at": row["observed_at"]}


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def paused_capacity_pools(connection):
    """Project explicit zero ceilings only; not free capacity or runtime approval.

    Missing limits still fail at reservation, but are not an operator pause.
    The reserve barrier remains authoritative for races and occupied pools.
    """
    return frozenset(
        connection.execute(
            select(pool_limits.c.pool).where(
                or_(
                    pool_limits.c.max_instances == 0,
                    pool_limits.c.max_physical_gpus == 0,
                )
            )
        ).scalars()
    )


def _allowed_ids(values):
    """Bounded explicit authorization set; empty never broadens into unfiltered."""
    if values is None:
        return None
    if not isinstance(values, (list, tuple, set, frozenset)) or len(values) > 4096:
        raise ValueError("invalid_authorized_identifier_set")
    return tuple(sorted({identifier(value) for value in values}))


def _pagination(limit, offset):
    if (
        type(limit) is not int
        or not 1 <= limit <= 1000
        or type(offset) is not int
        or offset < 0
    ):
        raise ValueError("invalid_pagination")
