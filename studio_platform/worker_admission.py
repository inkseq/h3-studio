"""Read-only eligibility shared by readiness, claim and the late submit guard.

These gates apply only to new inference. Existing attempts retain their original
worker binding and can reconcile or collect after the admission window closes.
"""

import math

from .budget_policy import effective_budget_limit

from sqlalchemy import inspect, select

from .repository import (
    attempts,
    budget_accounts,
    budget_reservations,
    instance_intents,
    jobs,
    registered_workers,
    scaler_actions,
)

STOP_NEW_SECONDS = 300
COMPLETION_MARGIN_SECONDS = 120
CONSUMER_HEARTBEAT_SECONDS = 60
SHUTDOWN_MARGIN_SECONDS = 300


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def dstack_policy_reason(
    intent, node, now, *, last_business_activity=None, has_obligations=False
):
    """Validate the explicit new lifecycle; idle expiry is not a job deadline.

    Historical finite machines never acquire this policy. Active/unknown/result
    obligations pause the idle timer, while a manual stop always blocks new work.
    """
    payload = node.get("payload", {})
    lifecycle = payload.get("lifecycle")
    request, original = payload.get("request"), payload.get("dstack")
    if (
        payload.get("capacity_backend") != "dstack-v1"
        or not isinstance(lifecycle, dict)
        or type(lifecycle.get("version")) is not int
        or lifecycle["version"] != 2
        or intent.get("hard_deadline") is not None
        or payload.get("hard_deadline") is not None
        or not isinstance(request, dict)
        or type(request.get("lifecycle_version")) is not int
        or request["lifecycle_version"] != 2
        or request.get("hard_deadline") is not None
        or request.get("max_duration_s") is not None
        or not isinstance(original, dict)
        or type(original.get("lifecycle_version")) is not int
        or original["lifecycle_version"] != 2
    ):
        return "managed_dstack_lifecycle_unconfirmed"
    keep_on, hold = lifecycle.get("keep_on"), lifecycle.get("hold_until")
    idle = lifecycle.get("idle_shutdown_seconds")
    accounts = lifecycle.get("budget_account_ids")
    if (
        not isinstance(keep_on, str)
        or keep_on not in {"idle", "until", "until_stopped"}
        or not _finite(hold)
        or hold < 0
        or keep_on == "until"
        and hold <= 0
        or keep_on != "until"
        and hold != 0
        or type(idle) is not int
        or not 1 <= idle <= 86400
        or type(lifecycle.get("reservation_seconds")) is not int
        or not 120 <= lifecycle["reservation_seconds"] <= 86400
        or not isinstance(accounts, list)
        or not accounts
        or any(not isinstance(account, str) or not account for account in accounts)
        or len(accounts) != len(set(accounts))
        or not _finite(now)
    ):
        return "managed_dstack_lifecycle_unconfirmed"
    if node.get("desired_state") != "running" or intent.get("state") not in {
        "reserved",
        "creating",
        "creation_unknown",
        "starting",
        "ready",
        "busy",
    }:
        return "worker_capacity_stopping"
    from .dstack_test_budget import runtime_reason

    reason = runtime_reason(intent, node, now)
    if reason:
        return reason
    if keep_on == "until_stopped" or has_obligations or last_business_activity is None:
        return None
    if not _finite(last_business_activity) or last_business_activity > now:
        return "managed_dstack_lifecycle_unconfirmed"
    last = (
        max(last_business_activity, hold)
        if keep_on == "until"
        else last_business_activity
    )
    if now >= last + idle:
        return "worker_idle_expired"
    return None


def dstack_budget_reason(connection, intent, node, now, *, expected_runtime_s=None):
    """Read the funded runway of this instance, without settling supplier costs.

    The controller funds rolling chunks in the existing reservation rows. This
    conservative max-price estimate is an admission bound, never an invoice or
    an immutable lifetime deadline. The same helper is used before claiming and
    immediately before submission, including time spent preparing the machine.
    """
    reason = dstack_policy_reason(intent, node, now)
    if reason:
        return reason
    payload, lifecycle = node["payload"], node["payload"]["lifecycle"]
    hourly, extra = (
        payload.get("hourly_cost_microusd"),
        payload.get("extra_reservation_microusd", 0),
    )
    amount = intent.get("reserved_cost_microusd")
    if (
        type(hourly) is not int
        or hourly <= 0
        or type(extra) is not int
        or extra < 0
        or type(amount) is not int
        or amount <= extra
    ):
        return "managed_dstack_budget_unconfirmed"
    duration = 0 if expected_runtime_s is None else expected_runtime_s
    if (
        not _finite(duration)
        or duration < 0
        or expected_runtime_s is not None
        and duration <= 0
    ):
        return "job_runtime_unconfirmed"
    from .dstack_test_budget import budget_reason

    reason = budget_reason(connection, intent, node, now, expected_runtime_s=duration)
    if reason:
        return reason
    rows = list(
        connection.execute(
            select(budget_reservations).where(
                budget_reservations.c.reference_type == "instance",
                budget_reservations.c.reference_id == intent["id"],
            )
        ).mappings()
    )
    account_ids = lifecycle["budget_account_ids"]
    if (
        len(rows) != len(account_ids)
        or {row["account_id"] for row in rows} != set(account_ids)
        or any(
            row["state"] != "reserved" or row["amount_microusd"] != amount
            for row in rows
        )
    ):
        return "managed_dstack_budget_unconfirmed"
    accounts = list(
        connection.execute(
            select(budget_accounts).where(budget_accounts.c.id.in_(account_ids))
        ).mappings()
    )
    if len(accounts) != len(account_ids) or any(
        account["tenant_id"] != payload.get("tenant_id")
        or account["owner_id"] is not None
        and account["owner_id"] != payload.get("owner_id")
        or account["project_id"] is not None
        and account["project_id"] != payload.get("project_id")
        or account["reserved_microusd"] < amount
        or account["spent_microusd"] + account["reserved_microusd"]
        > effective_budget_limit(account)
        for account in accounts
    ):
        return "managed_dstack_budget_unconfirmed"
    started = connection.execute(
        select(scaler_actions.c.create_started_at).where(
            scaler_actions.c.intent_id == intent["id"]
        )
    ).scalar_one_or_none()
    if started is None:
        started = intent.get("created_at")
    if not _finite(started) or not 0 <= now - started:
        return "managed_dstack_budget_unconfirmed"
    funded_seconds = (amount - extra) * 3600 / hourly
    if (
        now - started + duration + COMPLETION_MARGIN_SECONDS + SHUTDOWN_MARGIN_SECONDS
        >= funded_seconds
    ):
        return (
            "job_exceeds_spend_runway"
            if expected_runtime_s is not None
            else "worker_spend_limit_reached"
        )
    return None


def managed_window_reason(
    intent,
    node,
    now,
    *,
    expected_runtime_s=None,
    deployment_profile_id=None,
    binding=None,
):
    from .provider_lifetime import provider_lifetime_current

    payload = node.get("payload", {})
    lifecycle = payload.get("lifecycle")
    policy_lifetime = payload.get("capacity_backend") == "dstack-v1" and (
        intent.get("hard_deadline") is None
        or isinstance(lifecycle, dict)
        and lifecycle.get("version") == 2
    )
    if policy_lifetime:
        reason = dstack_policy_reason(intent, node, now)
        if reason:
            return reason
        from .dstack_test_budget import runtime_reason

        reason = runtime_reason(
            intent,
            node,
            now,
            expected_runtime_s=expected_runtime_s
            if expected_runtime_s is not None
            else 0,
        )
        if reason:
            return reason
        deadline = None
    else:
        if not _finite(intent.get("hard_deadline")):
            return "worker_window_unconfirmed"
        deadline = (
            min(intent["hard_deadline"], binding.expires_at)
            if binding
            else intent["hard_deadline"]
        )
    if node["desired_state"] != "running" or intent["state"] not in {
        "starting",
        "ready",
        "busy",
    }:
        return "worker_capacity_stopping"
    if binding and (not binding.enabled or node["binding_hash"] != binding.fingerprint):
        return "managed_binding_changed"
    if node.get("runtime_state") in {
        "blocked",
        "failed",
        "bootstrap_unconfigured",
        "observation_failed",
        "provider_execution_unverified",
        "removal_pending",
        "stopped",
        "draining",
    }:
        return "managed_runtime_unavailable"
    if deadline is not None and now >= deadline - STOP_NEW_SECONDS:
        return "worker_window_closing"
    if payload.get("capacity_backend") == "dstack-v1":
        # Historical finite dstack windows remain binding. New lifecycle v2
        # uses funded idle/keep-on policy, never a fabricated supplier deadline.
        proof = payload.get("dstack_observation", {})
        original = payload.get("dstack", {})
        observed = proof.get("observed_at")
        if (
            type(observed) not in (int, float)
            or not math.isfinite(observed)
            or not 0 <= now - observed <= payload.get("observation_fresh_seconds", 30)
            or proof.get("status") != "running"
            or not original.get("run_id")
            or proof.get("run_id") != original.get("run_id")
            or proof.get("instance_id") != intent["provider_instance_id"]
            or node.get("runtime_state") not in {"ready", "busy"}
            or not original.get("runtime_incarnation")
        ):
            return "managed_dstack_readiness_unconfirmed"
    elif not provider_lifetime_current(payload, intent, now):
        return "managed_provider_lifetime_unverified"
    if (
        binding is None
        and node.get("payload", {}).get("selection", {}).get("runtime_profile_id")
        != deployment_profile_id
    ):
        return "matching_profile_unavailable"
    if expected_runtime_s is not None:
        if (
            type(expected_runtime_s) not in (int, float)
            or not math.isfinite(expected_runtime_s)
            or expected_runtime_s <= 0
        ):
            return "job_runtime_unconfirmed"
        if (
            deadline is not None
            and now + expected_runtime_s + COMPLETION_MARGIN_SECONDS >= deadline
        ):
            return "job_exceeds_worker_window"
    return None


def matching_dstack_queued_jobs(connection, intent, node):
    """Read compatible accepted work in this run's shared tenant/pool.

    This pauses business idle only; it grants no slot, funding or dispatch.
    Original bound attempts retain their separate recovery obligations.
    """
    payload = node.get("payload", {})
    lifecycle, request, original = (
        payload.get("lifecycle", {}),
        payload.get("request", {}),
        payload.get("dstack", {}),
    )
    if (
        node.get("desired_state") != "running"
        or payload.get("capacity_backend") != "dstack-v1"
        or not all(isinstance(value, dict) for value in (lifecycle, request, original))
        or lifecycle.get("version") != 2
        or request.get("lifecycle_version") != 2
        or original.get("lifecycle_version") != 2
        or any(
            not isinstance(payload.get(field), str) or not payload[field]
            for field in ("tenant_id", "owner_id", "project_id")
        )
        or any(
            not isinstance(original.get(field), str)
            or not original[field]
            or request.get(field) != original[field]
            for field in (
                "profile_id",
                "mode",
                "model_id",
                "configuration_id",
                "manifest_digest",
            )
        )
    ):
        return ()
    recipe = {"fl": "h3-base-fl2va-v1", "ref": "h3-base-ref2va-v1"}.get(
        original["mode"]
    )
    if recipe is None:
        return ()
    from .runtime_catalog import profile_output_delivery

    try:
        original_delivery = profile_output_delivery(original["profile_id"])
    except ValueError:
        return ()
    from .identity import account_status

    # Unclaimed work is not bound to this machine. A manual stop must leave it
    # queued for other capacity rather than hold a drained worker indefinitely.
    queued = connection.execute(
        select(jobs).where(
            jobs.c.tenant_id == payload["tenant_id"],
            jobs.c.pool == intent["pool"],
            jobs.c.status.in_(("queued", "waiting_capacity")),
            jobs.c.attempt_no == 0,
            jobs.c.current_attempt_id.is_(None),
            jobs.c.lease_worker_id.is_(None),
            jobs.c.lease_expires_at.is_(None),
        )
    ).mappings()
    matching = []
    for job in queued:
        stored, plan = job["request"], job["execution_plan"]
        if not isinstance(stored, dict) or not isinstance(plan, dict):
            continue
        effective = stored.get("request", stored)
        if (
            not isinstance(effective, dict)
            or stored.get("recipe_id") != recipe
            or stored.get("deployment_profile_id") != original["profile_id"]
            or effective.get("model") != original["model_id"]
            or effective.get("mode") != original["mode"]
            or plan.get("pool") != intent["pool"]
            or plan.get("backend") != "wangp-worker"
            or plan.get("enabled") is not True
            or plan.get("dispatch_backend") != "hatchet-v1"
            or plan.get("output_delivery") != original_delivery
            or plan.get("deployment_profile_id") != original["profile_id"]
            or plan.get("configuration_id") != original["configuration_id"]
            or plan.get("engine_manifest_digest") != original["manifest_digest"]
        ):
            continue
        if plan.get("capacity_binding") is not None:
            # Native no-deadline runs cannot satisfy pool-members-v1's finite
            # member deadline. Never let private work keep shared capacity on.
            # Preserve the old owner/project fence for finite legacy rows;
            # their member eligibility remains with the original claim helper.
            if intent.get("hard_deadline") is None or (
                job["owner_id"] != payload["owner_id"]
                or job["project_id"] != payload["project_id"]
            ):
                continue
        if account_status(connection, job["tenant_id"], job["owner_id"]) != "active":
            continue
        matching.append(job)
    return tuple(matching)


def _dstack_idle_reason(connection, intent, node, now):
    workers = list(
        connection.execute(
            select(registered_workers.c.id, registered_workers.c.current_job_id).where(
                registered_workers.c.provider == intent["provider"],
                registered_workers.c.instance_id == intent["provider_instance_id"],
            )
        ).mappings()
    )
    worker_ids = [worker["id"] for worker in workers]
    related = list(
        connection.execute(
            select(attempts).where(attempts.c.worker_id.in_(worker_ids))
        ).mappings()
    )
    job_ids = {
        worker["current_job_id"] for worker in workers if worker["current_job_id"]
    }
    job_ids.update(attempt["job_id"] for attempt in related)
    linked = list(
        connection.execute(
            select(jobs.c.status, jobs.c.updated_at).where(jobs.c.id.in_(job_ids))
        ).mappings()
    )
    obligations = any(
        job["status"] not in {"succeeded", "failed", "cancelled"} for job in linked
    )
    obligations |= any(
        attempt["submission_started_at"] is not None
        and attempt["upstream_stopped"] != 1
        for attempt in related
    )
    obligations |= bool(matching_dstack_queued_jobs(connection, intent, node))
    ready_at = node["payload"].get("dstack", {}).get("first_runtime_ready_at")
    if not _finite(ready_at) or not 0 <= ready_at <= now:
        return "managed_dstack_lifecycle_unconfirmed"
    last = max([ready_at, *[job["updated_at"] for job in linked]])
    return dstack_policy_reason(
        intent, node, now, last_business_activity=last, has_obligations=obligations
    )


def worker_window_reason(
    connection, worker, now, *, expected_runtime_s=None, deployment_profile_id=None
):
    """Unmanaged slots retain the existing intent TTL gate; managed slots add proof."""
    if worker['provider'] == 'modal':
        from .modal_budget import worker_reason
        from .modal_dispatch import consumer_reason

        return worker_reason(
            connection, worker, now, expected_runtime_s
        ) or consumer_reason(connection, worker, now)
    from .operator_capacity import operator_nodes

    managed = operator_nodes.name in inspect(connection).get_table_names()
    intents = connection.execute(
        select(instance_intents).where(
            instance_intents.c.provider == worker["provider"],
            instance_intents.c.provider_instance_id == worker["instance_id"],
        )
    ).mappings()
    for intent in intents:
        node = (
            connection.execute(
                select(operator_nodes).where(operator_nodes.c.intent_id == intent["id"])
            )
            .mappings()
            .first()
            if managed
            else None
        )
        if node is not None:
            if node.get("payload", {}).get("capacity_backend") == "dstack-v1":
                original = node["payload"].get("dstack", {})
                spec = worker["spec"]
                if (
                    spec.get("dispatch_backend") != "hatchet-v1"
                    or spec.get("engine_manifest_digest")
                    != original.get("manifest_digest")
                    or spec.get("configuration_id") != original.get("configuration_id")
                    or spec.get("model_id") != original.get("model_id")
                ):
                    return "managed_dstack_binding_mismatch"
                consumer = original.get("consumer_observation", {})
                observed = consumer.get("observed_at")
                if (
                    consumer.get("ready") is not True
                    or consumer.get("worker_id") != worker["id"]
                    or consumer.get("worker_fence") != worker["fence"]
                    or consumer.get("instance_id") != worker["instance_id"]
                    or consumer.get("runtime_incarnation")
                    != original.get("runtime_incarnation")
                    or not original.get("runtime_incarnation")
                    or not _finite(observed)
                    or not 0 <= now - observed <= CONSUMER_HEARTBEAT_SECONDS
                ):
                    return "managed_queue_consumer_unconfirmed"
            reason = managed_window_reason(
                intent,
                node,
                now,
                expected_runtime_s=expected_runtime_s,
                deployment_profile_id=deployment_profile_id,
            )
            if reason:
                return reason
            if intent["hard_deadline"] is None:
                reason = _dstack_idle_reason(
                    connection, intent, node, now
                ) or dstack_budget_reason(
                    connection, intent, node, now, expected_runtime_s=expected_runtime_s
                )
                if reason:
                    return reason
        elif not _finite(intent["hard_deadline"]):
            return "worker_window_unconfirmed"
        elif (
            intent["state"] not in {"starting", "ready", "busy"}
            or intent["hard_deadline"] <= now
        ):
            return "worker_capacity_stopping"
        elif (
            expected_runtime_s is not None
            and now + expected_runtime_s + COMPLETION_MARGIN_SECONDS
            >= intent["hard_deadline"]
        ):
            return "job_exceeds_worker_window"
    return None
