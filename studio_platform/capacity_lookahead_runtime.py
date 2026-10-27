"""Opt-in demand lookahead over original capacity/worker/job evidence.

The planner can only refuse a new start. The original admission transaction
still owns budget reservation, provider creation, fencing and recovery.
"""

from dataclasses import asdict
import math
import uuid

from sqlalchemy import insert, select

from .capacity_lookahead import Machine, Policy, plan
from .control import WorkerControl
from .operator_capacity import require
from .repository import instance_intents, jobs, outbox, registered_workers, request_hash


def enabled(service):
    value = service.policy.get("demand_start", {}).get("lookahead_version")
    return type(value) is int and value == 1


def rules(service, connection, profile):
    from .capacity_start_rules import StartRules
    from .dstack_workspace import DstackWorkspace

    settings = StartRules(DstackWorkspace(service))
    identity, binding = settings._target(profile["id"])
    return settings._load(connection, identity, binding)[0]


def idle_seconds(service, connection, profile):
    if not enabled(service):
        return service.policy["idle_shutdown_seconds"]
    return rules(service, connection, profile)["value"]["boot_seconds"]


def same_work(left, right):
    a, b = left["execution_plan"], right["execution_plan"]
    return (
        all(left[k] == right[k] for k in ("tenant_id", "pool"))
        and all(
            a.get(k) == b.get(k)
            for k in (
                "backend",
                "dispatch_backend",
                "configuration_id",
                "model_id",
                "engine_manifest_digest",
            )
        )
        and left["request"].get("deployment_profile_id")
        == right["request"].get("deployment_profile_id")
        and left["request"].get("request", {}).get("mode")
        == right["request"].get("request", {}).get("mode")
    )


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def evaluate(service, connection, job, profile, work, usage, now):
    """Use the caller's fresh native allocation witness; never issue provider I/O.

    At most one proposed start per tick. Affordable headroom is NOT asserted by
    this result: all existing financial gates must succeed afterwards. Ambiguous
    active capacity blocks; stopping/expired slots never create free headroom.
    """
    from .dstack_demand import _matches, _nodes
    from .dstack_operator import PROVIDERS

    stored = rules(service, connection, profile)
    maximum, _ = service.machine_limits(connection, for_demand=True)
    policy = Policy(**stored["value"], max_machines=maximum)
    witness = {item["intent_id"]: item for item in usage["observations"]}
    machines, remaining, identities = [], 0.0, set()
    blocked = False
    workers = {
        row["id"]: dict(row)
        for row in connection.execute(
            select(registered_workers).where(registered_workers.c.pool == job["pool"])
        ).mappings()
    }
    for node in _nodes(connection, service.settings.tenant_id):
        if node["payload"].get("owner_id") != profile["owner_id"] or not _matches(
            node, job
        ):
            continue
        observation = witness.get(node["intent_id"])
        if observation is not None and observation["counted"] is False:
            continue
        if (
            observation is None
            or observation["reason_code"] != "dstack_capacity_active"
        ):
            blocked = True
            continue
        binding = node["payload"]["dstack"]
        physical = (binding.get("backend"), node["provider_instance_id"])
        if physical[1] is not None and physical in identities:
            blocked = True
            continue
        identities.add(physical)
        worker = workers.get("dstack-" + node["intent_id"].replace("-", ""))
        ready = bool(
            worker
            and worker["state"] == "ready"
            and not worker["drain_requested"]
            and _number(worker["expires_at"])
            and worker["expires_at"] > now
            and worker["instance_id"] == node["provider_instance_id"]
            and worker["provider"] == PROVIDERS.get(binding.get("backend"))
            and node["desired_state"] == "running"
            and WorkerControl.matches(worker, job)
        )
        if ready:
            machines.append(Machine(node["intent_id"], "ready"))
            if worker["current_job_id"]:
                current = (
                    connection.execute(
                        select(jobs).where(jobs.c.id == worker["current_job_id"])
                    )
                    .mappings()
                    .first()
                )
                # A busy slot contributes its remaining configured job estimate,
                # not another full queued job. Missing timing keeps one estimate.
                elapsed = 0
                if current and same_work(dict(current), job):
                    from .repository import attempts

                    started = connection.scalar(
                        select(attempts.c.submission_started_at).where(
                            attempts.c.id == current["current_attempt_id"],
                            attempts.c.job_id == current["id"],
                        )
                    )
                    if _number(started) and started <= now:
                        elapsed = now - started
                remaining += max(0, policy.job_seconds - elapsed)
        else:
            # A known allocated host without a live usable registration occupies
            # a starting slot. A stale heartbeat cannot supply forecast throughput.
            created = connection.scalar(
                select(instance_intents.c.created_at).where(
                    instance_intents.c.id == node["intent_id"]
                )
            )
            eta = (
                max(0, policy.boot_seconds - (now - created))
                if worker is None and _number(created) and created <= now
                else None
            )
            machines.append(
                Machine(node["intent_id"], "starting", ready_in_seconds=eta)
            )
            if node["desired_state"] != "running":
                blocked = True
    queued = sum(
        selected["id"] == profile["id"] and same_work(other, job)
        for other, selected in work
    )
    decision = plan(
        policy,
        tuple(machines),
        queued=queued,
        remaining_job_seconds=remaining,
        available_slots=1,
        admission_blocked=blocked,
    )
    return {
        "version": 1,
        "rules_revision": stored["revision"],
        "policy": asdict(policy),
        "queued": queued,
        "remaining_job_seconds": remaining,
        "machines": [asdict(machine) for machine in machines],
        "decision": asdict(decision),
    }


def record_decision(
    connection, tenant, job_id, snapshot, now, *, prior=None, phase="planning"
):
    if prior is not None and request_hash(prior) == request_hash(snapshot):
        return
    connection.execute(
        insert(outbox).values(
            id=str(uuid.uuid4()),
            event_type="capacity.lookahead_decision",
            aggregate_id=job_id,
            payload={"tenant_id": tenant, "job_id": job_id, "phase": phase, **snapshot},
            created_at=now,
        )
    )


def enforce(service, connection, value, profile, usage, now):
    if not enabled(service) or not value.get("demand_start"):
        return
    from .dstack_demand import eligible_jobs

    work = eligible_jobs(service, connection, now)
    job = next(
        (job for job, _ in work if job["id"] == value["demand_start"]["job_id"]), None
    )
    require(job is not None, "dstack_demand_changed")
    snapshot = evaluate(service, connection, job, profile, work, usage, now)
    require(
        snapshot["rules_revision"] == value["demand_start"].get("start_rules_revision"),
        "dstack_demand_changed",
    )
    require(
        value.get("lifecycle", {}).get("idle_shutdown_seconds")
        == snapshot["policy"]["boot_seconds"],
        "dstack_demand_changed",
    )
    require(snapshot["decision"]["start_count"] > 0, "dstack_lookahead_covered")
    record_decision(
        connection,
        service.settings.tenant_id,
        job["id"],
        snapshot,
        now,
        phase="admission",
    )


def admit_capacity(service, connection, owner, value, profile):
    """Original admission gates, with lookahead rechecked before reservation."""
    from .operator_capacity import OperatorError
    from .supplier_spending import admit as admit_supplier_spending

    if value.get("group_id") is not None:
        from .dstack_workspace import owned_group

        owned_group(connection, service.settings, owner, value["group_id"])
    now = service.repo.clock()
    guard = value.get("lifecycle", {}).get("test_gpu_guard")
    if guard is not None:
        from .dstack_test_budget import (
            projected_cost,
            guard_limit,
            limit_reason,
            validate_guard,
        )

        try:
            validate_guard(guard)
        except ValueError:
            raise OperatorError("dstack_test_guard_invalid", 422) from None
        require(
            guard["version"] == 2
            and value["request"].get("test_task_id") == guard["task_id"],
            "dstack_test_task_required",
            422,
        )
        require(
            projected_cost(connection, guard, now) + value["reservation_microusd"]
            < guard_limit(guard),
            limit_reason(guard),
        )
    capacity_snapshot = admit_supplier_spending(
        connection,
        service.settings.tenant_id,
        now,
        additional_microusd=value["reservation_microusd"],
        reference_type="instance",
        test_task=guard is not None,
        policy=service._admin_limits(connection),
    )
    usage = service._limits(
        connection,
        profile["max_price_microusd"],
        profile=profile,
        request=value["request"],
        capacity_snapshot=capacity_snapshot,
    )
    enforce(service, connection, value, profile, usage, now)
    return now, capacity_snapshot, usage
