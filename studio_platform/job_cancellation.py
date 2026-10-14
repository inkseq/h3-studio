"""Closed original-job cancellation and retry proofs; no upstream calls."""
from __future__ import annotations

import math

from sqlalchemy import select

TERMINAL = {"succeeded", "failed", "cancelled"}
OPERATOR_REASONS = {"operator_queue_cleared", "operator_request_cancelled", "capacity_unavailable"}
MESSAGES = {
    "owner_request_cancelled": "任务所属账户请求取消。",
    "operator_queue_cleared": "管理员清理了等待队列。",
    "operator_request_cancelled": "管理员取消了此等待任务。",
    "capacity_unavailable": "所需执行容量不可用，此任务已停止等待。",
    "machine_failed": "执行机器失败，此任务已结束。",
    "generation_failed": "执行未能完成，请核对原任务的停止和费用状态。",
}


def metadata_for(kind, reason_code, actor_id, now):
    from .repository import canonical, identifier
    allowed = ({"owner_request_cancelled"} if kind == "owner" else OPERATOR_REASONS if kind == "operator"
               else {"capacity_unavailable", "machine_failed", "generation_failed"} if kind == "system" else set())
    if (not isinstance(reason_code, str) or reason_code not in allowed
            or type(now) not in (int, float) or not math.isfinite(now) or now < 0):
        raise ValueError("invalid_job_cancellation")
    if actor_id is not None:
        identifier(actor_id)
    if kind != "system" and actor_id is None:
        raise ValueError("invalid_job_cancellation_actor")
    return canonical({"version": 1, "cancelled_by": kind, "actor_id": actor_id,
        "reason_code": reason_code, "message": MESSAGES[reason_code], "requested_at": float(now)})


def validate_metadata(value):
    if (not isinstance(value, dict) or set(value) != {
            "version", "cancelled_by", "actor_id", "reason_code", "message", "requested_at"}
            or type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("invalid_job_cancellation")
    expected = metadata_for(value["cancelled_by"], value["reason_code"], value["actor_id"], value["requested_at"])
    if value != expected:
        raise ValueError("invalid_job_cancellation")
    return expected


def failure_metadata(error_code, now):
    reason = ("capacity_unavailable" if error_code in {
        "capacity_approval_expired_or_revoked", "capacity_wait_expired", "capacity_wait_deadline_expired",
        "capacity_unavailable"} else
        "machine_failed" if error_code in {"machine_failed", "worker_process_failed", "runtime_process_exited"}
        else "generation_failed")
    return metadata_for("system", reason, None, now)


def cancel_snapshot(job):
    from .repository import request_hash
    return request_hash({name: job[name] for name in ("id", "request_hash", "status", "fence",
        "current_attempt_id", "attempt_no", "updated_at", "lease_worker_id", "lease_expires_at")})


def attempt_history(connection, job):
    from .repository import attempts
    return list(connection.execute(select(attempts.c.id, attempts.c.number, attempts.c.status,
        attempts.c.fence, attempts.c.submission_started_at, attempts.c.upstream_task_id,
        attempts.c.upstream_stopped).where(attempts.c.job_id == job["id"])
        .order_by(attempts.c.number)).mappings())


def cancellation_columns():
    from .repository import jobs
    return (jobs.c.id, jobs.c.status, jobs.c.updated_at, jobs.c.error_code, jobs.c.cancellation,
        jobs.c.attempt_no, jobs.c.current_attempt_id, jobs.c.fence, jobs.c.lease_worker_id,
        jobs.c.lease_expires_at, jobs.c.result["actual_cost_microusd"].label("_actual_cost_microusd"))


def _history_identity(job, history):
    number = job["attempt_no"]
    if type(number) is not int or number < 0 or len(history) != number:
        return False
    if [row["number"] for row in history] != list(range(1, number + 1)):
        return False
    return (job["current_attempt_id"] is None if not history else
            job["current_attempt_id"] == history[-1]["id"])


def cancel_eligibility(connection, job):
    from .repository import registered_workers
    if job["status"] not in {"queued", "waiting_capacity"}:
        return {"cancellable": False, "reason_code": "job_not_waiting"}
    if job["lease_worker_id"] is not None or job["lease_expires_at"] is not None:
        return {"cancellable": False, "reason_code": "job_has_lease"}
    # A held worker binding remains a possible handoff even when the job lease
    # is absent. Read only: worker-before-job claim lock order stays unchanged.
    if connection.execute(select(registered_workers.c.id).where(
            registered_workers.c.current_job_id == job["id"]).limit(1)).first():
        return {"cancellable": False, "reason_code": "job_bound_to_worker"}
    history = attempt_history(connection, job)
    if not _history_identity(job, history) or any(
            row["submission_started_at"] is not None or row["upstream_task_id"] is not None
            or row["status"] not in {"lease_expired", "deferred", "cancelled", "failed"}
            for row in history) or history and job["fence"] <= history[-1]["fence"]:
        return {"cancellable": False, "reason_code": "submission_unconfirmed"}
    if job["status"] == "waiting_capacity" and history:
        return {"cancellable": False, "reason_code": "submission_unconfirmed"}
    return {"cancellable": True, "reason_code": None}


def retry_advice(connection, job, *, history=None, reservations=None):
    from .repository import budget_reservations
    history = attempt_history(connection, job) if history is None else history
    identity = _history_identity(job, history)
    unsubmitted = identity and all(row["submission_started_at"] is None
        and row["upstream_task_id"] is None for row in history)
    stopped = identity and (unsubmitted or all(row["upstream_stopped"] == 1 for row in history))
    no_lease = job["lease_worker_id"] is None and job["lease_expires_at"] is None
    reservations = list(connection.execute(select(budget_reservations.c.state,
        budget_reservations.c.actual_cost_microusd).where(budget_reservations.c.reference_type == "job",
        budget_reservations.c.reference_id == job["id"])).mappings()) if reservations is None else reservations
    known_costs = [row["actual_cost_microusd"] for row in reservations
        if row["state"] in {"settled", "released"} and type(row["actual_cost_microusd"]) is int
        and row["actual_cost_microusd"] >= 0]
    if any(cost > 0 for cost in known_costs):
        charged = True
    elif reservations and len(known_costs) == len(reservations):
        charged = False
    elif reservations:
        charged = None
    else:
        actual = job.get("_actual_cost_microusd")
        if actual is None and isinstance(job.get("result"), dict):
            actual = job["result"].get("actual_cost_microusd")
        charged = (actual > 0 if type(actual) is int and actual >= 0 else
                   False if not reservations and unsubmitted and no_lease
                   and job["status"] in {"failed", "cancelled"} else None)
    # Match the existing Quick Chat retry gate. Historical unsubmitted attempts
    # without explicit stop flags remain blocked; this view cannot loosen it.
    retry_stopped = identity and (not history or all(row["upstream_stopped"] == 1 for row in history))
    return {"safe_to_retry": bool(retry_stopped and no_lease and job["status"] in {"failed", "cancelled"}),
            "charged": charged, "upstream_still_running": False if stopped else None}


def cancellation_fields(connection, job, *, history=None, reservations=None):
    value = job.get("cancellation")
    try:
        value = validate_metadata(value) if value is not None else None
    except (ValueError, TypeError, KeyError):
        value = None
    return {"cancelled_by": value["cancelled_by"] if value else None,
            "reason_code": value["reason_code"] if value else None,
            "message": value["message"] if value else None,
            "retry_advice": retry_advice(connection, job, history=history, reservations=reservations)}
