"""Original-ledger timeouts. CPU lease renewal is never engine progress.

No provider calls occur here. Fencing/requeue commits before the owned node is
drained; physical removal remains a separate billing reconciliation fact.
"""

from __future__ import annotations

import copy
import math

from sqlalchemy import select, update

from .operator_capacity import advance_command_revision, operator_nodes
from .repository import (
    Conflict,
    InvalidTransition,
    Scope,
    attempts,
    jobs,
    outbox,
    registered_workers,
    request_hash,
)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _boot_failed(binding):
    """The one-shot launch ended in a failure with its processes stopped."""
    preparation = binding.get("preparation")
    return (
        isinstance(preparation, dict)
        and preparation.get("phase") in {"setup_failed", "failed"}
        and binding.get("first_runtime_ready_at") is None
    )


def record_engine_progress(repo, queue, lease):
    """Only call after a positively validated running engine receipt."""
    with repo.transaction() as connection:
        job = queue._leased(connection, lease)
        value = copy.deepcopy(job.get("result") or {})
        value["capacity_progress"] = {
            "attempt_id": lease.attempt_id,
            "at": repo.clock(),
            "kind": "engine_running",
        }
        connection.execute(
            update(jobs).where(jobs.c.id == job["id"]).values(result=value)
        )


def timeout_reason(
    job,
    attempt,
    now,
    *,
    no_progress_seconds=600,
    runtime_floor_seconds=1800,
    runtime_ceiling_seconds=10800,
):
    started = attempt.get("submission_started_at")
    if not _number(started) or started > now:
        return None
    expected = job.get("expected_runtime_s")
    expected = expected if _number(expected) else 0
    progress = (job.get("result") or {}).get("capacity_progress", {})
    last = started
    if (
        isinstance(progress, dict)
        and progress.get("attempt_id") == attempt["id"]
        and progress.get("kind") == "engine_running"
        and _number(progress.get("at"))
        and started <= progress["at"] <= now
    ):
        last = progress["at"]
    limit = max(2 * expected, runtime_floor_seconds)
    if last > started and now - last < no_progress_seconds:
        # A long shot that keeps reporting engine progress is working, not lost: a
        # 15 s 1344x768 REF take ran past 60 min at full GPU (#193, 2026-10-25).
        # It still stops at the hard ceiling; silence is caught below.
        limit = max(limit, runtime_ceiling_seconds)
    if now - started > limit:
        return "capacity_runtime_exceeded", last
    if now - last >= no_progress_seconds:
        return "capacity_no_progress", last
    return None


def reclaimed_attempt(job, attempt):
    """Fenced lost work, never proof of upstream stop or a zero invoice."""
    from .capacity_reclaim import authorized_lost_attempt

    return authorized_lost_attempt(job, attempt)


def node_workers(connection, intent):
    identity = "dstack-" + intent["id"].replace("-", "")
    rows = list(
        connection.execute(
            select(registered_workers).where(
                (registered_workers.c.id == identity)
                | (
                    (registered_workers.c.provider == intent["provider"])
                    & (
                        registered_workers.c.instance_id
                        == intent.get("provider_instance_id")
                    )
                )
            )
        ).mappings()
    )
    return identity, rows


def fenced_node_stop(connection, node, intent, *, now=None):
    """Exact recovery command plus absence of any unfenced execution obligation."""
    marker = node["payload"].get("capacity_watchdog")
    if (
        not isinstance(marker, dict)
        or type(marker.get("version")) is not int
        or marker["version"] != 1
    ):
        return False
    if type(marker.get("reason")) is not str or marker["reason"] not in {
        "capacity_no_progress",
        "capacity_runtime_exceeded",
        "capacity_owner_recovery",
        "capacity_startup_timeout",
        "capacity_startup_failed",
        "capacity_provider_removed",
        "capacity_dispatch_unavailable",
    }:
        return False
    # New decisions use the current clock. A committed desired=stopped command
    # remains authoritative for its original one-shot/retry journal afterwards.
    decision_at = (
        now
        if now is not None
        else marker.get("at")
        if node["desired_state"] == "stopped"
        else None
    )
    identity, workers = node_workers(connection, intent)
    ids = [identity, *[worker["id"] for worker in workers]]
    related = list(
        connection.execute(
            select(attempts).where(attempts.c.worker_id.in_(ids))
        ).mappings()
    )
    removed = marker["reason"] == "capacity_provider_removed" and provider_removed(
        node["payload"].get("dstack", {}), intent, decision_at
    )
    authorized_events = set()
    authorized_worker_jobs = set()
    for attempt in related:
        job = (
            connection.execute(select(jobs).where(jobs.c.id == attempt["job_id"]))
            .mappings()
            .one()
        )
        if reclaimed_attempt(job, attempt):
            authorized_events.update(
                item["event_id"]
                for item in job["result"]["capacity_reclaims"]
                if item["attempt_id"] == attempt["id"]
            )
            authorized_worker_jobs.add((attempt["worker_id"], attempt["job_id"]))
            continue
        if removed and job["status"] in {"succeeded", "failed", "cancelled"}:
            continue  # Physical absence is execution evidence, never invoice settlement.
        if (
            attempt["submission_started_at"] is not None
            or attempt["upstream_task_id"] is not None
        ) and attempt["upstream_stopped"] != 1:
            return False
        if job["lease_worker_id"] in ids and job["status"] not in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            return False
    for worker in workers:
        if (
            worker["current_job_id"]
            and (worker["id"], worker["current_job_id"]) not in authorized_worker_jobs
        ):
            current = (
                connection.execute(
                    select(jobs).where(jobs.c.id == worker["current_job_id"])
                )
                .mappings()
                .first()
            )
            if current is None or current["status"] not in {
                "succeeded",
                "failed",
                "cancelled",
            }:
                return False
    # A startup timeout cannot launder a previously admitted runtime.
    if marker["reason"] == "capacity_provider_removed":
        if not removed:
            return False
    elif marker["reason"] == "capacity_dispatch_unavailable":
        from .capacity_health import HOST_FAULTS
        from .worker_admission import matching_dstack_queued_jobs

        health = (
            node["payload"]
            .get("capacity_health", {})
            .get("capacity_paid_idle_queue", {})
        )
        binding = node["payload"].get("dstack", {})
        observed = binding.get("observed_at")
        if (
            health.get("event_id") != marker.get("health_event_id")
            or health.get("diagnosis") != "host_runtime"
            or not _number(health.get("since"))
            or not _number(decision_at)
            or decision_at - health["since"] <= 600
            or binding.get("reason_code") not in HOST_FAULTS
            or not _number(observed)
            or not 0 <= decision_at - observed <= 120
            or node["desired_state"] == "running"
            and not matching_dstack_queued_jobs(connection, intent, node)
        ):
            return False
    elif marker["reason"] == "capacity_startup_failed":
        # Decided on a failed one-shot launch; a replay sees the phase move on
        # to stopping, so it re-checks only facts that stay true.
        binding = node["payload"].get("dstack", {})
        if (
            related
            or workers
            or binding.get("first_runtime_ready_at") is not None
            or binding.get("apply_started") is not True
        ):
            return False
    elif marker["reason"] == "capacity_startup_timeout":
        binding = node["payload"].get("dstack", {})
        seconds = marker.get("timeout_seconds")
        started = binding.get("apply_started_at")
        if (
            related
            or workers
            or binding.get("first_runtime_ready_at") is not None
            or binding.get("apply_started") is not True
            or not _number(started)
            or not _number(marker.get("at"))
            or type(seconds) is not int
            or not 1 <= seconds <= 86400
            or marker["at"] - started < seconds
        ):
            return False
    else:
        events = marker.get("event_ids")
        if (
            not related
            or not isinstance(events, list)
            or not events
            or any(
                not isinstance(item, str) or item not in authorized_events
                for item in events
            )
        ):
            return False
    return not connection.execute(
        select(jobs.c.id)
        .where(
            jobs.c.lease_worker_id.in_(ids),
            jobs.c.status.not_in({"succeeded", "failed", "cancelled"}),
        )
        .limit(1)
    ).first()


def _stop(
    repo,
    node,
    intent,
    reason,
    receipts,
    now,
    *,
    startup_timeout_seconds=1200,
    health_event_id=None,
):
    with repo.transaction() as connection:
        repo._lock_capacity(connection)
        current = repo._locked(
            connection,
            select(operator_nodes).where(operator_nodes.c.intent_id == intent["id"]),
        )
        from .control import WorkerControl

        control = WorkerControl(repo)
        identity, found = node_workers(connection, intent)
        # WorkerControl.claim serializes on these exact rows. Hold them before
        # inspecting job/attempt obligations, then retire on this connection.
        workers = [
            control._worker(connection, worker_id, lock=True)
            for worker_id in sorted(worker["id"] for worker in found)
        ]
        payload = copy.deepcopy(current["payload"])
        marker = payload.get("capacity_watchdog")
        if not isinstance(marker, dict):
            marker = {
                "version": 1,
                "reason": reason,
                "at": now,
                "event_ids": [item["event_id"] for item in receipts],
            }
            if reason == "capacity_startup_timeout":
                marker["timeout_seconds"] = startup_timeout_seconds
            elif reason == "capacity_dispatch_unavailable":
                marker["health_event_id"] = health_event_id
            payload["capacity_watchdog"] = marker
        candidate = dict(current, payload=payload)
        if not fenced_node_stop(connection, candidate, intent, now=now):
            return False
        if current["desired_state"] != "stopped":
            payload["stop_reason_code"] = reason
            payload = advance_command_revision(payload)
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == intent["id"])
                .values(
                    desired_state="stopped",
                    runtime_state="draining",
                    payload=payload,
                    updated_at=now,
                )
            )
            repo._emit(
                connection,
                "capacity.node_replacement_requested",
                intent["id"],
                {
                    "intent_id": intent["id"],
                    "reason_code": reason,
                    "event_ids": marker["event_ids"],
                },
            )
        elif current["payload"].get("capacity_watchdog") != marker:
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == intent["id"])
                .values(payload=payload)
            )
        # Retirement is an execution fence, explicitly NOT native idle proof.
        for worker in workers:
            control._retire_locked(connection, worker, release_retired=True)
            if worker["state"] != "retired":
                connection.execute(
                    update(registered_workers)
                    .where(registered_workers.c.id == worker["id"])
                    .values(drain_requested=1, expires_at=now, updated_at=now)
                )
        return True


def stop_unserved_node(service, node_id):
    """Only an audited fresh host-fault finding, never a cap/config refusal."""
    from .repository import instance_intents

    with service.repo.engine.connect() as connection:
        node = dict(
            connection.execute(
                select(operator_nodes).where(operator_nodes.c.intent_id == node_id)
            )
            .mappings()
            .one()
        )
        intent = dict(
            connection.execute(
                select(instance_intents).where(instance_intents.c.id == node_id)
            )
            .mappings()
            .one()
        )
    health = (
        node["payload"].get("capacity_health", {}).get("capacity_paid_idle_queue", {})
    )
    return _stop(
        service.repo,
        node,
        intent,
        "capacity_dispatch_unavailable",
        [],
        service.repo.clock(),
        health_event_id=health.get("event_id"),
    )


def provider_removed(binding, intent, now):
    """Original writer already validated account-bound complete absence."""
    from .dstack_operator import PROVIDERS

    if not _number(now):
        return False
    proof = binding.get("provider_removal_proof")
    return (
        isinstance(proof, dict)
        and set(proof)
        == {
            "version",
            "backend",
            "run_id",
            "provider_instance_id",
            "observed_at",
            "inventory_observed_at",
            "complete",
            "absent",
            "inventory_sha256",
        }
        and type(proof.get("version")) is int
        and proof["version"] == 1
        and proof.get("complete") is True
        and proof.get("absent") is True
        and proof.get("backend") == binding.get("backend")
        and PROVIDERS.get(binding.get("backend")) == intent.get("provider")
        and proof.get("run_id") == binding.get("run_id")
        and bool(binding.get("run_id"))
        and proof.get("provider_instance_id") == intent.get("provider_instance_id")
        and bool(intent.get("provider_instance_id"))
        and binding.get("provider_instance_id") == intent.get("provider_instance_id")
        and binding.get("state") == "stopped"
        and binding.get("dstack_status") in {"done", "failed", "terminated", "aborted"}
        and _number(proof.get("observed_at"))
        and 0 <= now - proof["observed_at"] <= 30
        and _number(proof.get("inventory_observed_at"))
        and 0 <= now - proof["inventory_observed_at"] <= 30
        and 0 <= proof["observed_at"] - proof["inventory_observed_at"] <= 30
        and isinstance(proof.get("inventory_sha256"), str)
        and len(proof["inventory_sha256"]) == 64
        and all(letter in "0123456789abcdef" for letter in proof["inventory_sha256"])
    )


def report_reclaims(service, node_id, receipts, telemetry, reporter=None):
    """Retry original alerts until enabled exporters accept them; never tasks."""
    repo = service.repo
    with repo.engine.connect() as connection:
        node = (
            connection.execute(
                select(operator_nodes).where(operator_nodes.c.intent_id == node_id)
            )
            .mappings()
            .one()
        )
        marker = node["payload"].get("capacity_watchdog")
        if not isinstance(marker, dict):
            return
        reported = marker.get("reported_event_ids", [])
        identities = marker.get("event_ids", [])
        if receipts is None:
            receipts = [
                row["payload"]
                for row in connection.execute(
                    select(outbox).where(
                        outbox.c.id.in_(identities),
                        outbox.c.event_type == "capacity.attempt_lost",
                    )
                ).mappings()
            ]
        pending = [
            receipt
            for receipt in receipts
            if receipt["event_id"] in identities and receipt["event_id"] not in reported
        ]
    accepted = []
    for receipt in pending:
        try:
            grafana_ok = True
            if telemetry.enabled:
                duration = telemetry.start(
                    "generate",
                    {
                        "job_id": receipt["job_id"],
                        "attempt_id": receipt["attempt_id"],
                        "node_id": node_id,
                    },
                ).finish("failure", error_code=receipt["reason"], duration_seconds=0)
                grafana_ok = (
                    duration is not None and telemetry.force_flush(1000) is not False
                )
            sentry_ok = reporter is None or reporter(receipt) is not None
            if grafana_ok and sentry_ok:
                accepted.append(receipt["event_id"])
        except Exception:
            pass  # Original outbox identity remains pending for the next tick.
    if accepted:
        with repo.transaction() as connection:
            repo._lock_capacity(connection)
            node = repo._locked(
                connection,
                select(operator_nodes).where(operator_nodes.c.intent_id == node_id),
            )
            payload = copy.deepcopy(node["payload"])
            marker = payload.get("capacity_watchdog")
            if not isinstance(marker, dict):
                return
            reported = marker.setdefault("reported_event_ids", [])
            reported.extend(
                identity for identity in accepted if identity not in reported
            )
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(payload=payload)
            )


def reclaim_node(service, node, intent):
    """Inspect only original attempts; replay receipts after a crash to finish drain."""
    from .queue import TaskQueue

    repo, settings = service.repo, service.settings
    now = repo.clock()
    silence = getattr(settings, "capacity_no_progress_seconds", 600)
    floor = getattr(settings, "capacity_runtime_floor_seconds", 1800)
    startup = getattr(settings, "capacity_startup_timeout_seconds", 1200)
    with repo.engine.connect() as connection:
        identity, workers = node_workers(connection, intent)
        ids = [identity, *[worker["id"] for worker in workers]]
        related = [
            dict(row)
            for row in connection.execute(
                select(attempts).where(attempts.c.worker_id.in_(ids))
            ).mappings()
        ]
        originals = {
            row["job_id"]: dict(
                connection.execute(select(jobs).where(jobs.c.id == row["job_id"]))
                .mappings()
                .one()
            )
            for row in related
        }
    receipts = []
    queue = TaskQueue(repo)
    binding = node["payload"].get("dstack", {})
    removed = provider_removed(binding, intent, now)
    for attempt in related:
        job = originals[attempt["job_id"]]
        if reclaimed_attempt(job, attempt):
            receipts.extend(
                item
                for item in job["result"]["capacity_reclaims"]
                if item.get("attempt_id") == attempt["id"]
            )
            continue
        if job["current_attempt_id"] != attempt["id"] or job["status"] not in {
            "submitting",
            "running",
            "submission_unknown",
        }:
            continue
        due = (
            ("capacity_provider_removed", None)
            if removed
            else timeout_reason(
                job,
                attempt,
                now,
                no_progress_seconds=silence,
                runtime_floor_seconds=floor,
            )
        )
        if due:
            reason, last = due
            evidence = request_hash(
                {"attempt": attempt["id"], "reason": reason, "last_progress_at": last}
            )
            try:
                receipts.append(
                    queue.reclaim_capacity(
                        Scope(
                            job["tenant_id"],
                            job["owner_id"],
                            job["project_id"],
                            job["actor_id"],
                        ),
                        job["id"],
                        expected_attempt_id=attempt["id"],
                        expected_fence=job["fence"],
                        reason=reason,
                        evidence_id=evidence,
                        last_progress_at=last,
                        no_progress_seconds=silence,
                        runtime_floor_seconds=floor,
                    )
                )
            except (Conflict, InvalidTransition) as error:
                if error.args != () and error.args[0] in {
                    "capacity_reclaim_conflict",
                    "capacity_reclaim_progress_changed",
                    "capacity_reclaim_not_due",
                    "capacity_reclaim_cancel_pending",
                    "capacity_reclaim_output_exists",
                    "capacity_reclaim_phase_invalid",
                }:
                    continue  # A live transition won the original row lock.
                raise
    started = binding.get("apply_started_at")
    cold_due = (
        binding.get("apply_started") is True
        and _number(started)
        and now - started >= startup
        and binding.get("first_runtime_ready_at") is None
        and not workers
        and not related
        and node["desired_state"] == "running"
    )
    boot_failed = (
        _boot_failed(binding)
        and not workers
        and not related
        and node["desired_state"] == "running"
    )
    if receipts or cold_due or removed or boot_failed:
        reason = (
            receipts[-1]["reason"]
            if receipts
            else "capacity_provider_removed"
            if removed
            else "capacity_startup_failed"
            if boot_failed
            else "capacity_startup_timeout"
        )
        stopped = _stop(
            repo, node, intent, reason, receipts, now, startup_timeout_seconds=startup
        )
        return {"stop_requested": stopped, "reason": reason, "receipts": receipts}
    return {"stop_requested": False, "receipts": []}
