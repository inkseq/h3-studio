"""Read-only native capacity ownership, resource and idle projections."""

from __future__ import annotations

import logging

import math
import re
import time


from .dstack_control_plane import foreign_usage


from .dstack_request import (
    BACKENDS,
    IDENTIFIER,
    TERMINAL,
    RUN_STATUSES,
    DstackError,
    _require,
    _uuid,
)

_logger = logging.getLogger(__name__)


def assert_run_owned(run, binding, project):
    """Compare the owned configuration subset, tolerating added server defaults."""
    _require(
        isinstance(run, dict) and run.get("project_name") == project,
        "dstack_run_ownership_mismatch",
    )
    _uuid(run.get("id"))
    spec = run.get("run_spec", {})
    wanted = binding["run_spec"]
    _require(isinstance(spec, dict), "dstack_run_binding_mismatch")
    _require(
        spec.get("run_name") == binding["run_name"], "dstack_run_ownership_mismatch"
    )
    actual = spec.get("configuration", {})
    expected = wanted["configuration"]
    _require(isinstance(actual, dict), "dstack_run_binding_mismatch")
    for key in (
        "type",
        "name",
        "image",
        "commands",
        "backends",
        "retry",
        "max_duration",
        "max_price",
        "tags",
    ):
        _require(actual.get(key) == expected[key], "dstack_run_binding_mismatch")
    # SSH runs carry no env; a dial-out run carries exactly its boot env (#859).
    _require(
        (actual.get("env") or {}) == (expected.get("env") or {})
        and actual.get("ports", []) in ([], None)
        and actual.get("dstack", False) is False,
        "dstack_run_binding_mismatch",
    )
    _require(
        spec.get("ssh_key_pub") == wanted["ssh_key_pub"], "dstack_run_binding_mismatch"
    )
    if "profile" in wanted:
        profile = spec.get("profile")
        _require(
            isinstance(profile, dict)
            and profile.get("instance_types") == wanted["profile"]["instance_types"],
            "dstack_host_selector_changed",
        )
        _require(actual.get("instance_types") is None, "dstack_host_selector_changed")
        _require(
            all(
                value in (None, False, "", "default")
                for key, value in profile.items()
                if key not in {"instance_types", "name"}
            )
            and profile.get("name") in (None, "", "default"),
            "dstack_host_selector_changed",
        )
    for key in (
        "privileged",
        "docker",
        "registry_auth",
        "entrypoint",
        "schedule",
        "groups",
    ):
        _require(actual.get(key) in (None, False), "dstack_run_binding_mismatch")
    # 0.22.5 run preprocessing stores a homogeneous TaskConfiguration's
    # omitted nodes as integer 1. Preserve the frozen spec while allowing only
    # that single-node default, never multinode or grouped execution.
    nodes = actual.get("nodes")
    _require(
        nodes is None or type(nodes) is int and nodes == 1,
        "dstack_run_binding_mismatch",
    )
    for key in ("files", "repos", "setup", "volumes"):
        _require(actual.get(key) in (None, []), "dstack_run_binding_mismatch")
    _require(
        actual.get("spot_policy") == expected["spot_policy"]
        and actual.get("regions") == expected["regions"]
        and actual.get("stop_duration") == expected["stop_duration"],
        "dstack_run_binding_mismatch",
    )
    _require(
        _resources(actual.get("resources")) == _resources(expected["resources"]),
        "dstack_run_binding_mismatch",
    )
    wanted_options = expected.get("backend_options", [])
    actual_options = actual.get("backend_options") or []
    _require(
        isinstance(actual_options, list)
        and len(wanted_options) == len(actual_options)
        and all(
            isinstance(observed, dict)
            and all(observed.get(key) == value for key, value in desired.items())
            for observed, desired in zip(actual_options, wanted_options, strict=False)
        ),
        "dstack_run_binding_mismatch",
    )
    if binding.get("run_id"):
        _require(run["id"] == binding["run_id"], "dstack_run_identity_changed")


# Compatibility for retained tests/callers; the public validator is authoritative.
_owns = assert_run_owned


def _range(value):
    # The pinned API serializes parsed scalar/range requirements as min/max.
    if isinstance(value, dict) and set(value) == {"min", "max"}:
        return value
    if type(value) in (int, float):
        return {"min": value, "max": value}
    if isinstance(value, str):
        match = re.fullmatch(r"([0-9]+)(?:GB)?\.\.", value)
        if match:
            return {"min": float(match.group(1)), "max": None}
    raise DstackError("dstack_resource_response_invalid")


def _resources(value):
    _require(isinstance(value, dict), "dstack_resource_response_invalid")
    cpu, gpu, disk = value.get("cpu", {}), value.get("gpu", {}), value.get("disk", {})
    _require(
        all(isinstance(item, dict) for item in (cpu, gpu, disk)),
        "dstack_resource_response_invalid",
    )
    return {
        "cpu_arch": cpu.get("arch"),
        "cpu_count": _range(cpu.get("count")),
        "memory": _range(value.get("memory")),
        "disk": _range(disk.get("size")),
        "gpu_vendor": gpu.get("vendor"),
        "gpu_name": gpu.get("name"),
        "gpu_count": _range(gpu.get("count")),
        "gpu_memory": _range(gpu.get("memory")),
    }


def terminal_no_allocation_shape(run, binding):
    """Complete failed singleton without runtime identity; not financial proof."""
    if (
        run.get("status") != "failed"
        or run.get("termination_reason") != "job_failed"
        or any(
            binding.get(key) is not None
            for key in (
                "provider_instance_id",
                "runtime_incarnation",
                "first_runtime_ready_at",
            )
        )
        or any(
            binding.get(key) not in (None, False)
            for key in (
                "bootstrap_started",
                "bootstrap_launch_started",
                "runtime_launch_started",
            )
        )
    ):
        return False
    jobs = run.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != 1 or not isinstance(jobs[0], dict):
        return False
    submissions = jobs[0].get("job_submissions")
    latest = run.get("latest_job_submission")
    if (
        not isinstance(submissions, list)
        or len(submissions) != 1
        or not isinstance(latest, dict)
        or latest != submissions[0]
    ):
        return False
    try:
        _uuid(latest.get("id"))
    except DstackError:
        return False
    return (
        latest.get("status") == "failed"
        and latest.get("termination_reason") == "failed_to_start_due_to_no_capacity"
        and all(
            key in latest and latest[key] is None
            for key in ("job_provisioning_data", "job_runtime_data")
        )
    )


_terminal_no_allocation_shape = terminal_no_allocation_shape


def terminal_no_allocation(run, binding):
    from .dstack_allocation_proof import terminal_no_allocation as qualify

    return qualify(run, binding, shape_check=terminal_no_allocation_shape)


def terminal_allocated_identity(run, backend):
    """One original allocation from a terminal singleton; not absence or billing."""
    if not isinstance(run, dict) or run.get("status") not in TERMINAL:
        return None
    jobs = run.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != 1 or not isinstance(jobs[0], dict):
        return None
    submissions = jobs[0].get("job_submissions")
    if not isinstance(submissions, list) or not submissions:
        return None
    # Also refuse conflicting evidence in the top-level projection of the
    # latest submission; it must not hide a second allocation.
    latest = run.get("latest_job_submission")
    records = submissions + ([latest] if latest is not None else [])
    identities = set()
    for submission in records:
        if not isinstance(submission, dict):
            return None
        provisioning = submission.get("job_provisioning_data")
        if provisioning is None:
            continue
        if not isinstance(provisioning, dict) or provisioning.get("backend") != backend:
            return None
        identity = provisioning.get("instance_id")
        if not isinstance(identity, str) or not IDENTIFIER.fullmatch(identity):
            return None
        identities.add(identity)
    return next(iter(identities)) if len(identities) == 1 else None


def live_capacity_usage(
    client, nodes, *, provider_inventory=None, clock=time.time, fresh_seconds=30
):
    """Read current owned dstack capacity without settling historical invoices.

    Call with original operator-node rows while holding the existing capacity
    reservation lock, and use the result for that same reservation transaction.
    Legacy rental nodes are outside this path. An unsubmitted intent reserves
    capacity immediately; absence, failed reads and identity drift keep it.

    The injected read-only provider reader must authenticate to the SAME account
    as the configured dstack backend. Its complete account list is projected as
    ``{backend, complete: True, observed_at, instance_ids}``; a stock list or
    partial/paginated response is not absence evidence. Read-start timestamps
    bound freshness, and the clock is sampled after the reads. No input rows,
    reservations, budgets, billing states or provider resources are modified.
    """
    _require(
        isinstance(nodes, (list, tuple)) and len(nodes) <= 4096,
        "dstack_capacity_rows_invalid",
    )
    _require(
        type(fresh_seconds) in (int, float)
        and math.isfinite(fresh_seconds)
        and 0 < fresh_seconds <= 120,
        "dstack_capacity_freshness_invalid",
    )
    inventories, counted, seen, resources, observations, projections, accounted = (
        {},
        [],
        set(),
        {},
        [],
        [],
        {},
    )
    hourly = 0

    def provider_absent(backend, identity):
        if backend not in inventories:
            try:
                document = (
                    provider_inventory(backend)
                    if callable(provider_inventory)
                    else None
                )
                _require(
                    isinstance(document, dict)
                    and document.get("backend") == backend
                    and document.get("complete") is True,
                    "dstack_provider_inventory_unconfirmed",
                )
                ids = document.get("instance_ids")
                _require(
                    isinstance(ids, (list, tuple))
                    and len(ids) <= 4096
                    and all(
                        isinstance(value, str) and IDENTIFIER.fullmatch(value)
                        for value in ids
                    )
                    and len(set(ids)) == len(ids),
                    "dstack_provider_inventory_unconfirmed",
                )
                observed = document.get("observed_at")
                _require(
                    type(observed) in (int, float)
                    and math.isfinite(observed)
                    and observed > 0,
                    "dstack_provider_inventory_unconfirmed",
                )
                aliases = document.get("aliases", {})
                _require(
                    isinstance(aliases, dict)
                    and all(value in ids for value in aliases.values()),
                    "dstack_provider_inventory_unconfirmed",
                )
                inventories[backend] = (observed, frozenset(ids) | frozenset(aliases))
            except Exception as _observation_error:
                _logger.warning(
                    "dstack_observations_provider_absent_failed error_type=%s",
                    type(_observation_error).__name__,
                )
                inventories[backend] = None
        inventory = inventories[backend]
        now = clock()
        _require(
            type(now) in (int, float) and math.isfinite(now) and now > 0,
            "dstack_capacity_clock_invalid",
        )
        return bool(
            inventory
            and 0 <= now - inventory[0] <= fresh_seconds
            and identity not in inventory[1]
        )

    for node in nodes:
        _require(
            isinstance(node, dict) or hasattr(node, "keys"),
            "dstack_capacity_rows_invalid",
        )
        payload = node.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("capacity_backend") != "dstack-v1"
        ):
            continue
        intent_id = _uuid(node.get("intent_id"))
        _require(intent_id not in seen, "dstack_capacity_rows_invalid")
        seen.add(intent_id)
        cost = payload.get("hourly_cost_microusd")
        binding = payload.get("dstack")
        _require(
            isinstance(binding, dict) and binding.get("intent_id") == intent_id,
            "dstack_intent_binding_invalid",
        )
        included, reason, status = True, "dstack_capacity_pending", None
        resource_key = "intent:" + intent_id
        physical_gpus = 1
        if (
            node.get("desired_state") == "stopped"
            and binding.get("apply_started") is not True
            and not binding.get("run_id")
            and not binding.get("provider_instance_id")
            and binding.get("state") == "stopped"
            and binding.get("reason_code") == "dstack_never_applied"
            and binding.get("billing_state") == "no_charge_confirmed"
        ):
            included, reason = False, "dstack_never_applied"
        elif (
            foreign := foreign_usage(binding, client, provider_absent, BACKENDS)
        ) is not None:
            included, reason = foreign
        elif binding.get("apply_started") is True:
            try:
                run = (
                    client.get(run_id=binding["run_id"])
                    if binding.get("run_id")
                    else client.get(run_name=binding["run_name"])
                )
                assert_run_owned(run, binding, client.project)
                observed_status = run.get("status")
                _require(
                    isinstance(observed_status, str)
                    and observed_status in RUN_STATUSES,
                    "dstack_run_status_invalid",
                )
                status = observed_status
                reason = "dstack_capacity_active"
                if terminal_no_allocation(run, binding):
                    included, reason = False, "dstack_terminal_no_allocation"
                    projections.append(
                        (
                            intent_id,
                            cost,
                            resource_key,
                            physical_gpus,
                            included,
                            reason,
                            status,
                            binding.get("backend"),
                        )
                    )
                    continue
                if (
                    terminal_no_allocation_shape(run, binding)
                    and binding.get("host_observation") is None
                    and binding.get("host_failure") is None
                ):
                    backend = binding.get("backend")
                    _require(backend in BACKENDS, "dstack_backend_binding_mismatch")
                    # Fetch at most once for this backend. The final pass checks
                    # every current account ID against OTHER exact owned runs.
                    # An unknown PUT remains an unchanged accounting obligation.
                    provider_absent(backend, None)
                    reason = "dstack_terminal_provider_accounted"
                    projections.append(
                        (
                            intent_id,
                            cost,
                            resource_key,
                            physical_gpus,
                            included,
                            reason,
                            status,
                            backend,
                        )
                    )
                    continue
                provisioning = (run.get("latest_job_submission") or {}).get(
                    "job_provisioning_data"
                ) or {}
                identity = provisioning.get("instance_id")
                backend = binding.get("backend")
                _require(
                    backend in BACKENDS
                    and provisioning.get("backend") == backend
                    and isinstance(identity, str)
                    and IDENTIFIER.fullmatch(identity)
                    and binding.get("provider_instance_id") in (None, identity),
                    "dstack_instance_identity_unconfirmed",
                )
                resource_key = backend + ":" + identity
                gpus = (
                    provisioning.get("instance_type", {})
                    .get("resources", {})
                    .get("gpus")
                )
                if isinstance(gpus, list) and 1 <= len(gpus) <= 128:
                    physical_gpus = len(gpus)
                if status in TERMINAL:
                    if provider_absent(backend, identity):
                        included, reason = False, "dstack_terminal_provider_absent"
                    else:
                        reason = "dstack_terminal_provider_unconfirmed"
                accounted.setdefault(backend, {}).setdefault(identity, set()).add(
                    intent_id
                )
            except Exception as _observation_error:
                _logger.warning(
                    "dstack_observations_live_capacity_usage_failed error_type=%s",
                    type(_observation_error).__name__,
                )
                reason = "dstack_capacity_unconfirmed"
        projections.append(
            (
                intent_id,
                cost,
                resource_key,
                physical_gpus,
                included,
                reason,
                status,
                binding.get("backend"),
            )
        )
    # Later run/account reads can outlive an earlier positive absence receipt.
    # Revalidate every exclusion at the same final clock without another read.
    now = clock()
    _require(
        type(now) in (int, float) and math.isfinite(now) and now > 0,
        "dstack_capacity_clock_invalid",
    )
    for (
        intent_id,
        cost,
        resource_key,
        physical_gpus,
        included,
        reason,
        status,
        backend,
    ) in projections:
        if reason in {
            "dstack_terminal_provider_absent",
            "dstack_terminal_provider_accounted",
        }:
            inventory = inventories.get(backend)
            known = {
                identity
                for identity, owners in accounted.get(backend, {}).items()
                if len(owners) == 1 and intent_id not in owners
            }
            if (
                not inventory
                or not 0 <= now - inventory[0] <= fresh_seconds
                or reason == "dstack_terminal_provider_accounted"
                and not inventory[1].issubset(known)
            ):
                included, reason = True, "dstack_terminal_provider_unconfirmed"
            elif reason == "dstack_terminal_provider_accounted":
                included = False
        if included:
            _require(type(cost) is int and cost > 0, "dstack_active_cost_unconfirmed")
            counted.append(intent_id)
            # This adapter permits exactly one GPU per original run. Known
            # shared physical identities count once globally; unknown intents
            # retain independent conservative reservations.
            resources[resource_key] = max(resources.get(resource_key, 0), physical_gpus)
            hourly += cost
        observations.append(
            {
                "intent_id": intent_id,
                "counted": included,
                "reason_code": reason,
                "dstack_status": status,
            }
        )
    return {
        "instances": len(resources),
        "physical_gpus": sum(resources.values()),
        "logical_slots": len(counted),
        "hourly_cost_microusd": hourly,
        "active_instance_ids": tuple(counted),
        "resources": resources,
        "observations": observations,
    }


def idle_action(
    *,
    now,
    hard_deadline,
    hold_until,
    last_business_activity,
    idle_seconds,
    active_jobs,
    unsafe_attempts,
    collection_holds,
    observations_fresh,
    lifecycle_version=1,
    keep_on="idle",
    budget_available=True,
):
    """Pure policy; stopping is still guarded transactionally by begin_stop.

    A running sleep command is not business-idle. Finite version-1 rentals
    retain their original absolute lifetime. Explicit version 2 uses business
    idle/hold policy and the original ledger's bounded funding authority.
    """
    _require(
        all(
            type(v) in (int, float) and math.isfinite(v) and v >= 0
            for v in (now, hold_until, last_business_activity, idle_seconds)
        ),
        "dstack_idle_policy_invalid",
    )
    _require(
        idle_seconds > 0
        and all(
            type(n) is int and n >= 0
            for n in (active_jobs, unsafe_attempts, collection_holds)
        )
        and type(observations_fresh) is bool,
        "dstack_idle_policy_invalid",
    )
    _require(
        type(lifecycle_version) is int
        and lifecycle_version in {1, 2}
        and type(budget_available) is bool
        and isinstance(keep_on, str)
        and keep_on in {"idle", "until", "until_stopped"},
        "dstack_idle_policy_invalid",
    )
    if lifecycle_version == 1:
        _require(
            type(hard_deadline) in (int, float)
            and math.isfinite(hard_deadline)
            and hard_deadline >= 0
            and keep_on == "idle"
            and budget_available,
            "dstack_idle_policy_invalid",
        )
    else:
        _require(hard_deadline is None, "dstack_idle_policy_invalid")
    if not observations_fresh:
        return "reconcile"
    if lifecycle_version == 2:
        obligations = active_jobs or unsafe_attempts or collection_holds
        if not budget_available:
            return "drain" if obligations else "stop"
        if obligations or keep_on == "until_stopped" or now < hold_until:
            return "retain"
        # Expiring a hold begins a full idle interval; it must not immediately
        # stop a machine whose previous activity predates the hold.
        last_activity = max(last_business_activity, hold_until)
        return "stop" if now - last_activity >= idle_seconds else "retain"
    if active_jobs or unsafe_attempts or collection_holds:
        return "drain" if now >= hard_deadline else "retain"
    if now >= hard_deadline:
        return "stop"
    if now < hold_until:
        return "retain"
    return "stop" if now - last_business_activity >= idle_seconds else "retain"


# Preserve the original public import and pickle identity of moved definitions.
assert_run_owned.__module__ = "studio_platform.dstack_capacity"
_range.__module__ = "studio_platform.dstack_capacity"
_resources.__module__ = "studio_platform.dstack_capacity"
terminal_no_allocation_shape.__module__ = "studio_platform.dstack_capacity"
terminal_no_allocation.__module__ = "studio_platform.dstack_capacity"
live_capacity_usage.__module__ = "studio_platform.dstack_capacity"
idle_action.__module__ = "studio_platform.dstack_capacity"
