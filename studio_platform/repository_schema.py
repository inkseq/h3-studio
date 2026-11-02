"""Original ledger tables, types and value helpers; one metadata owner, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

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
)


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


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


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


# Keep historical pickle/import identity while sharing one definition.
for _public_type in (
    LedgerError,
    NotFound,
    Conflict,
    BudgetExceeded,
    LeaseLost,
    InvalidTransition,
    Scope,
):
    _public_type.__module__ = "studio_platform.repository"
del _public_type
