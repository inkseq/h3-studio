"""Authenticated advisory capacity projection; no admission or provider actions."""

from contextlib import ExitStack
from sqlalchemy import select

from .control import WorkerControl, REAL_GPU_BACKENDS
from .execution_policy import read_policy, authority_current
from .execution_profiles import read_profiles
from .operator_capacity import (
    operator_nodes,
    operator_heartbeats,
    OperatorError,
    CONTROLLER_FRESH_SECONDS,
)
from .repository import Scope, instance_intents
from .runtime_catalog import public_catalog, profile_output_delivery

MODES = {"fl": "h3-base-fl2va-v1", "ref": "h3-base-ref2va-v1"}


def _result(recipe, state, reason):
    return {
        "recipe_id": recipe,
        "state": state,
        "reason_code": reason,
        "available": state in {"ready", "busy"},
        "can_queue": state in {"ready", "busy"},
    }


def _preparing(repo, registry, policy, mode, now, *, snapshot):
    """Only a fresh exact managed binding can claim an in-progress startup."""
    if not policy.get("deployment_profile_id"):
        return "unavailable"
    if snapshot["preparing"] is None:
        connection = snapshot["pool"].connection
        rows = list(
            connection.execute(
                select(
                    operator_nodes,
                    instance_intents.c.provider,
                    instance_intents.c.state.label("instance_state"),
                    instance_intents.c.hard_deadline,
                    instance_intents.c.provider_instance_id.label("bound_instance_id"),
                    instance_intents.c.pool,
                ).join(
                    instance_intents,
                    instance_intents.c.id == operator_nodes.c.intent_id,
                )
            ).mappings()
        )
        heartbeat = (
            connection.execute(
                select(operator_heartbeats).where(operator_heartbeats.c.id == "global")
            )
            .mappings()
            .first()
        )
        by_pool = {}
        for row in rows:
            by_pool.setdefault(row["pool"], []).append(row)
        snapshot["preparing"] = (by_pool, heartbeat)
    by_pool, heartbeat = snapshot["preparing"]
    rows = by_pool.get(policy["pool"], ())
    states = set()
    for row in rows:
        chosen = row["payload"].get("selection", {})
        if (
            chosen.get("runtime_profile_id") != policy["deployment_profile_id"]
            or chosen.get("mode") != mode
        ):
            continue
        from .worker_admission import dstack_policy_reason

        lifetime_v2 = (
            row["payload"].get("capacity_backend") == "dstack-v1"
            and row["hard_deadline"] is None
        )
        if (
            row["desired_state"] != "running"
            or row["instance_state"] in {"draining", "destroying", "destroyed"}
            or not lifetime_v2
            and (row["hard_deadline"] is None or row["hard_deadline"] <= now)
        ):
            continue
        if lifetime_v2 and dstack_policy_reason(
            {"hard_deadline": None, "state": row["instance_state"]}, row, now
        ):
            states.add("unknown")
            continue
        try:
            if row["payload"].get("capacity_backend") == "dstack-v1":
                original = row["payload"].get("dstack", {})
                request = row["payload"].get("request", {})
                exact = (
                    policy.get("dispatch_backend") == "hatchet-v1"
                    and original.get("configuration_id") == policy["configuration_id"]
                    and original.get("model_id") == policy["model_id"]
                    and original.get("manifest_digest")
                    == policy["engine_manifest_digest"]
                    and request.get("profile_id") == policy["deployment_profile_id"]
                    and request.get("mode") == mode
                    and policy.get("output_delivery")
                    == profile_output_delivery(policy["deployment_profile_id"])
                )
            else:
                binding = (
                    registry.get(row["binding_id"]) if registry is not None else None
                )
                exact = bool(
                    binding
                    and binding.fingerprint == row["binding_hash"]
                    and binding.runtime_profile_id == policy["deployment_profile_id"]
                    and binding.mode == mode
                    and binding.pool == policy["pool"]
                    and binding.configuration_id == policy["configuration_id"]
                    and binding.model_id == policy["model_id"]
                    and binding.engine_manifest_digest
                    == policy["engine_manifest_digest"]
                    and policy.get("output_delivery") == "native-frames-v1"
                    and binding.launch.provider == row["provider"]
                )
        except (OperatorError, ValueError, AttributeError, TypeError):
            exact = False
        if row["payload"].get("capacity_backend") == "dstack-v1":
            proof = row["payload"].get("dstack_observation", {})
            original = row["payload"].get("dstack", {})
            observed, freshness = (
                proof.get("observed_at"),
                row["payload"].get("observation_fresh_seconds", 30),
            )
            fresh = (
                type(observed) in (int, float)
                and type(freshness) in (int, float)
                and freshness > 0
                and 0 <= now - observed <= freshness
                and bool(original.get("run_id"))
                and proof.get("run_id") == original["run_id"]
                and proof.get("instance_id") == row["bound_instance_id"]
                and isinstance(proof.get("status"), str)
                and proof["status"]
                in {"pending", "submitted", "provisioning", "running"}
                and 0 <= now - row["updated_at"] <= 60
            )
        else:
            fresh = (
                heartbeat
                and heartbeat["state"] == "running"
                and 0 <= now - heartbeat["observed_at"] <= CONTROLLER_FRESH_SECONDS
                and 0 <= now - row["updated_at"] <= 60
            )
        if not exact or not fresh or row["instance_state"] == "creation_unknown":
            states.add("unknown")
        elif row["runtime_state"] in {
            "blocked",
            "failed",
            "bootstrap_unconfigured",
            "draining",
            "stopped",
            "removal_pending",
        }:
            states.add("unavailable")
        elif row["runtime_state"] in {
            "waiting_provider",
            "preparing",
            "starting",
            "waiting",
            "recovering",
            "awaiting_qualified_workers",
            "provider_lifetime_unverified",
            "provider_execution_unverified",
        }:
            states.add("starting")
        else:
            states.add("unknown")
    return (
        "starting"
        if "starting" in states
        else "unknown"
        if "unknown" in states
        else "unavailable"
    )


def _mode_result(
    settings,
    repo,
    registry,
    policy,
    valid,
    mode,
    recipe,
    now,
    *,
    control,
    snapshot,
    owner=None,
):
    demand = None
    if (
        not settings.generation_enabled
        or settings.execution_backend not in REAL_GPU_BACKENDS
    ):
        state, reason = "disabled", "generation_disabled"
    elif not valid:
        state, reason = "unknown", "availability_configuration_unconfirmed"
    elif policy is None or recipe not in policy["recipe_ids"]:
        state, reason = "disabled", "profile_not_configured"
    elif not policy["enabled"] or policy["backend"] != settings.execution_backend:
        state, reason = "disabled", "profile_disabled"
    elif not authority_current(
        policy, now, policy["reservation"]["expected_runtime_s"]
    ):
        state, reason = "disabled", "profile_authority_expired"
    else:
        if snapshot["pool"] is None:
            snapshot["pool"] = snapshot["contexts"].enter_context(
                control.pool_status_snapshot(now=now)
            )
        counts = control.pool_status(
            policy["pool"],
            model_id=policy["model_id"],
            configuration_id=policy["configuration_id"],
            recipe_id=recipe,
            backend=policy["backend"],
            engine_manifest_digest=policy.get("engine_manifest_digest", ""),
            output_delivery=policy.get("output_delivery", ""),
            expected_runtime_s=policy["reservation"]["expected_runtime_s"],
            deployment_profile_id=policy.get("deployment_profile_id"),
            dispatch_backend=policy.get("dispatch_backend", "legacy"),
            _snapshot=snapshot["pool"],
        )
        if counts["ready"]:
            state, reason = "ready", "matching_worker_ready"
        elif counts["busy"]:
            state, reason = "busy", "matching_worker_busy"
        elif counts["reason_counts"]:
            reason = sorted(counts["reason_counts"])[0]
            state = (
                "unknown"
                if reason == "managed_provider_lifetime_unverified"
                else "unavailable"
            )
        elif counts["unknown"]:
            state, reason = "unknown", "worker_readiness_unconfirmed"
        elif counts["registered"]:
            state, reason = "starting", "matching_worker_starting"
        else:
            state = _preparing(repo, registry, policy, mode, now, snapshot=snapshot)
            reason = {
                "starting": "matching_capacity_starting",
                "unknown": "capacity_readiness_unconfirmed",
                "unavailable": "matching_capacity_unavailable",
            }[state]
        if owner is not None and state not in {"ready", "busy"}:
            from .dstack_demand import demand_admission

            demand = demand_admission(
                settings,
                repo,
                Scope(settings.tenant_id, owner, "__availability"),
                {
                    "deployment_profile_id": policy.get("deployment_profile_id"),
                    "recipe_id": recipe,
                    "request": {"mode": mode, "model": policy["model_id"]},
                },
                policy,
            )
    result = _result(recipe, state, reason)
    result["can_queue"] = result["available"] or demand is not None
    return result


def generation_availability(settings, repo, *, registry=None, owner=None):
    now = repo.clock()
    try:
        policies = read_profiles(settings.execution_profiles_file)
        policies_valid = True
    except ValueError:
        policies, policies_valid = {}, False
    profiles = []
    control = WorkerControl(repo)
    with ExitStack() as contexts:
        snapshot = {"pool": None, "preparing": None, "contexts": contexts}
        for profile in public_catalog()["profiles"]:
            modes = {}
            for mode, recipe in MODES.items():
                policy = policies.get((profile["id"], recipe))
                modes[mode] = _mode_result(
                    settings,
                    repo,
                    registry,
                    policy,
                    policies_valid,
                    mode,
                    recipe,
                    now,
                    control=control,
                    snapshot=snapshot,
                    owner=owner,
                )
            profiles.append({"deployment_profile_id": profile["id"], "modes": modes})
        # Missing profile selection is the existing legacy route, never the default
        # named profile. Read precisely the same policy source as selected_policy().
        try:
            legacy, legacy_valid = read_policy(settings.execution_policy_file), True
        except ValueError:
            legacy, legacy_valid = None, False
        profiles.append(
            {
                "deployment_profile_id": None,
                "modes": {
                    mode: _mode_result(
                        settings,
                        repo,
                        registry,
                        legacy,
                        legacy_valid,
                        mode,
                        recipe,
                        now,
                        control=control,
                        snapshot=snapshot,
                        owner=owner,
                    )
                    for mode, recipe in MODES.items()
                },
            }
        )
    return {
        "version": 1,
        "observed_at": now,
        "expires_at": now + 10,
        "poll_after_seconds": 10,
        "advisory_only": True,
        "profiles": profiles,
    }
