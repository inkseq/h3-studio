"""Model-first, read-only whole-allocation discovery from normalized stock.

Discovery does not qualify a runtime or authorize rental. Exact-offer admission
reuses the same checks against fresh observations and the protected registry.
"""

from __future__ import annotations

import copy
import math

from sqlalchemy import select

from .capacity_inventory import allocation_resources
from .capacity_memory import planning_ram_gib
from .capacity_market import (
    FRESH_SECONDS,
    PROVIDERS,
    ROW_FIELDS,
    _constraints,
    market_inventory,
    refresh_projection,
)
from .repository import paused_capacity_pools


def _require(condition, code, status=409):
    # OperatorCapacity imports this module only at the admission boundary.
    from .operator_capacity import OperatorError

    if not condition:
        raise OperatorError(code, status)


def _profiles(registry, model_id, mode, ttl_seconds):
    _require(
        isinstance(model_id, str) and 1 <= len(model_id) <= 200,
        "operator_model_invalid",
        422,
    )
    _require(mode in ("fl", "ref"), "operator_mode_invalid", 422)
    _require(
        type(ttl_seconds) is int and 120 <= ttl_seconds <= 14400,
        "operator_selection_limits_invalid",
        422,
    )
    profiles = [
        profile
        for profile in registry.catalog().get("profiles", [])
        if profile["model_id"] == model_id
        and any(model["mode"] == mode for model in profile["models"])
    ]
    _require(profiles, "operator_model_unavailable", 422)
    return profiles


def _observations(connection, now):
    stored = {
        row["provider"]: row
        for row in connection.execute(select(market_inventory)).mappings()
    }
    providers, rows = [], []
    for provider in PROVIDERS:
        record = stored.get(provider)
        observed, status, reason = (
            None,
            "unconfigured",
            "inventory_scanner_not_configured",
        )
        if record:
            observed = record["observed_at"]
            if not 0 <= now - observed <= FRESH_SECONDS:
                status, reason = "stale", "inventory_scan_stale"
            else:
                payload = record["payload"]
                status = payload["status"]
                reason = None if status == "ok" else "inventory_scan_failed"
                if status == "ok":
                    offers = payload["offers"]
                    identities = [offer["offer_id"] for offer in offers]
                    if len(set(identities)) != len(identities):
                        status, reason = "error", "inventory_offer_ambiguous"
                    else:
                        rows.extend(
                            (
                                {
                                    key: copy.deepcopy(value)
                                    for key, value in offer.items()
                                    if key in ROW_FIELDS
                                },
                                observed,
                            )
                            for offer in offers
                        )
        providers.append(
            dict(
                provider=provider,
                status=status,
                observed_at=observed,
                reason_code=reason,
                **refresh_projection(record, now),
            )
        )
    return providers, rows


def _physical_limits(profile):
    return {
        "min_ram_gib": planning_ram_gib(
            profile.get("id"), math.ceil(profile["minimum_ram_bytes"] / 1024**3)
        ),
        "min_disk_gib": math.ceil(profile["minimum_disk_bytes"] / 1024**3),
        "min_cpu_cores": profile["hardware_filters"]["minimum_cpu_cores"],
    }


def _filter_projection(registry, profile, mode):
    """Safe effective policy, distinct from secrets or runtime qualification."""
    deployments = []
    for binding in registry.bindings.values():
        if (
            binding.runtime_profile_id != profile["id"]
            or binding.mode != mode
            or binding.model_id != profile["model_id"]
        ):
            continue
        deployments.append(
            {
                "provider": binding.launch.provider,
                "gpu_type": binding.gpu_type,
                "gpu_count": binding.gpu_count,
                "binding_id": binding.binding_id,
                "enabled": binding.enabled,
                "source": "protected_operator_registry",
                "filters": copy.deepcopy(binding.filters),
                "hourly_cost_ceiling_microusd": binding.hourly_cost_microusd,
            }
        )
    deployments.sort(
        key=lambda item: (
            item["provider"],
            item["gpu_type"],
            item["gpu_count"],
            item["binding_id"],
        )
    )
    hardware = profile["hardware_filters"]
    return {
        "runtime_profile_id": profile["id"],
        "model_id": profile["model_id"],
        "mode": mode,
        "source": "runtime_profile",
        "hard_requirements": _physical_limits(profile),
        "guidance": {
            "min_download_mbps": hardware["minimum_download_mbps"],
            "max_price_per_gpu_hour_microusd": hardware[
                "maximum_price_per_gpu_hour_microusd"
            ],
        },
        "deployments": deployments,
    }


def _verified_constraints(row, filters):
    row = allocation_resources(row)
    blockers = _constraints(row, filters)
    for limit, field, label in (
        ("min_ram_gib", "ram_gib", "ram"),
        ("min_disk_gib", "disk_gib", "disk"),
        ("min_cpu_cores", "cpu_cores", "cpu"),
        ("min_download_mbps", "download_mbps", "bandwidth"),
        ("max_price_per_gpu_hour_microusd", "price_per_gpu_hour_microusd", "price"),
        ("allowed_countries", "country", "country"),
    ):
        if filters.get(limit) and field in row.get("unverified_fields", []):
            blockers.append("inventory_unknown_" + label)
    return list(dict.fromkeys(blockers))


def _spec_blockers(profile, row):
    blockers = _verified_constraints(row, _physical_limits(profile))
    if (
        row.get("hourly_cost_microusd") is None
        or row.get("price_per_gpu_hour_microusd") is None
    ):
        blockers.append("inventory_unknown_price")
    for field, label in (
        ("hourly_cost_microusd", "price"),
        ("price_per_gpu_hour_microusd", "price"),
    ):
        if field in row.get("unverified_fields", []):
            blockers.append("inventory_unknown_" + label)
    return list(dict.fromkeys(blockers))


def _deployment(registry, profile, row, choice, now):
    blockers, slots = [], None
    if row["gpu_count"] not in profile["gpu_count_options"]:
        blockers.append("operator_topology_not_qualified")
    try:
        binding = registry.resolve(choice)
    except ValueError as error:
        code = getattr(error, "code", "operator_deployment_not_configured")
        blockers.append(
            code
            if code
            in {
                "operator_provider_start_unqualified",
                "operator_deployment_not_configured",
            }
            else "operator_deployment_not_qualified"
        )
        return blockers, slots
    if (
        binding.model_id != profile["model_id"]
        or not binding.enabled
        or binding.expires_at <= now + choice["ttl_seconds"]
    ):
        blockers.append("operator_deployment_not_qualified")
    # A Targon binding always approves a resource SKU. A nonempty Lium launch
    # ID is conservatively fixed to that executor; the approved dynamic Lium
    # runtime uses an empty ID and checks the chosen executor before its POST.
    if (
        row["provider"] == "targon" or binding.launch.offer_id
    ) and binding.launch.offer_id != row["offer_id"]:
        blockers.append("operator_offer_selection_mismatch")
    if row["kind"] != ("vm" if row["provider"] == "targon" else "executor"):
        blockers.append("operator_offer_selection_mismatch")
    if binding.execution_slots != row["gpu_count"]:
        blockers.append("operator_execution_slots_unqualified")
    if choice["ttl_seconds"] < binding.min_ttl_seconds:
        blockers.append("operator_ttl_below_provider_minimum")
    if choice["ttl_seconds"] > binding.max_ttl_seconds:
        blockers.append("operator_ttl_limit")
    blockers.extend(_verified_constraints(row, binding.filters))
    if (
        row.get("hourly_cost_microusd") is not None
        and row["hourly_cost_microusd"] > binding.hourly_cost_microusd
    ):
        blockers.append("inventory_price_above_limit")
    if not blockers:
        slots = binding.execution_slots
    return list(dict.fromkeys(blockers)), slots


def _candidate(registry, profile, row, observed, mode, ttl_seconds, now, paused_pools):
    choice = {
        "runtime_profile_id": profile["id"],
        "mode": mode,
        "provider": row["provider"],
        "gpu_type": row["gpu_type"],
        "gpu_count": row["gpu_count"],
        "node_count": 1,
        "ttl_seconds": ttl_seconds,
        "filters": {},
        "offer_id": row["offer_id"],
    }
    blockers = _spec_blockers(profile, row)
    deployment_blockers, slots = _deployment(registry, profile, row, choice, now)
    blockers = list(dict.fromkeys(blockers + deployment_blockers))
    # A paused execution pool retains its measured runtime qualification. Its
    # explicit operator ceiling, not temporary occupancy, blocks new starts.
    if not deployment_blockers and registry.resolve(choice).pool in paused_pools:
        blockers.append("operator_pool_paused")
    hardware, hints = profile["hardware_filters"], []
    if (
        row.get("price_per_gpu_hour_microusd") is not None
        and row["price_per_gpu_hour_microusd"]
        > hardware["maximum_price_per_gpu_hour_microusd"]
    ):
        hints.append("inventory_price_above_guidance")
    if row.get("download_mbps") is None:
        hints.append("inventory_bandwidth_unknown")
    elif row["download_mbps"] < hardware["minimum_download_mbps"]:
        hints.append("inventory_bandwidth_below_guidance")
    qualified = not blockers
    return {
        **copy.deepcopy(row),
        "observed_at": observed,
        "selection": choice,
        "allocation_ram_gib": allocation_resources(row).get("ram_gib"),
        "offer_kind": "executor" if row["provider"] == "lium" else "resource_sku",
        "execution_slots": slots,
        "blockers": blockers,
        "deployment_qualified": not deployment_blockers,
        "specs_confirmed": not any(
            code.startswith("inventory_unknown_") for code in blockers
        ),
        "qualification": "qualified" if qualified else "unqualified",
        "preference_hints": hints,
        "rank_reasons": [
            "deployment_qualified" if not deployment_blockers else "deployment_pending",
            "whole_allocation_price",
            "bandwidth_known"
            if row.get("download_mbps") is not None
            else "bandwidth_unknown",
        ],
    }


def candidates_projection(connection, registry, model_id, mode, ttl_seconds, now):
    """Keep model/precision exact; discover only explicit catalog GPU names."""
    profiles = _profiles(registry, model_id, mode, ttl_seconds)
    providers, rows = _observations(connection, now)
    paused_pools = paused_capacity_pools(connection)
    known_gpus = {
        gpu
        for profile in registry.catalog().get("profiles", [])
        for gpu in profile["gpu_models"]
    }
    candidates, excluded, excluded_count, uncertain_allocation = [], [], 0, False

    def exclude(profile, row, observed, blockers):
        nonlocal excluded_count
        excluded_count += 1
        if len(excluded) < 100:
            excluded.append(
                {
                    **copy.deepcopy(row),
                    "observed_at": observed,
                    "runtime_profile_id": profile["id"],
                    "blockers": list(dict.fromkeys(blockers)),
                    "allocation_ram_gib": allocation_resources(row).get("ram_gib"),
                }
            )

    for row, observed in rows:
        compatible = [
            profile for profile in profiles if row["gpu_type"] in profile["gpu_models"]
        ]
        if not compatible:
            # A supplier's generic PRO label is not an approved GPU edition for
            # another model. Explain known H3 hardware instead of hiding stock.
            if row["gpu_type"] in known_gpus and row["available_count"] > 0:
                for profile in profiles:
                    exclude(
                        profile,
                        row,
                        observed,
                        ["operator_gpu_not_catalogued"] + _spec_blockers(profile, row),
                    )
            continue
        if not 1 <= row["gpu_count"] <= 8:
            continue
        for profile in compatible:
            physical = _spec_blockers(profile, row)
            if any(
                code in physical
                for code in (
                    "inventory_ram_below_minimum",
                    "inventory_disk_below_minimum",
                )
            ):
                exclude(profile, row, observed, physical)
                continue
            if row["available_count"] <= 0:
                # A partial executor is not an available whole allocation. Its
                # unknown slicing contract cannot supply a fabricated quote.
                uncertain_allocation |= "allocation" in row.get("unverified_fields", [])
                continue
            candidates.append(
                _candidate(
                    registry,
                    profile,
                    row,
                    observed,
                    mode,
                    ttl_seconds,
                    now,
                    paused_pools,
                )
            )
    candidates.sort(
        key=lambda row: (
            row["qualification"] != "qualified",
            row["hourly_cost_microusd"]
            if row.get("hourly_cost_microusd") is not None
            else math.inf,
            row.get("download_mbps") is None,
            -(row.get("download_mbps") or 0),
            row["provider"],
            row["offer_id"],
            row["selection"]["runtime_profile_id"],
        )
    )
    complete = all(provider["status"] == "ok" for provider in providers)
    reason = (
        "inventory_candidates_found"
        if candidates
        else "inventory_specs_unconfirmed"
        if uncertain_allocation
        else "inventory_no_matching_stock"
        if complete
        else "inventory_scan_unconfirmed"
    )
    status = "ok" if complete else "partial" if candidates else "unconfirmed"
    if uncertain_allocation and not candidates:
        status = "unconfirmed"
    return {
        "model_id": model_id,
        "mode": mode,
        "observed_at": now,
        "fresh_seconds": FRESH_SECONDS,
        "providers": providers,
        "candidates": candidates,
        "status": status,
        "filters": [
            _filter_projection(registry, profile, mode) for profile in profiles
        ],
        "excluded": excluded,
        "excluded_count": excluded_count,
        "reason_code": reason,
        "advisory_only": True,
    }


def resolve_selected_offer(connection, registry, chosen, now):
    """Return only trusted normalized stock after exact, fresh selection checks.

    The caller must still freeze its quote, enforce budgets and recheck the
    provider before deployment. This performs no network calls or writes.
    """
    _require(chosen.get("offer_id"), "operator_offer_required", 422)
    _require(chosen.get("node_count") == 1, "operator_offer_quantity_invalid", 422)
    profile = next(
        (
            item
            for item in registry.catalog().get("profiles", [])
            if item["id"] == chosen.get("runtime_profile_id")
        ),
        None,
    )
    _require(profile is not None, "operator_model_unavailable", 422)
    _profiles(
        registry, profile["model_id"], chosen.get("mode"), chosen.get("ttl_seconds")
    )
    _require(
        any(model["mode"] == chosen["mode"] for model in profile["models"]),
        "operator_mode_invalid",
        422,
    )
    providers, rows = _observations(connection, now)
    provider = next(
        (
            item
            for item in providers
            if item["provider"] == chosen.get("provider", "lium")
        ),
        None,
    )
    _require(
        provider and provider["status"] == "ok",
        "operator_offer_observation_unavailable",
    )
    matches = [
        row
        for row, _ in rows
        if row["provider"] == provider["provider"]
        and row["offer_id"] == chosen["offer_id"]
    ]
    _require(len(matches) == 1, "operator_offer_unavailable")
    row = matches[0]
    _require(
        row["gpu_type"] == chosen.get("gpu_type")
        and row["gpu_count"] == chosen.get("gpu_count")
        and row["gpu_type"] in profile["gpu_models"],
        "operator_offer_selection_mismatch",
    )
    _require(row["available_count"] > 0, "operator_offer_unavailable")
    blockers = (
        _spec_blockers(profile, row)
        + _deployment(registry, profile, row, chosen, now)[0]
    )
    _require(not blockers, blockers[0] if blockers else "operator_offer_unqualified")
    _require(
        registry.resolve(chosen).pool not in paused_capacity_pools(connection),
        "operator_pool_paused",
    )
    return copy.deepcopy(row)
