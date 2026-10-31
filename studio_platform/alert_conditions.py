"""Pure notification transitions; stdlib only, no task/budget/provider authority.

Persist returned state only as monitor deduplication metadata. Unknown samples
never resolve an alarm. Delivery/ack persistence belongs to the host publisher.
"""
import copy
import math
import re
import uuid

DEFAULTS = {
    "gpu_max_seconds": 7200, "stuck_seconds": 1800, "wake_count": 10,
    "cpu_percent": 70, "memory_percent": 85, "host_sustain_seconds": 900,
    "health_down_seconds": 120, "http_5xx_min": 3, "http_5xx_ratio": .05,
    "max_sample_gap_seconds": 180, "terminal_reservation_seconds": 3600,
    "payment_fulfillment_seconds": 300, "payment_processing_seconds": 300,
    "payment_window_seconds": 300, "payment_failure_count": 1,
}
CODES = frozenset({"gpu_runtime_exceeded", "gpu_idle_stop_overdue", "test_spend_80",
    "test_spend_100", "job_stuck", "job_repeated_wake", "host_cpu_sustained",
    "host_memory_sustained", "host_disk_80", "host_disk_90", "host_process_unhealthy", "retention_service_failed",
    "business_backup_failed", "business_backup_missing", "business_backup_unobservable", "business_backup_loss",
    "health_down", "http_5xx_rate", "generation_failure", "terminal_reservation_stale",
    "payment_fulfillment_stalled", "payment_processing_stalled", "payment_failed",
    "payment_refunded", "payment_disputed", "payment_monitor_unavailable",
    "payment_webhook_signature_failed", "payment_webhook_receipt_failed"})
STATES = frozenset({"waiting_capacity", "queued", "claimed", "submitting", "running",
    "submission_unknown", "cancel_requested", "collecting", "recovery_hold", "starting", "upstream_status_unknown"})


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def identity(value):
    try:
        return value if isinstance(value, str) and str(uuid.UUID(value)) == value else None
    except (ValueError, TypeError, AttributeError):
        return None


def _configuration(overrides):
    if overrides is not None and (not isinstance(overrides, dict) or set(overrides) - set(DEFAULTS)):
        raise ValueError("alert_thresholds_invalid")
    config = {**DEFAULTS, **(overrides or {})}
    if any(not finite(v) or v <= 0 for v in config.values()):
        raise ValueError("alert_thresholds_invalid")
    if config["http_5xx_ratio"] > 1 or config["cpu_percent"] > 100 or config["memory_percent"] > 100:
        raise ValueError("alert_thresholds_invalid")
    if type(config["payment_window_seconds"]) is not int or not 60 <= config["payment_window_seconds"] <= 3600:
        raise ValueError("alert_thresholds_invalid")
    return config


def evaluate(snapshot, previous_state, *, environment, now, host=None, thresholds=None):
    """Return {state, notifications}; caller persists before attempting delivery."""
    if environment not in {"staging", "production"} or not finite(now) or now < 0:
        raise ValueError("alert_context_invalid")
    config = _configuration(thresholds)
    prior = previous_state or {}
    if prior and (not isinstance(prior, dict) or prior.get("version") != 1
                  or prior.get("environment") != environment):
        raise ValueError("alert_state_invalid")
    state = copy.deepcopy(prior) if prior else {"version": 1, "environment": environment,
        "conditions": {}, "seen_failure_kinds": [], "observed_jobs": {}}
    notifications, considered = [], set()
    origin = "https://staging.inkseq.com" if environment == "staging" else "https://inkseq.com"

    def message(code, resource, transition, since):
        assert code in CODES and (resource is None or identity(resource))
        return {"code": code, "environment": environment, "resource_id": resource,
            "duration_seconds": max(0, int(now-since)), "console_link": origin+"/freestyle",
            "transition": transition}

    def condition(code, resource, value, *, sustain=0, since=None, suffix=""):
        key = code+":"+(resource or "host")+suffix
        considered.add(key)
        current = state["conditions"].get(key, {"code": code, "resource_id": resource,
            "active": False, "since": None, "last_observed": None})
        if value is None:
            # Keep an active incident, but do not count missing time toward a
            # not-yet-fired sustained condition.
            if not current["active"]:
                current["since"] = None
            state["conditions"][key] = current
            return
        if value:
            gap = current["last_observed"] is None or not 0 <= now-current["last_observed"] <= config["max_sample_gap_seconds"]
            if current["since"] is None or gap and not current["active"]:
                current["since"] = since if finite(since) and 0 <= since <= now else now
            if not current["active"] and now-current["since"] >= sustain:
                current["active"] = True
                notifications.append(message(code, resource, "firing", current["since"]))
        else:
            if current["active"]:
                notifications.append(message(code, resource, "resolved", current["since"] if current["since"] is not None else now))
            current.update(active=False, since=None)
        current["last_observed"] = now
        state["conditions"][key] = current

    complete = (isinstance(snapshot, dict) and snapshot.get("version") == 1 and snapshot.get("complete") is True
        and all(isinstance(snapshot.get(name), list) for name in ("gpus", "jobs", "failures"))
        and "test_spend" in snapshot
        and finite(snapshot.get("observed_at")) and 0 <= now-snapshot["observed_at"] <= config["max_sample_gap_seconds"])
    db_codes = {"gpu_runtime_exceeded", "gpu_idle_stop_overdue", "job_stuck", "job_repeated_wake"}
    if complete:
        for gpu in snapshot.get("gpus", []):
            resource = identity(gpu.get("id"))
            if resource is None:
                continue
            started, idle = gpu.get("running_since"), gpu.get("idle_since")
            limit = gpu.get("idle_limit_seconds")
            condition("gpu_runtime_exceeded", resource,
                now-started > config["gpu_max_seconds"] if finite(started) and 0 <= started <= now else None,
                since=started)
            condition("gpu_idle_stop_overdue", resource,
                False if gpu.get("has_obligations") is True else
                now-idle > limit if gpu.get("has_obligations") is False and finite(idle)
                    and 0 <= idle <= now and finite(limit) and limit > 0 else None, since=idle)
        terminal_holds = snapshot.get("terminal_reservations")
        if isinstance(terminal_holds, list):
            db_codes.add("terminal_reservation_stale")
            for hold in terminal_holds:
                resource, since = identity(hold.get("id")), hold.get("terminal_since")
                if resource is not None:
                    if finite(since) and 0 <= since <= now:
                        condition("terminal_reservation_stale", resource, now-since > config["terminal_reservation_seconds"], since=since)
                    else:
                        condition("terminal_reservation_stale", resource, True, sustain=config["terminal_reservation_seconds"])
        observed = {}
        for job in snapshot.get("jobs", []):
            resource, status = identity(job.get("id")), job.get("status")
            if resource is None or status not in STATES:
                continue
            previous = state["observed_jobs"].get(resource, {})
            first = job.get("state_since")
            if not finite(first) or not 0 <= first <= now:
                consecutive = (state.get("database_gap") is not True and finite(state.get("database_observed_at"))
                    and 0 <= now-state["database_observed_at"] <= config["max_sample_gap_seconds"])
                first = previous.get("since") if previous.get("status") == status and consecutive else now
                if not finite(first) or not 0 <= first <= now:
                    first = now
            observed[resource] = {"status": status, "since": first}
            # A changed state starts a new episode even if both states are old.
            old_status = previous.get("status")
            if old_status is not None and old_status != status:
                condition("job_stuck", resource, False)
            condition("job_stuck", resource, now-first > config["stuck_seconds"], since=first)
            count = job.get("wake_count")
            condition("job_repeated_wake", resource,
                count > config["wake_count"] if type(count) is int and count >= 0 else None, since=first)
        state["observed_jobs"] = observed
        spend = snapshot.get("test_spend")
        if isinstance(spend, dict) and spend.get("scope") == "acceptance_task" and isinstance(spend.get("tasks"), list):
            # A scoped snapshot supersedes only the retired global warning;
            # each acceptance task keeps its own stable notification identity.
            for percent in (80, 100):
                condition("test_spend_"+str(percent), None, False)
            for task in spend["tasks"]:
                resource = identity(task.get("resource_id")) if isinstance(task, dict) else None
                if resource is None:
                    continue
                amount, limit = task.get("projected_microusd"), task.get("limit_microusd")
                for percent in (80, 100):
                    condition("test_spend_"+str(percent), resource,
                        amount*100 >= limit*percent if type(amount) is int and amount >= 0
                            and type(limit) is int and limit > 0 else None)
        elif isinstance(spend, dict):
            amount, limit = spend.get("projected_microusd"), spend.get("limit_microusd")
            for percent in (80, 100):
                condition("test_spend_"+str(percent), None,
                    amount*100 >= limit*percent if type(amount) is int and amount >= 0
                        and type(limit) is int and limit > 0 else None)
        else:
            for percent in (80, 100):
                condition("test_spend_"+str(percent), None, None)
        seen = set(state["seen_failure_kinds"])
        for failure in snapshot.get("failures", []):
            kind, resource = failure.get("kind"), identity(failure.get("job_id"))
            if (not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,159}", kind)
                    or resource is None or kind in seen):
                continue
            seen.add(kind)
            at = failure.get("at")
            notifications.append(message("generation_failure", resource, "firing",
                at if finite(at) and 0 <= at <= now else now))
        state["seen_failure_kinds"] = sorted(seen)
        # Only a complete authoritative snapshot may establish disappearance.
        for key, current in list(state["conditions"].items()):
            if key not in considered and current["code"] in db_codes:
                condition(current["code"], current["resource_id"], False)
                del state["conditions"][key]
        state["database_observed_at"] = now
        state["database_gap"] = False
    else:
        state["database_gap"] = True
    payment = snapshot.get("payments") if isinstance(snapshot, dict) else None
    payment_complete = (complete and isinstance(payment, dict) and payment.get("complete") is True
        and isinstance(payment.get("fulfillment"), list) and isinstance(payment.get("processing"), list)
        and isinstance(payment.get("activity"), dict))
    condition("payment_monitor_unavailable", None,
        not payment_complete if complete and isinstance(payment, dict) else None)
    if payment_complete:
        payment_codes = {"payment_fulfillment_stalled", "payment_processing_stalled"}
        for name, code, threshold in (("fulfillment", "payment_fulfillment_stalled", "payment_fulfillment_seconds"),
                ("processing", "payment_processing_stalled", "payment_processing_seconds")):
            for item in payment[name]:
                resource, since = identity(item.get("id")), item.get("since")
                if resource is not None:
                    condition(code, resource,
                        now-since >= config[threshold] if finite(since) and 0 <= since <= now else None,
                        since=since)
        for code in ("payment_failed", "payment_refunded", "payment_disputed"):
            activity = payment["activity"].get(code, {})
            count, since = activity.get("count"), activity.get("since")
            condition(code, None, count >= config["payment_failure_count"]
                if type(count) is int and count >= 0 else None, since=since)
        # Only a complete payment read may resolve disappeared purchase incidents.
        for key, current in list(state["conditions"].items()):
            if key not in considered and current["code"] in payment_codes:
                condition(current["code"], current["resource_id"], False)
                del state["conditions"][key]
    host = host if isinstance(host, dict) else {}
    for field, code in (("payment_signature_failures", "payment_webhook_signature_failed"),
            ("payment_receipt_failures", "payment_webhook_receipt_failed")):
        count = host.get(field)
        condition(code, None, count >= config["payment_failure_count"]
            if type(count) is int and count >= 0 else None)
    retention_failed = host.get("retention_service_failed")
    condition("retention_service_failed", None, retention_failed if type(retention_failed) is bool else None)
    for code in ("business_backup_failed", "business_backup_missing", "business_backup_unobservable", "business_backup_loss"):
        value = host.get(code)
        condition(code, None, value if type(value) is bool else None)
    disk = host.get("disk_used_percent")
    for percent in (80, 90):
        condition("host_disk_" + str(percent), None,
            disk >= percent if finite(disk) and 0 <= disk <= 100 else None)
    for field, code, threshold in (("cpu_percent", "host_cpu_sustained", "cpu_percent"),
                                  ("memory_percent", "host_memory_sustained", "memory_percent")):
        value = host.get(field)
        condition(code, None, value > config[threshold] if finite(value) and 0 <= value <= 100 else None,
            sustain=config["host_sustain_seconds"])
    for field, code, duration in (("health_ok", "health_down", "health_down_seconds"),
                                  ("processes_ok", "host_process_unhealthy", "host_sustain_seconds")):
        value = host.get(field)
        condition(code, None, not value if type(value) is bool else None, sustain=config[duration])
    total, failed = host.get("http_requests"), host.get("http_5xx")
    condition("http_5xx_rate", None,
        failed >= config["http_5xx_min"] and total > 0 and failed/total >= config["http_5xx_ratio"]
        if type(total) is int and type(failed) is int and 0 <= failed <= total else None)
    state["observed_at"] = now
    return {"state": state, "notifications": notifications}
