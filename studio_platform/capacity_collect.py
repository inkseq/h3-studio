"""Result collection that cannot finish on its host: hold or reclaim.

A job in ``collecting`` waits for its own worker to fetch the output. When
that host is gone, or collection keeps failing, nothing else ever collects it:
staging job 27b6213f sat in collecting/collection_failed for hours after its
machine was destroyed. The watchdog uses this module to decide, from committed
rows only (no provider, storage or GPU IO):

- output already outside the host (the collection receipt holds verified
  objects in storage, or files staged on the collector): never regenerate;
  hold, and the ``capacity_collect_stuck`` alert makes it visible;
- otherwise the output lived only on the host: the attempt is lost, and the
  watchdog reclaims it through the audited ``capacity_reclaim`` path.
"""

from __future__ import annotations

import json
import math

from sqlalchemy import inspect, select

# A collect turn retries about every 30 s on a live host; ten failures in a
# row is a host that cannot deliver, not a transient storage error.
COLLECT_FAILURE_LIMIT = 10
# A failed collection that no worker retried for this long has no collector.
COLLECT_IDLE_SECONDS = 600
# The health alert for a collection that is held or keeps failing.
COLLECT_STUCK_SECONDS = 900


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _receipts(connection, job_id, attempt_id):
    from .artifact_writer import write_receipts

    if not inspect(connection).has_table(write_receipts.name):
        return []
    rows = connection.execute(
        select(write_receipts.c.record).where(
            write_receipts.c.job_id == job_id,
            write_receipts.c.attempt_id == attempt_id,
        )
    ).scalars()
    records = []
    for raw in rows:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            value = None
        # A receipt we cannot read is kept as possible output, never ignored.
        records.append(value if isinstance(value, dict) else {"phase": "unreadable"})
    return records


def output_stored(connection, job_id, attempt_id):
    """True when this attempt's output already exists outside its GPU host.

    GPU delivery (``gpu-r2-v1``): every expected role verified in storage by
    its sealed size and hash. A role still ``putting`` or ``unknown`` is not
    proof: its host is the only writer and may never have sent it. CPU
    delivery: any receipt past ``staging`` holds verified files on the
    collector itself. A settled receipt always counts.
    """
    for record in _receipts(connection, job_id, attempt_id):
        phase = record.get("phase")
        if phase == "staging":
            continue
        if phase == "settled":
            return True
        if record.get("collection_route") != "gpu-r2-v1":
            return True
        roles = record.get("roles")
        expected = record.get("expected_roles")
        if (
            isinstance(roles, dict)
            and isinstance(expected, list)
            and expected
            and set(roles) == set(expected)
            and all(
                isinstance(role, dict) and role.get("status") == "verified"
                for role in roles.values()
            )
        ):
            return True
    return False


def collecting(job, attempt):
    return (
        job["current_attempt_id"] == attempt["id"]
        and job["status"] == "collecting"
        and attempt["status"] == "collecting"
    )


def collection_due(job, attempt, now, *, removed, stored):
    """(reason, None) when a collecting attempt must be reclaimed, else None."""
    if not collecting(job, attempt):
        return None
    expires = job.get("lease_expires_at")
    if job.get("lease_worker_id") is not None and _finite(expires) and expires > now:
        return None  # A collect turn is running; its own outcome decides.
    if stored:
        return None  # Never regenerate an output that already exists.
    if removed:
        return "capacity_provider_removed", None
    failures = attempt.get("collection_failures") or 0
    if failures >= COLLECT_FAILURE_LIMIT:
        return "capacity_collection_failed", None
    updated = attempt.get("updated_at")
    if failures >= 1 and _finite(updated) and now - updated >= COLLECT_IDLE_SECONDS:
        return "capacity_collection_failed", None
    return None


def stuck_collections(connection, job_rows, attempt_rows, now):
    """Health findings: collecting with failures for COLLECT_STUCK_SECONDS.

    ``since`` is the job's collection start (its ``job.collecting`` event),
    a stable episode start; attempt.updated_at moves on every retry.
    """
    from .repository import outbox

    attempts_by_id = {row["id"]: row for row in attempt_rows}
    found = []
    for job in job_rows:
        attempt = attempts_by_id.get(job.get("current_attempt_id"))
        if (
            attempt is None
            or not collecting(job, attempt)
            or not (attempt.get("collection_failures") or 0)
        ):
            continue
        since = connection.execute(
            select(outbox.c.created_at)
            .where(
                outbox.c.event_type == "job.collecting",
                outbox.c.aggregate_id == job["id"],
            )
            .order_by(outbox.c.created_at.desc())
            .limit(1)
        ).scalar()
        if not _finite(since):
            # Outbox rows can be pruned; the submission is an earlier bound.
            since = attempt.get("submission_started_at")
        if not _finite(since) or now - since < COLLECT_STUCK_SECONDS:
            continue
        stored = output_stored(connection, job["id"], attempt["id"])
        found.append((job, attempt, since, stored))
    return found
