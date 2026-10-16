"""Read-only projection of the original business ledger for push notifications.

No schema creation, provider client, worker construction, settlement or task
mutation. Queries select closed scalars; provider config and user content never
appear in output. The daily projection is the existing guard, not a new ledger.
"""
import json
import math
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, or_, select

from .alert_conditions import STATES, identity
from .dstack_test_budget import ACCOUNT, projected_cost, validate_guard
from .generation_diagnostics import WANGP_VALIDATION_CODES, COMMON_CODES, _WORKER_CODES
from .inference.protocol import INFERENCE_FAILURE_CODES
from .operator_capacity import operator_nodes
from .repository import (Repository, attempts, budget_accounts, instance_intents,
    jobs, outbox, registered_workers, scaler_actions)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
WAKE_EVENTS = ("job.created", "job.queued", "job.lease_expired", "job.dispatch_requested")
MAX_ROWS = 10000


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _rows(connection, statement):
    result = list(connection.execute(statement.limit(MAX_ROWS+1)).mappings())
    if len(result) > MAX_ROWS:
        raise ValueError("alert_snapshot_too_large")
    return result


def _state_age(connection, tenant_id):
    # Poll/reconcile events repeatedly publish the same public status. The last
    # DIFFERENT status sequence is the boundary; updated_at is never an age.
    status = outbox.c.payload["public_state"]["status"].as_string()
    active = jobs.c.status.in_(tuple(STATES))
    different = select(outbox.c.aggregate_id.label("job_id"),
        func.max(outbox.c.job_event_seq).label("boundary")).select_from(outbox.join(jobs,
            outbox.c.aggregate_id == jobs.c.id)).where(jobs.c.tenant_id == tenant_id, active,
                status.is_not(None), status != jobs.c.status).group_by(outbox.c.aggregate_id).subquery()
    statement = select(jobs.c.id, func.min(outbox.c.created_at).label("since")).select_from(
        jobs.join(outbox, outbox.c.aggregate_id == jobs.c.id).outerjoin(different, different.c.job_id == jobs.c.id))
    statement = statement.where(jobs.c.tenant_id == tenant_id, active, status == jobs.c.status,
        outbox.c.job_event_seq > func.coalesce(different.c.boundary, 0)).group_by(jobs.c.id)
    return {row.id: row.since for row in connection.execute(statement)}


def read_snapshot(repo, *, now, tenant_id, test_guard=None, wake_window_seconds=1800):
    if not finite(now) or now < 0 or not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("alert_snapshot_context_invalid")
    if type(wake_window_seconds) is not int or not 60 <= wake_window_seconds <= 86400:
        raise ValueError("alert_wake_window_invalid")
    result = {"version": 1, "observed_at": now, "complete": True,
        "gpus": [], "jobs": [], "test_spend": None, "failures": []}
    with repo.engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        job_rows = _rows(connection, select(jobs.c.id, jobs.c.status, jobs.c.created_at,
            jobs.c.updated_at, jobs.c.pool, jobs.c.owner_id, jobs.c.lease_worker_id,
            jobs.c.execution_plan["deployment_profile_id"].as_string().label("profile_id"),
            jobs.c.result["failure_diagnostic"]["reason_code"].as_string().label("failure_code"),
            jobs.c.error_code).where(jobs.c.tenant_id == tenant_id, jobs.c.status.not_in(tuple(TERMINAL))))
        if any(identity(row["id"]) is None for row in job_rows):
            raise ValueError("alert_job_identity_invalid")
        ages = _state_age(connection, tenant_id)
        counts = {row.aggregate_id: row.count for row in connection.execute(select(outbox.c.aggregate_id,
            func.count().label("count")).select_from(outbox.join(jobs, outbox.c.aggregate_id == jobs.c.id)).where(
                jobs.c.tenant_id == tenant_id, jobs.c.status.in_(tuple(STATES)),
                outbox.c.event_type.in_(WAKE_EVENTS), outbox.c.created_at >= now-wake_window_seconds,
                outbox.c.created_at <= now).group_by(outbox.c.aggregate_id))}
        for row in job_rows:
            if row["status"] in STATES:
                result["jobs"].append({"id": row["id"], "status": row["status"],
                    "state_since": ages.get(row["id"]), "wake_count": counts.get(row["id"], 0)})
        # Collapse lifetime failures to the finite, closed source vocabulary in
        # SQL. One deterministic representative preserves event dedup identity.
        raw_code = func.coalesce(func.nullif(jobs.c.result["failure_diagnostic"]["reason_code"].as_string(), ""), jobs.c.error_code)
        vocabulary = tuple(WANGP_VALIDATION_CODES | COMMON_CODES | _WORKER_CODES | INFERENCE_FAILURE_CODES)
        reason = case((raw_code.in_(vocabulary), raw_code), else_="generation_failure_unclassified")
        failures = select(jobs.c.id, jobs.c.updated_at, reason.label("reason"),
            func.row_number().over(partition_by=reason, order_by=(jobs.c.updated_at, jobs.c.id)).label("rank"))            .where(jobs.c.tenant_id == tenant_id, jobs.c.status == "failed").subquery()
        for row in connection.execute(select(failures).where(failures.c.rank == 1)).mappings():
            if identity(row["id"]) is None:
                raise ValueError("alert_job_identity_invalid")
            result["failures"].append({"job_id": row["id"], "kind": row["reason"],
                "reason_code": row["reason"], "at": row["updated_at"]})
        projection = [instance_intents.c.id, instance_intents.c.pool, instance_intents.c.provider,
            instance_intents.c.provider_instance_id, instance_intents.c.state, instance_intents.c.created_at,
            scaler_actions.c.create_started_at, operator_nodes.c.runtime_state,
            operator_nodes.c.payload["tenant_id"].as_string().label("tenant_id"),
            operator_nodes.c.payload["owner_id"].as_string().label("owner_id"),
            operator_nodes.c.payload["dstack"]["profile_id"].as_string().label("profile_id"),
            operator_nodes.c.payload["dstack"]["first_runtime_ready_at"].label("ready_at"),
            operator_nodes.c.payload["lifecycle"].label("lifecycle")]
        node_query = select(*projection).select_from(instance_intents.outerjoin(operator_nodes,
            operator_nodes.c.intent_id == instance_intents.c.id).outerjoin(scaler_actions,
                scaler_actions.c.intent_id == instance_intents.c.id)).where(
                    operator_nodes.c.payload["tenant_id"].as_string() == tenant_id)
        # Latest historical guard remains relevant after all GPUs stop. Fetch
        # only that guard, while GPU observations contain current instances.
        guard_node = connection.execute(node_query.where(
            operator_nodes.c.payload["lifecycle"]["test_gpu_guard"]["daily_limit_microusd"].as_integer().is_not(None))
            .order_by(instance_intents.c.created_at.desc(), instance_intents.c.id.desc()).limit(1)).mappings().first()
        guards = [validate_guard(guard_node["lifecycle"]["test_gpu_guard"])] if guard_node else []
        nodes = _rows(connection, node_query.where(instance_intents.c.provider_instance_id.is_not(None),
            instance_intents.c.state != "destroyed", or_(operator_nodes.c.runtime_state.is_(None), operator_nodes.c.runtime_state != "stopped")))
        for node in nodes:
            lifecycle = node["lifecycle"] if isinstance(node["lifecycle"], dict) else {}
            if identity(node["id"]) is None:
                raise ValueError("alert_gpu_identity_invalid")
            worker_ids = select(registered_workers.c.id).where(
                registered_workers.c.provider == node["provider"],
                registered_workers.c.instance_id == node["provider_instance_id"])
            # Aggregate historical activity and unresolved submissions in SQL;
            # retired workers and terminal jobs still carry obligations.
            related_ids = select(attempts.c.job_id).where(attempts.c.worker_id.in_(worker_ids))
            current_ids = select(registered_workers.c.current_job_id).where(registered_workers.c.id.in_(worker_ids))
            linked = and_(jobs.c.tenant_id == tenant_id, or_(jobs.c.id.in_(related_ids),
                jobs.c.id.in_(current_ids), jobs.c.lease_worker_id.in_(worker_ids)))
            activity, active_count = connection.execute(select(func.max(jobs.c.updated_at),
                func.sum(case((jobs.c.status.not_in(tuple(TERMINAL)), 1), else_=0))).where(linked)).one()
            unsafe = connection.execute(select(attempts.c.id).select_from(attempts.join(jobs, jobs.c.id == attempts.c.job_id)).where(
                jobs.c.tenant_id == tenant_id, attempts.c.worker_id.in_(worker_ids),
                or_(attempts.c.submission_started_at.is_not(None), attempts.c.upstream_task_id.is_not(None)),
                or_(attempts.c.upstream_stopped.is_(None), attempts.c.upstream_stopped != 1)).limit(1)).first()
            busy = bool(active_count or unsafe)
            # Matching accepted queued demand pauses the original idle policy.
            busy |= any(row["status"] in {"queued", "waiting_capacity"} and row["pool"] == node["pool"]
                and row["owner_id"] == node["owner_id"] and row["profile_id"] == node["profile_id"] for row in job_rows)
            ready = node["ready_at"]
            idle_since = max(ready, activity) if finite(ready) and finite(activity) else ready if finite(ready) else None
            if lifecycle.get("keep_on") == "until" and finite(lifecycle.get("hold_until")) and idle_since is not None:
                idle_since = max(idle_since, lifecycle["hold_until"])
            idle_limit = lifecycle.get("idle_shutdown_seconds")
            if lifecycle.get("keep_on") == "until_stopped":
                idle_limit = None  # Explicit keep-on is not an auto-stop failure.
            started = node["create_started_at"] if node["create_started_at"] is not None else node["created_at"]
            result["gpus"].append({"id": node["id"], "running_since": started,
                "idle_since": idle_since, "idle_limit_seconds": idle_limit, "has_obligations": bool(busy)})
        account = connection.execute(select(budget_accounts.c.id).where(budget_accounts.c.id == ACCOUNT,
            budget_accounts.c.tenant_id == tenant_id)).first()
        if account:
            guard = validate_guard(test_guard) if test_guard is not None else guards[0] if guards else None
            if guard is None:
                raise ValueError("alert_test_guard_unconfirmed")
            result["test_spend"] = {"day": datetime.fromtimestamp(now, timezone.utc).astimezone(
                ZoneInfo("Australia/Sydney")).date().isoformat(), "projected_microusd": projected_cost(connection, guard, now),
                "limit_microusd": guard["daily_limit_microusd"]}
    return result


def main():
    from .settings import Settings
    repo = None
    now = time.time()
    try:
        settings = Settings.from_environment()
        repo = Repository(settings.database_url)
        value = read_snapshot(repo, now=now, tenant_id=settings.tenant_id)
        output = json.dumps(value, sort_keys=True, allow_nan=False)
    except Exception:
        value = {"version": 1, "observed_at": now, "complete": False,
            "gpus": [], "jobs": [], "test_spend": None, "failures": []}
        output = json.dumps(value, sort_keys=True)
    finally:
        if repo is not None:
            try:
                repo.close()
            except Exception:
                pass
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
