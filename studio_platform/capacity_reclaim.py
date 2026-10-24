"""Explicit lost-attempt authority on the original job, never supplier stop proof.

Only trusted control-plane callers may request this transition. No provider IO,
new job/admission, quote, reservation or financial settlement is performed here.
"""

import copy
import logging
import math
import uuid

from sqlalchemy import insert, select, update

from .repository import (
    Conflict,
    InvalidTransition,
    artifacts,
    attempts,
    identifier,
    jobs,
    outbox,
)

MAX_RECLAIMS = 2
REASONS = {
    "capacity_no_progress",
    "capacity_runtime_exceeded",
    "capacity_owner_recovery",
}
FIELDS = {
    "version",
    "job_id",
    "attempt_id",
    "number",
    "worker_id",
    "attempt_fence",
    "previous_fence",
    "fence",
    "reason",
    "evidence_id",
    "at",
    "last_progress_at",
    "state",
    "count",
    "event_id",
}
logger = logging.getLogger(__name__)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def history(job):
    """Fail closed on malformed authority; legacy jobs have no lost-attempt grant."""
    result = job.get("result") or {}
    if not isinstance(result, dict):
        raise Conflict("capacity_reclaim_history_invalid")
    count = result.get("capacity_reclaim_count", 0)
    records = result.get("capacity_reclaims", [])
    valid = type(count) is int and 0 <= count <= MAX_RECLAIMS
    valid = valid and isinstance(records, list) and len(records) <= MAX_RECLAIMS + 1
    if not valid:
        raise Conflict("capacity_reclaim_history_invalid")
    seen, requeued, previous_epoch = set(), 0, -1
    for index, row in enumerate(records):
        if not isinstance(row, dict) or set(row) != FIELDS:
            raise Conflict("capacity_reclaim_history_invalid")
        for field in ("job_id", "attempt_id", "worker_id", "evidence_id", "event_id"):
            if not isinstance(row[field], str) or not 1 <= len(row[field]) <= 200:
                raise Conflict("capacity_reclaim_history_invalid")
        if row["state"] == "requeued":
            requeued += 1
        valid = (
            type(row["version"]) is int
            and row["version"] == 1
            and row["job_id"] == job["id"]
            and row["attempt_id"] not in seen
            and isinstance(row["reason"], str)
            and row["reason"] in REASONS
            and all(
                type(row[k]) is int and row[k] >= 0
                for k in ("number", "attempt_fence", "previous_fence", "fence", "count")
            )
            and row["number"] > 0
            and row["attempt_fence"] <= row["previous_fence"]
            and row["fence"] == row["previous_fence"] + 1
            and row["previous_fence"] >= previous_epoch
            and finite(row["at"])
            and (
                row["last_progress_at"] is None
                or finite(row["last_progress_at"])
                and row["last_progress_at"] <= row["at"]
            )
            and (
                row["state"] == "requeued"
                and requeued <= MAX_RECLAIMS
                and row["count"] == requeued
                or row["state"] == "exhausted"
                and index == len(records) - 1
                and requeued == MAX_RECLAIMS
                and row["count"] == MAX_RECLAIMS
            )
        )
        if not valid:
            raise Conflict("capacity_reclaim_history_invalid")
        seen.add(row["attempt_id"])
        previous_epoch = row["fence"]
    if requeued != count:
        raise Conflict("capacity_reclaim_history_invalid")
    return count, copy.deepcopy(records)


def authorized_lost_attempt(job, attempt):
    """Execution authority only. This MUST NOT be used as billing/removal proof."""
    try:
        _, records = history(job)
    except (Conflict, KeyError, TypeError):
        return False
    return any(
        attempt["status"] == "attempt_lost"
        and attempt["job_id"] == job["id"]
        and row["attempt_id"] == attempt["id"]
        and row["number"] == attempt["number"]
        and row["worker_id"] == attempt["worker_id"]
        and row["attempt_fence"] == attempt["fence"]
        and row["fence"] <= job["fence"]
        and job["current_attempt_id"] != attempt["id"]
        for row in records
    )


def retained_fields(job):
    count, records = history(job)
    return (
        {"capacity_reclaim_count": count, "capacity_reclaims": records}
        if records
        else {}
    )


def aggregate_cost(connection, job, current_attempt_id, cost):
    """A delivered attempt cannot erase an earlier unresolved supplier liability."""
    _, records = history(job)
    if not records or cost is None:
        return cost
    total = cost
    lost_ids = {row["attempt_id"] for row in records}
    prior = (
        connection.execute(
            select(attempts).where(
                attempts.c.job_id == job["id"], attempts.c.id.in_(lost_ids)
            )
        )
        .mappings()
        .all()
    )
    if len(prior) != len(lost_ids):
        raise Conflict("capacity_reclaim_history_invalid")
    for attempt in prior:
        if attempt["id"] == current_attempt_id or not authorized_lost_attempt(
            job, attempt
        ):
            raise Conflict("capacity_reclaim_history_invalid")
        if attempt["actual_cost_microusd"] is None or attempt["upstream_stopped"] != 1:
            return None
        total += attempt["actual_cost_microusd"]
    return total


def cancel_before_replacement(repo, connection, job):
    """Cancel queued/unsubmitted replacement work without settling lost invoices."""
    if job["status"] not in {"queued", "claimed"}:
        return None
    _, records = history(job)
    if not records:
        return None
    prior = (
        connection.execute(select(attempts).where(attempts.c.job_id == job["id"]))
        .mappings()
        .all()
    )
    lost_ids = {row["attempt_id"] for row in records}
    if {row["id"] for row in prior if authorized_lost_attempt(job, row)} != lost_ids:
        raise Conflict("capacity_reclaim_history_invalid")
    current = next(
        (row for row in prior if row["id"] == job["current_attempt_id"]), None
    )
    if job["current_attempt_id"] is not None and current is None:
        return None
    if any(
        row["id"] not in lost_ids
        and (
            row["submission_started_at"] is not None
            or row["upstream_task_id"] is not None
        )
        for row in prior
    ):
        return None
    now = repo.clock()
    if current is not None:
        connection.execute(
            update(attempts)
            .where(attempts.c.id == current["id"])
            .values(status="cancelled", actual_cost_microusd=0, updated_at=now)
        )
    result = {
        **(job.get("result") or {}),
        "actual_cost_microusd": None,
        "billing_status": "pending",
    }
    result.pop("capacity_progress", None)
    connection.execute(
        update(jobs)
        .where(jobs.c.id == job["id"])
        .values(
            status="cancelled",
            cancel_from_status=job["status"],
            result=result,
            fence=job["fence"] + 1,
            lease_worker_id=None,
            lease_expires_at=None,
            updated_at=now,
        )
    )
    from .credits import release

    cancelled = repo._job(connection, job["id"])
    release(connection, repo, cancelled, reason="cancelled_before_replacement")
    repo._emit(
        connection,
        "job.cancel_requested",
        job["id"],
        {"job_id": job["id"], "status": "cancelled", "upstream_stopped": False},
    )
    return cancelled


def reclaim(
    queue,
    scope,
    job_id,
    *,
    expected_attempt_id,
    expected_fence,
    reason,
    evidence_id,
    last_progress_at=None,
    no_progress_seconds=600,
    runtime_floor_seconds=1800,
    connection=None,
):
    """Fence one exact original attempt; the caller separately stops its owned node.

    Owner recovery is an explicit trusted incident operation, not an HTTP option.
    Automatic callers must supply the last positive engine heartbeat, never a
    lease renewal or a failed status read. Replays return the original receipt.
    """
    from contextlib import nullcontext
    from .identity import lock_account_gate

    for value in (job_id, expected_attempt_id, evidence_id):
        identifier(value)
    if (
        not isinstance(reason, str)
        or reason not in REASONS
        or type(expected_fence) is not int
        or expected_fence < 0
    ):
        raise ValueError("capacity_reclaim_request_invalid")
    if not all(
        finite(v) and 1 <= v <= 86400
        for v in (no_progress_seconds, runtime_floor_seconds)
    ):
        raise ValueError("capacity_reclaim_timeout_invalid")
    repo = queue.repository
    with repo.transaction() if connection is None else nullcontext(connection) as conn:
        lock_account_gate(conn)
        job = repo._job(conn, job_id, scope, lock=True)
        count, records = history(job)
        for original in records:
            if original["attempt_id"] == expected_attempt_id:
                if (
                    original["previous_fence"],
                    original["reason"],
                    original["evidence_id"],
                    original["last_progress_at"],
                ) != (expected_fence, reason, evidence_id, last_progress_at):
                    raise Conflict("capacity_reclaim_idempotency_conflict")
                return {**original, "replayed": True}
        if (
            job["current_attempt_id"] != expected_attempt_id
            or job["fence"] != expected_fence
        ):
            raise Conflict("capacity_reclaim_conflict")
        if job["status"] not in {
            "submitting",
            "submission_unknown",
            "running",
            "collecting",
            "recovery_hold",
        }:
            raise InvalidTransition("capacity_reclaim_phase_invalid")
        if (
            job.get("cancellation") is not None
            or (job.get("result") or {}).get("recovery_cancel_requested") is True
        ):
            raise Conflict("capacity_reclaim_cancel_pending")
        attempt = repo._locked(
            conn,
            select(attempts).where(
                attempts.c.id == expected_attempt_id, attempts.c.job_id == job_id
            ),
        )
        if (
            attempt is None
            or attempt["status"]
            not in {"submitting", "submission_unknown", "running", "collecting"}
            or attempt["number"] != job["attempt_no"]
        ):
            raise Conflict("capacity_reclaim_attempt_invalid")
        if conn.execute(
            select(artifacts.c.id).where(artifacts.c.job_id == job_id).limit(1)
        ).first():
            raise Conflict("capacity_reclaim_output_exists")
        now = repo.clock()
        started = attempt["submission_started_at"]
        if not finite(now) or not finite(started) or started > now:
            raise Conflict("capacity_reclaim_submission_unconfirmed")
        if reason == "capacity_no_progress":
            if not finite(last_progress_at) or not started <= last_progress_at <= now:
                raise Conflict("capacity_reclaim_progress_invalid")
            # A later positive engine observation committed before our row lock
            # invalidates the watchdog's snapshot, even if its lease is unchanged.
            progress = (job.get("result") or {}).get("capacity_progress")
            if (
                isinstance(progress, dict)
                and progress.get("attempt_id") == expected_attempt_id
                and finite(progress.get("at"))
                and progress["at"] > last_progress_at
            ):
                raise Conflict("capacity_reclaim_progress_changed")
            if now - last_progress_at < no_progress_seconds:
                raise Conflict("capacity_reclaim_not_due")
        elif reason == "capacity_runtime_exceeded":
            if not finite(job["expected_runtime_s"]) or now - started <= max(
                2 * job["expected_runtime_s"], runtime_floor_seconds
            ):
                raise Conflict("capacity_reclaim_not_due")
        elif last_progress_at is not None:
            raise ValueError("capacity_reclaim_progress_invalid")
        exhausted = count >= MAX_RECLAIMS
        record = dict(
            version=1,
            job_id=job_id,
            attempt_id=attempt["id"],
            number=attempt["number"],
            worker_id=attempt["worker_id"],
            attempt_fence=attempt["fence"],
            previous_fence=job["fence"],
            fence=job["fence"] + 1,
            reason=reason,
            evidence_id=evidence_id,
            at=now,
            last_progress_at=last_progress_at,
            state="exhausted" if exhausted else "requeued",
            count=count if exhausted else count + 1,
            event_id=str(uuid.uuid4()),
        )
        result = {
            **(job.get("result") or {}),
            "capacity_reclaim_count": record["count"],
            "capacity_reclaims": [*records, record],
        }
        result.pop("capacity_progress", None)
        error = "capacity_lost_retries_exhausted" if exhausted else reason
        conn.execute(
            update(attempts)
            .where(attempts.c.id == attempt["id"])
            .values(status="attempt_lost", error_code=reason, updated_at=now)
        )
        conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(
                status="failed" if exhausted else "queued",
                current_attempt_id=None,
                fence=record["fence"],
                lease_worker_id=None,
                lease_expires_at=None,
                not_before=now,
                error_code=error,
                result=result,
                updated_at=now,
            )
        )
        conn.execute(
            insert(outbox).values(
                id=record["event_id"],
                event_type="capacity.attempt_lost",
                aggregate_id=job_id,
                payload={
                    **record,
                    "tenant_id": job["tenant_id"],
                    "owner_id": job["owner_id"],
                    "project_id": job["project_id"],
                },
                created_at=now,
            )
        )
        if exhausted:
            from .credits import release

            release(conn, repo, repo._job(conn, job_id), reason=error)
        repo._emit(
            conn,
            "job.failed" if exhausted else "job.queued",
            job_id,
            {
                "job_id": job_id,
                "capacity_reclaim_count": record["count"],
                "reclaim_event_id": record["event_id"],
            },
        )
        return {**record, "replayed": False}
