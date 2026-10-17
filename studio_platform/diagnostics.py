"""Read-only aggregate diagnostics. Never select prompts, URLs, tokens or payloads.

Public callers MUST supply an authenticated owner and authorized project. The
operator CLI has broader local DB access but still prints bounded aggregates,
not per-user/project/job labels or provider responses. No schema changes occur.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from .repository import (Repository, jobs, registered_workers, instance_intents,
                         budget_accounts, budget_reservations, identifier)
from .settings import Settings

JOB_STATES = ("planned", "blocked", "waiting_capacity", "queued", "claimed", "submitting", "submission_unknown",
              "running", "collecting", "cancel_requested", "recovery_hold", "succeeded", "failed", "cancelled")
WORKER_STATES = ("registered", "ready", "busy", "draining", "unknown", "retired")
INSTANCE_STATES = ("reserved", "creating", "creation_unknown", "starting", "busy",
                   "ready", "draining", "destroying", "destroy_unknown", "destroyed")
TERMINAL = ("succeeded", "failed", "cancelled", "blocked", "planned")


def _counted(connection, table, column, clauses, states):
    # CASE folds unexpected data into one bounded label before grouping.
    category = case((column.in_(states), column), else_="other")
    rows = connection.execute(select(category, func.count()).select_from(table)
                              .where(*clauses).group_by(category))
    result = {state: 0 for state in (*states, "other")}
    for state, count in rows:
        result[state] = count
    return result


def _age(now, value):
    return None if value is None else round(max(0, now-value), 3)


def job_activity(repository, *, tenant_id, owner_id=None, project_id=None):
    """Sample persisted job facts; queued age is not predicted wait or an SLO."""
    identifier(tenant_id)
    clauses = [jobs.c.tenant_id == tenant_id]
    if owner_id is not None:
        identifier(owner_id)
        clauses.append(jobs.c.owner_id == owner_id)
    if project_id is not None:
        identifier(project_id)
        clauses.append(jobs.c.project_id == project_id)
    now = repository.clock()
    if not math.isfinite(now):
        raise ValueError("invalid_diagnostic_clock")
    with repository.engine.connect() as conn:
        counts = _counted(conn, jobs, jobs.c.status, clauses, JOB_STATES)
        metrics = conn.execute(select(
            func.min(case((jobs.c.status == "queued", jobs.c.created_at))),
            func.min(case((and_(jobs.c.status == "queued", jobs.c.attempt_no == 0), jobs.c.created_at))),
            func.min(case((and_(jobs.c.status == "queued", jobs.c.attempt_no > 0), jobs.c.created_at))),
            func.sum(case((and_(jobs.c.status.not_in(TERMINAL), jobs.c.lease_worker_id.is_not(None),
                               jobs.c.lease_expires_at <= now), 1), else_=0)),
            func.sum(case((jobs.c.result["billing_status"].as_string() == "pending", 1), else_=0)),
            func.min(case((jobs.c.status == "waiting_capacity", jobs.c.created_at))),
        ).where(*clauses)).one()
    return {"observed_at": now, "consistency": "sampled_persisted_state", "total": sum(counts.values()),
            "counts": counts, "oldest_queued_age_s": _age(now, metrics[0]),
            "oldest_first_attempt_age_s": _age(now, metrics[1]),
            "oldest_retry_age_s": _age(now, metrics[2]),
            "expired_active_leases": metrics[3] or 0, "billing_pending": metrics[4] or 0,
            "oldest_capacity_wait_age_s": _age(now, metrics[5]),
            "note": "Queue age describes time already elapsed; it is not a promised completion time."}


def operator_snapshot(repository, *, tenant_id):
    """Trusted local operator only. Counts do not claim upstream stopped billing."""
    value = job_activity(repository, tenant_id=tenant_id)
    now = repository.clock()
    with repository.engine.connect() as conn:
        observed_worker = case(
            (registered_workers.c.state == "retired", "retired"),
            (registered_workers.c.expires_at <= now, "unknown"),
            (registered_workers.c.drain_requested == 1, "draining"),
            (registered_workers.c.current_job_id.is_not(None), "busy"),
            else_=registered_workers.c.state)
        workers = _counted(conn, registered_workers, observed_worker, [], WORKER_STATES)
        instances = _counted(conn, instance_intents, instance_intents.c.state, [], INSTANCE_STATES)
        limits = conn.execute(select(func.coalesce(func.sum(budget_accounts.c.limit_microusd), 0),
            func.coalesce(func.sum(budget_accounts.c.reserved_microusd), 0),
            func.coalesce(func.sum(budget_accounts.c.spent_microusd), 0))
            .where(budget_accounts.c.tenant_id == tenant_id)).one()
        unresolved = conn.execute(select(func.count()).select_from(budget_reservations.join(
            budget_accounts, budget_reservations.c.account_id == budget_accounts.c.id)).where(
                budget_accounts.c.tenant_id == tenant_id, budget_reservations.c.state.in_(("reserved", "invoice_pending")),
                or_(and_(budget_reservations.c.reference_type == "job",
                         budget_reservations.c.reference_id.in_(select(jobs.c.id).where(
                             jobs.c.tenant_id == tenant_id, jobs.c.result["billing_status"].as_string() == "pending"))),
                    and_(budget_reservations.c.reference_type == "instance",
                         budget_reservations.c.reference_id.in_(select(instance_intents.c.id).where(
                             instance_intents.c.state == "destroyed")))))).scalar_one()
    alerts = []
    if value["counts"]["submission_unknown"]:
        alerts.append("submission_unknown_requires_reconciliation_not_resubmission")
    if value["counts"]["recovery_hold"]:
        alerts.append("restored_jobs_held_for_operator_reconciliation")
    if value["expired_active_leases"] or workers["unknown"]:
        alerts.append("stale_worker_or_lease_is_not_idle")
    if value["billing_pending"] or unresolved:
        alerts.append("billing_unsettled_keep_reservations")
    if (value["oldest_queued_age_s"] or 0) > 900:
        alerts.append("queue_older_than_15_minutes_review_capacity_and_fairness")
    if (value["oldest_capacity_wait_age_s"] or 0) > 900:
        alerts.append("capacity_wait_older_than_15_minutes_review_boot_and_qualification")
    return {"schema": "sixnine-diagnostics-v1", "jobs": value, "workers": workers,
            "instances": instances, "worker_instance_scope": "whole_control_database",
            "budget_counters": {"limit_microusd": int(limits[0]), "reserved_microusd": int(limits[1]),
                "spent_microusd": int(limits[2]), "pending_reservations": unresolved,
                "note": "Overlapping account limits/reservations are summed, not deduplicated charges or available cash."},
            "alerts": alerts, "cloud_requests_performed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only Sixnine aggregate diagnostics; no cloud requests")
    parser.parse_args(argv)
    repo = None
    try:
        settings = Settings.from_environment()
        url = make_url(settings.database_url)
        if url.drivername == "sqlite":
            if not url.database or url.database == ":memory:":
                raise ValueError("Existing on-disk database required")
            path = Path(url.database).resolve(strict=True)
            url = url.set(database=path.as_uri(), query={"mode": "ro", "uri": "true"})
        repo = Repository(url.render_as_string(hide_password=False))
        if url.drivername == "postgresql+psycopg":
            repo.engine = repo.engine.execution_options(postgresql_readonly=True, isolation_level="REPEATABLE READ")
        # Never create_schema or construct Auth/asset/provider clients here.
        print(json.dumps(operator_snapshot(repo, tenant_id=settings.tenant_id), ensure_ascii=False))
        return 0
    except (ValueError, OSError, SQLAlchemyError):
        print("diagnostics_unavailable_check_existing_database_and_protected_configuration", file=sys.stderr)
        return 1
    finally:
        if repo is not None:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
