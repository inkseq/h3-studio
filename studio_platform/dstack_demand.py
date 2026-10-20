"""Demand projections over the original jobs, operator policy and machine rows.

Creator admission/status uses stored projections. Human-admin capacity counts
reuse the existing read-only live-capacity witness. The original operator start
transaction remains the allocation fence.
"""

import json
import math
import os

from sqlalchemy import select

from .operator_capacity import operator_nodes, require
from .repository import (
    attempts,
    instance_intents,
    jobs,
    request_hash,
)

HARD_MAX_MACHINES = 2
MAX_HOURLY_PRICE = 850000
DERIVED_PROFILE_FIELDS = {"model_id", "manifest_digest", "recipe_id"}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _open(value, now):
    return value is None or _finite(value) and now < value


def demand_configuration(config, *, now):
    policy = config.get("policy", {})
    demand = policy.get("demand_start")
    if not isinstance(demand, dict) or not demand:
        return None
    selected = demand.get("profile_ids")
    if (
        set(demand) - {"profile_ids", "test_task_id"}
        or policy.get("enabled") is not True
        or policy.get("lifecycle_version") != 2
        or not _open(policy.get("expires_at", 0), now)
        or not isinstance(selected, list)
        or not selected
        or any(not isinstance(value, str) or not value for value in selected)
        or len(selected) != len(set(selected))
    ):
        return None
    candidates = config.get("profiles", [])
    if not isinstance(candidates, list) or any(
        not isinstance(p, dict) for p in candidates
    ):
        return None
    profiles = [p for p in candidates if p.get("id") in selected]
    if len(profiles) != len(selected):
        return None
    if any(
        p.get("mode") not in {"fl", "ref"}
        or type(p.get("max_price_microusd")) is not int
        or not 0 < p["max_price_microusd"] <= MAX_HOURLY_PRICE
        for p in profiles
    ):
        return None
    guard = policy.get("test_gpu_guard")
    if guard is not None:
        from .dstack_test_budget import valid_task_id

        if not valid_task_id(demand.get("test_task_id")):
            return None
    elif demand.get("test_task_id") is not None:
        return None
    return profiles


def profile_hash(config, profile):
    return request_hash(
        {
            "version": 1,
            "project": config["project"],
            "profile": {
                key: value
                for key, value in profile.items()
                if key not in DERIVED_PROFILE_FIELDS
            },
            "demand_start": config["policy"]["demand_start"],
        }
    )


def _profile_for(config, compiled, policy, owner, now):
    from .runtime_catalog import engine_manifest, profile_output_delivery

    profiles = demand_configuration(config, now=now)
    if (
        profiles is None
        or not isinstance(policy, dict)
        or policy.get("backend") != "wangp-worker"
        or policy.get("dispatch_backend") != "hatchet-v1"
        or policy.get("enabled") is not True
    ):
        return None
    request = compiled.get("request", {})
    matches = []
    for profile in profiles:
        if (
            profile.get("runtime_profile_id") != compiled.get("deployment_profile_id")
            or profile.get("mode") != request.get("mode")
            or profile.get("configuration_id") != policy.get("configuration_id")
            or profile.get("pool") != policy.get("pool")
            or policy.get("output_delivery")
            != profile_output_delivery(profile["runtime_profile_id"])
        ):
            continue
        manifest = engine_manifest(profile["runtime_profile_id"], profile["mode"])
        if (
            manifest.digest != policy.get("engine_manifest_digest")
            or manifest.document["generation_recipe_id"] != compiled.get("recipe_id")
            or policy.get("model_id") != request.get("model")
        ):
            continue
        matches.append(profile)
    # Existing native workers serve the shared tenant/pool. Rental ownership
    # stays with an explicitly configured fleet profile; job ownership and
    # signed input/output authorization are never reassigned to that owner.
    matches.sort(key=lambda p: (p["owner_id"] != owner, p["id"]))
    return matches[0] if matches else None


def _protected_configuration():
    from .dstack_operator import _protected_read

    path = os.environ.get("DSTACK_OPERATOR_CONFIG")
    if not path:
        return {}
    value = json.loads(_protected_read(path))
    return value if isinstance(value, dict) and value.get("version") == 1 else {}


def demand_admission(settings, repo, scope, compiled, policy):
    """Only the current enabled, trusted fleet can admit a cold confirmed job."""
    from .dstack_demand_policy import read_runtime
    from .execution_policy import authority_current
    from .identity import account_status
    from .capacity import pool_members_require_warm_binding

    try:
        config = _protected_configuration()
        now = repo.clock()
        if scope.tenant_id != settings.tenant_id:
            return None
        profile = _profile_for(config, compiled, policy, scope.owner_id, now)
        if profile is None or not authority_current(policy, now):
            return None
        if pool_members_require_warm_binding(
            repo, scope.tenant_id, policy["pool"], policy["configuration_id"]
        ):
            return None
        with repo.engine.connect() as connection:
            if (
                not read_runtime(connection, settings.tenant_id)["enabled"]
                or account_status(connection, scope.tenant_id, scope.owner_id)
                != "active"
            ):
                return None
        return {
            "version": 1,
            "profile_id": profile["id"],
            "profile_hash": profile_hash(config, profile),
            "policy_hash": request_hash(policy),
            "test_task_id": config["policy"]["demand_start"].get("test_task_id"),
        }
    except (ValueError, KeyError, TypeError, OSError):
        return None


def eligible_jobs(service, connection, now):
    """Confirmed unsubmitted work only; never treat a wake hint as authority."""
    from .execution_policy import ExecutionPolicies
    from .execution_profiles import selected_policy
    from .identity import account_status

    if demand_configuration(service.config, now=now) is None:
        return []
    policies = ExecutionPolicies(service.settings, service.repo)
    from .capacity import pool_members_require_warm_binding

    rows = connection.execute(
        select(jobs)
        .where(
            jobs.c.tenant_id == service.settings.tenant_id,
            jobs.c.status == "queued",
            jobs.c.not_before <= now,
            jobs.c.lease_worker_id.is_(None),
            jobs.c.lease_expires_at.is_(None),
            jobs.c.execution_plan["dispatch_backend"].as_string() == "hatchet-v1",
        )
        .order_by(jobs.c.created_at, jobs.c.id)
    ).mappings()
    result = []
    for raw in rows:
        job = dict(raw)
        if job["current_attempt_id"]:
            previous = connection.execute(
                select(
                    attempts.c.submission_started_at, attempts.c.upstream_task_id
                ).where(
                    attempts.c.id == job["current_attempt_id"],
                    attempts.c.job_id == job["id"],
                )
            ).first()
            if (
                previous is None
                or previous.submission_started_at is not None
                or previous.upstream_task_id is not None
            ):
                # Mirror the claim guard without changing original history.
                continue
        execution, compiled = job["execution_plan"], job["request"]
        if (
            execution.get("backend") != "wangp-worker"
            or execution.get("capacity_binding") is not None
            or execution.get("capacity_approval_id") is not None
            or account_status(connection, job["tenant_id"], job["owner_id"]) != "active"
            or not policies.submission_allowed(job)
        ):
            continue
        if pool_members_require_warm_binding(
            service.repo, job["tenant_id"], job["pool"], execution["configuration_id"]
        ):
            continue
        unresolved = connection.execute(
            select(attempts.c.id)
            .where(
                attempts.c.job_id == job["id"],
                attempts.c.submission_started_at.is_not(None),
                (attempts.c.upstream_stopped != 1)
                | attempts.c.upstream_stopped.is_(None),
            )
            .limit(1)
        ).first()
        if unresolved:
            continue
        try:
            policy = selected_policy(
                service.settings,
                profile_id=compiled.get("deployment_profile_id"),
                recipe_id=compiled.get("recipe_id"),
            )
            if policy is None or request_hash(policy) != execution.get("policy_hash"):
                continue
            profile = _profile_for(
                service.config, compiled, policy, job["owner_id"], now
            )
        except (ValueError, KeyError, TypeError, OSError):
            continue
        if profile is not None:
            result.append((job, service.profiles[profile["id"]]))
    return result


def validate_start(service, connection, job_id, profile_id):
    """Last same-transaction demand/CAS guard, before any rental reservation."""
    from .dstack_demand_policy import read_runtime
    from .identity import account_status

    runtime = read_runtime(connection, service.settings.tenant_id)
    require(runtime["enabled"], "dstack_demand_disabled")
    locked = service.repo._locked(
        connection,
        select(jobs).where(
            jobs.c.id == job_id, jobs.c.tenant_id == service.settings.tenant_id
        ),
    )
    require(
        locked is not None and locked["status"] == "queued", "dstack_demand_changed"
    )
    match = next(
        (
            (job, profile)
            for job, profile in eligible_jobs(service, connection, service.repo.clock())
            if job["id"] == job_id and profile["id"] == profile_id
        ),
        None,
    )
    require(match is not None, "dstack_demand_changed")
    job, profile = match
    require(
        account_status(connection, service.settings.tenant_id, profile["owner_id"])
        == "active",
        "dstack_demand_changed",
    )
    return {
        "job_id": job_id,
        "plan_id": job["plan_id"],
        "request_hash": job["request_hash"],
        "execution_hash": request_hash(job["execution_plan"]),
        "profile_id": profile_id,
        "profile_hash": profile_hash(service.config, profile),
        "policy_hash": job["execution_plan"]["policy_hash"],
        "runtime_revision": runtime["revision"],
    }


def _nodes(connection, tenant):
    rows = connection.execute(
        select(
            operator_nodes,
            instance_intents.c.pool,
            instance_intents.c.provider_instance_id,
            instance_intents.c.state.label("instance_state"),
        )
        .join(instance_intents, instance_intents.c.id == operator_nodes.c.intent_id)
        .where(
            operator_nodes.c.payload["capacity_backend"].as_string() == "dstack-v1",
            operator_nodes.c.payload["tenant_id"].as_string() == tenant,
        )
    ).mappings()
    return [dict(row) for row in rows]


def node_stage(node, now):
    """A read-only UI hint, never a live-allocation or removal witness."""
    if node["runtime_state"] == "stopped":
        return None
    if node["desired_state"] != "running":
        return "unknown"
    payload = node["payload"]
    observed = payload.get("dstack", {}).get("observed_at")
    if (
        "unknown" in node["runtime_state"]
        or node["runtime_state"]
        in {"failed", "blocked", "observation_failed", "reconciliation_required"}
    ) or (
        observed is not None
        and (not _finite(observed) or not 0 <= now - observed <= 120)
    ):
        return "unknown"
    if node["runtime_state"] in {"ready", "busy"}:
        return "running"
    binding = payload.get("dstack", {})
    preparation = binding.get("preparation", {})
    phase = preparation.get("phase") if isinstance(preparation, dict) else None
    model = preparation.get("model") if isinstance(preparation, dict) else None
    model = model if isinstance(model, dict) else {}
    phase = phase or binding.get("bootstrap_phase")
    if (
        phase in {"model_load", "loading", "loading_model", "model_loading"}
        or model.get("state") == "loading"
    ):
        return "loading"
    if phase in {
        "download_file",
        "model_download_waiting_for_shared_cache",
        "downloading",
        "downloading_weights",
        "model_download",
        "download_weights",
    }:
        return "downloading"
    return "machine_starting"


def _matches(node, job):
    binding, plan = node["payload"].get("dstack", {}), job["execution_plan"]
    request = job["request"]
    return (
        node["pool"] == plan.get("pool")
        and binding.get("profile_id") == request.get("deployment_profile_id")
        and binding.get("mode") == request.get("request", {}).get("mode")
        and binding.get("model_id")
        == plan.get("model_id", request.get("request", {}).get("model"))
        and binding.get("configuration_id") == plan.get("configuration_id")
        and binding.get("manifest_digest") == plan.get("engine_manifest_digest")
        and not plan.get("capacity_binding")
    )


def job_wait_stage(connection, job, now):
    if job["status"] in {"claimed", "submitting", "running"}:
        return "running"
    matches = [
        node_stage(n, now)
        for n in _nodes(connection, job["tenant_id"])
        if _matches(n, job)
    ]
    for stage in ("running", "loading", "downloading", "machine_starting", "unknown"):
        if stage in matches:
            return "awaiting_worker" if stage == "running" else stage
    return "queued_no_machine"


def summary(service, connection, now):
    from .dstack_demand_policy import read_runtime

    runtime = read_runtime(connection, service.settings.tenant_id)
    configured = demand_configuration(service.config, now=now) is not None
    work = eligible_jobs(service, connection, now) if configured else []
    nodes = _nodes(connection, service.settings.tenant_id)
    stages = [node_stage(n, now) for n in nodes]
    if configured and nodes:
        try:
            # Human-admin reads are advisory, not an allocation or settlement.
            # The same canonical witness is re-read under the original start
            # lock before reserving capacity. Never turn old UI state alone
            # into either a live slot or positive removal evidence.
            usage = service._live_capacity_usage(connection)
            observed = {o["intent_id"]: o for o in usage["observations"]}
            stages = [
                None
                if observed.get(n["intent_id"], {}).get("counted") is False
                else "unknown"
                if n["intent_id"] not in observed
                or observed[n["intent_id"]].get("reason_code")
                in {
                    "dstack_capacity_unconfirmed",
                    "dstack_terminal_provider_unconfirmed",
                }
                else node_stage(n, now) or "unknown"
                for n in nodes
            ]
        except Exception:
            # Missing/unreadable evidence cannot erase even a stopped marker's
            # potential live occupancy. Keep original rows and invoices intact.
            stages = ["unknown" for _ in nodes]
    # Display all accepted native waiting work, including blocked jobs. Rental
    # eligibility above remains a separate, stricter authority decision.
    counts = {"fl": 0, "ref": 0}
    waiting = connection.execute(
        select(jobs.c.request).where(
            jobs.c.tenant_id == service.settings.tenant_id,
            jobs.c.status.in_(("queued", "waiting_capacity")),
            jobs.c.execution_plan["backend"].as_string() == "wangp-worker",
            jobs.c.execution_plan["dispatch_backend"].as_string() == "hatchet-v1",
        )
    ).scalars()
    for stored in waiting:
        effective = stored.get("request", stored) if isinstance(stored, dict) else {}
        mode = effective.get("mode") if isinstance(effective, dict) else None
        if isinstance(mode, str) and mode in counts:
            counts[mode] += 1
    running = stages.count("running")
    starting = sum(s in {"machine_starting", "downloading", "loading"} for s in stages)
    uncertain = stages.count("unknown")
    reason = (
        "disabled"
        if not runtime["enabled"]
        else "policy_unavailable"
        if not configured
        else "no_eligible_work"
        if not sum(counts.values())
        else "no_qualified_profile"
        if not work
        else "allocation_uncertain"
        if uncertain
        else "machine_limit"
        if running + starting >= HARD_MAX_MACHINES
        else None
    )
    return {
        **runtime,
        "hard_max_machines": HARD_MAX_MACHINES,
        "running_machines": running,
        "starting_machines": starting,
        "uncertain_machines": uncertain,
        "queued_generation_jobs": sum(counts.values()),
        "queued_by_mode": counts,
        "blocked_reason": reason,
    }
