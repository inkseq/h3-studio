"""Read-only operator usage from original rental, attempt and output receipts.

These values do not settle invoices, create telemetry storage, or infer provider
removal. Missing evidence remains null. The submit-to-validated-result fallback
includes collection and publication; it is never reported as GPU generate time.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select

from .repository import (artifacts, attempts, budget_reservations, jobs, outbox,
                         scaler_actions, scaler_receipts)


def _time(value, now):
    return (type(value) in (int, float) and math.isfinite(value)
            and 0 <= value <= now)


def _money(value):
    return type(value) is int and 0 <= value <= 9_000_000_000_000


def _day(now, timezone):
    try:
        zone = ZoneInfo(timezone)
        local = datetime.fromtimestamp(now, zone)
        start = datetime.combine(local.date(), time.min, zone).timestamp()
        end = datetime.combine(local.date() + timedelta(days=1), time.min, zone).timestamp()
        return start, end
    except (TypeError, ValueError, OverflowError, ZoneInfoNotFoundError):
        return None, None


def _first_stopped(connection, intent, binding, now):
    stopped = binding.get("first_stopped_at")
    if _time(stopped, now):
        return stopped
    # Historical terminal receipts qualify only when their exact run identity
    # matches. A stop request/journal or the latest mutable observation does not.
    statement = select(scaler_receipts.c.observed_at, scaler_receipts.c.facts).where(
        scaler_receipts.c.intent_id == intent["id"],
        or_(scaler_receipts.c.facts["state"].as_string().in_(
            ("stopped", "terminated", "destroyed")),
            scaler_receipts.c.facts["dstack_status"].as_string().in_(
                ("terminated", "done", "failed", "aborted"))))
    for row in connection.execute(statement.order_by(scaler_receipts.c.observed_at)).mappings():
        facts, observed = row["facts"], row["observed_at"]
        if (isinstance(facts, dict) and binding.get("run_id")
                and facts.get("run_id") == binding["run_id"]
                and ("provider_instance_id" not in facts or
                     facts["provider_instance_id"] == intent.get("provider_instance_id"))
                and _time(observed, now)):
            return observed
    return None


def _rental_period(connection, node, intent, binding, now):
    started = connection.execute(select(scaler_actions.c.create_started_at).where(
        scaler_actions.c.intent_id == intent["id"])).scalar_one_or_none()
    if not _time(started, now):
        started = binding.get("apply_started_at")
    if not _time(started, now):
        if binding.get("reason_code") == "dstack_never_applied":
            return now, now, "never_applied"
        return None, None, "unavailable"
    stopped = _first_stopped(connection, intent, binding, now)
    if stopped is not None:
        return ((started, stopped, "durable_rental_interval") if stopped >= started
                else (None, None, "unavailable"))
    ended = (node.get("runtime_state") == "stopped" or intent.get("state") == "destroyed"
             or binding.get("state") == "stopped")
    if ended:
        return None, None, "unavailable"
    return started, now, "elapsed_since_create_request"


def _total_cost(connection, intent, binding):
    values = list(connection.execute(select(budget_reservations.c.state,
        budget_reservations.c.actual_cost_microusd).where(
            budget_reservations.c.reference_type == "instance",
            budget_reservations.c.reference_id == intent["id"])).mappings())
    # Tenant, owner and project reservations represent the same original charge.
    # Do not add these layered caps. Conflicting/pending settlement is unknown.
    if (values and all(row["state"] in {"settled", "released"}
                       and _money(row["actual_cost_microusd"]) for row in values)
            and len({row["actual_cost_microusd"] for row in values}) == 1):
        return values[0]["actual_cost_microusd"], "settled"
    estimated = binding.get("estimated_cost_microusd")
    if _money(estimated):
        return estimated, "dstack_estimate"
    return None, "unavailable"


def _hourly_cost(payload, binding, intent):
    # An additive original provisioning quote is usable only with its exact
    # bound provider instance. Otherwise retain the approved ceiling label.
    quote = binding.get("hourly_cost_microusd")
    if (binding.get("price_basis") == "provisioning_quote" and _money(quote)
            and quote > 0 and intent.get("provider_instance_id")
            and binding.get("provider_instance_id") == intent["provider_instance_id"]):
        return quote, "provisioning_quote_estimate"
    ceiling = payload.get("hourly_cost_microusd")
    if _money(ceiling) and ceiling > 0:
        return ceiling, "approved_ceiling_estimate"
    return None, "unavailable"


def _videos(connection, payload, intent, workers, now, day_start, day_end):
    scope = (payload.get("tenant_id"), payload.get("owner_id"), payload.get("project_id"))
    if not all(isinstance(value, str) and value for value in scope):
        return None, None, [], None, []
    expected_worker_id = "dstack-" + intent["id"].replace("-", "")
    worker_ids = {worker.get("id") for worker in workers
        if intent.get("provider_instance_id")
        and worker.get("provider") == intent["provider"]
        and worker.get("instance_id") == intent["provider_instance_id"]
        and worker.get("id") == expected_worker_id}
    if not worker_ids:
        # An original never-admitted machine proves no video by this allocation.
        # An already-qualified allocation missing retained workers is unknown.
        return ((0, 0 if day_start is not None else None, [], None, [])
                if not payload.get("dstack", {}).get("first_runtime_ready_at")
                and not payload.get("dstack", {}).get("runtime_incarnation")
                else (None, None, [], None, []))
    statement = select(jobs.c.id, attempts.c.id.label("attempt_id"),
        jobs.c.request["deployment_profile_id"].as_string().label("profile_id"),
        func.coalesce(jobs.c.request["request"]["mode"].as_string(),
            jobs.c.request["mode"].as_string()).label("mode"),
        attempts.c.created_at.label("attempt_created_at"),
        attempts.c.submission_started_at).select_from(jobs.join(attempts,
            (attempts.c.id == jobs.c.current_attempt_id) & (attempts.c.job_id == jobs.c.id))
            .join(artifacts, (artifacts.c.job_id == jobs.c.id) & (artifacts.c.attempt_id == attempts.c.id))).where(
        jobs.c.tenant_id == scope[0], jobs.c.owner_id == scope[1], jobs.c.project_id == scope[2],
        jobs.c.status == "succeeded", attempts.c.status == "succeeded",
        attempts.c.worker_id.in_(worker_ids), artifacts.c.metadata["kind"].as_string() == "video",
        artifacts.c.metadata["validated"].as_boolean().is_(True)).distinct()
    successes = list(connection.execute(statement).mappings())
    if not successes:
        return 0, 0 if day_start is not None else None, [], None, []
    identities = {row["id"] for row in successes}
    completions = {}
    for row in connection.execute(select(outbox.c.aggregate_id, outbox.c.created_at).where(
            outbox.c.event_type == "job.succeeded", outbox.c.aggregate_id.in_(identities),
            outbox.c.payload["job_id"].as_string() == outbox.c.aggregate_id,
            outbox.c.created_at <= now).order_by(outbox.c.created_at)).mappings():
        completions.setdefault(row["aggregate_id"], []).append(row["created_at"])
    count, today_count, intervals, first, stage_attempts = len(successes), 0, [], None, []
    completion_unknown = False
    for row in successes:
        created = row["attempt_created_at"]
        if not _time(created, now):
            completion_unknown = True
            continue
        completed = next((value for value in completions.get(row["id"], ())
                          if _time(value, now) and value >= created), None)
        if completed is None:
            completion_unknown = True
            continue
        if day_start is not None and day_start <= completed < day_end:
            today_count += 1
        submitted = row["submission_started_at"]
        if _time(submitted, now) and created <= submitted <= completed:
            intervals.append(completed - submitted)
            first = completed if first is None else min(first, completed)
            binding = payload.get("dstack", {})
            if row["profile_id"] == binding.get("profile_id") and row["mode"] == binding.get("mode"):
                stage_attempts.append({"job_id": row["id"], "attempt_id": row["attempt_id"],
                    "submission_started_at": submitted, "completed_at": completed})
    return count, today_count if day_start is not None and not completion_unknown else None, intervals, first, stage_attempts


def machine_metrics(connection, node, intent, workers, now, timezone="Australia/Sydney", stage_reader=None):
    """SELECT-only list projection; optional bounded Cloud read for detail only."""
    usage = {"runtime_seconds": None, "runtime_basis": "unavailable",
        "total_cost_microusd": None, "cost_basis": "unavailable",
        "today_cost_microusd": None, "today_cost_basis": "unavailable",
        "video_count": None, "today_video_count": None, "timezone": timezone}
    metrics = {"average_generate_seconds": None, "average_generation_cost_microusd": None,
        "average_completion_seconds": None, "samples": 0, "basis": "unavailable",
        "generate_samples": 0, "generation_basis": "unavailable", "generation_cost_basis": "unavailable",
        "sample_window_start": None, "sample_window_end": None, "stage_observed_at": None,
        "stage_query_status": "not_requested"}
    payload = node.get("payload", {})
    binding = payload.get("dstack", {}) if isinstance(payload, dict) else {}
    if (not _time(now, now) or not isinstance(payload, dict) or not isinstance(binding, dict)
            or payload.get("capacity_backend") != "dstack-v1"
            or node.get("intent_id") != intent.get("id")):
        return {"usage": usage, "metrics": metrics}
    day_start, day_end = _day(now, timezone)
    started, ended, runtime_basis = _rental_period(connection, node, intent, binding, now)
    total, cost_basis = _total_cost(connection, intent, binding)
    usage.update(total_cost_microusd=total, cost_basis=cost_basis, runtime_basis=runtime_basis)
    if started is not None and ended is not None:
        usage["runtime_seconds"] = ended - started
        if day_start is not None:
            overlap = max(0, min(ended, now, day_end) - max(started, day_start))
            if runtime_basis == "never_applied":
                usage.update(today_cost_microusd=0, today_cost_basis="settled")
            elif (cost_basis == "settled" and runtime_basis == "durable_rental_interval"
                  and day_start <= started <= ended < day_end):
                usage.update(today_cost_microusd=total, today_cost_basis="settled")
            else:
                hourly, basis = _hourly_cost(payload, binding, intent)
                if hourly is not None:
                    usage.update(today_cost_microusd=math.ceil(hourly * overlap / 3600),
                                 today_cost_basis=basis)
    count, today_count, intervals, _, stage_attempts = _videos(connection, payload, intent, workers,
                                              now, day_start, day_end)
    usage.update(video_count=count, today_video_count=today_count)
    if intervals:
        metrics.update(average_completion_seconds=sum(intervals) / len(intervals),
                       samples=len(intervals), basis="ledger_submit_to_validated_result")
    if stage_reader is not None:
        try:
            stage = stage_reader.generate(node_id=intent["id"], provider=intent["provider"],
                profile_id=binding.get("profile_id"), mode=binding.get("mode"), attempts=stage_attempts, now=now)
            metrics.update({key: value for key, value in stage.items() if key in metrics})
        except Exception:
            metrics["stage_query_status"] = "query_unavailable"
        average = metrics.get("average_generate_seconds")
        hourly, basis = _hourly_cost(payload, binding, intent)
        if type(average) in (int, float) and math.isfinite(average) and average > 0 and hourly is not None:
            metrics.update(average_generation_cost_microusd=math.ceil(hourly * average/3600),
                generation_cost_basis=basis)
    return {"usage": usage, "metrics": metrics}
