"""Advisory stock observations. Never admission proof or a rental authority.

The HTTP API reads this cache; a separate read-only scanner owns provider GETs.
No raw provider response, credentials, task data or lease state belong here.
"""

from __future__ import annotations

import copy
import math
import re
import uuid

from sqlalchemy import Column, Float, JSON, String, Table, insert, select, update

from .repository import metadata, request_hash
from .runtime_catalog import get_profile
from .capacity_memory import planning_ram_gib

PROVIDERS = ("lium", "targon")
FRESH_SECONDS = 120
REFRESH_TIMEOUT_SECONDS = 60
market_inventory = Table(
    "platform_capacity_market_observations",
    metadata,
    Column("provider", String(30), primary_key=True),
    Column("observed_at", Float, nullable=False),
    Column("payload", JSON, nullable=False),
)

ROW_FIELDS = {
    "offer_id",
    "provider",
    "gpu_type",
    "gpu_count",
    "available_count",
    "ram_gib",
    "disk_gib",
    "download_mbps",
    "country",
    "hourly_cost_microusd",
    "price_per_gpu_hour_microusd",
    "cpu_cores",
    "kind",
    "unverified_fields",
    "available_gpu_count",
    "min_gpu_count_for_rental",
}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _cache_lock(connection, provider):
    # Stock request/observation writes never acquire the paid capacity gate.
    if connection.dialect.name == "postgresql":
        lock_id = 685939796868749730 + PROVIDERS.index(provider)
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(" + str(lock_id) + ")")


def refresh_projection(record, now):
    """Request metadata does not make an old observation fresh."""
    payload = record["payload"] if record else {}
    requested = payload.get("refresh_requested_at")
    identity = payload.get("refresh_request_id")
    status, reason = "idle", None
    if (
        _number(requested)
        and isinstance(identity, str)
        and re.fullmatch(r"[0-9a-f]{32}", identity)
    ):
        observed = record["observed_at"]
        previous = payload.get("refresh_previous_observed_at", -1)
        advanced = previous == -1 or _number(previous) and observed > previous
        if requested <= observed <= now and advanced:
            status = "complete" if payload.get("status") == "ok" else "failed"
            reason = None if status == "complete" else "inventory_scan_failed"
        elif not 0 <= now - requested < REFRESH_TIMEOUT_SECONDS:
            status, reason = "timeout", "inventory_refresh_timeout"
        else:
            status = "pending"
    else:
        requested, identity = None, None
    return {
        "refresh_request_id": identity,
        "refresh_requested_at": requested,
        "refresh_status": status,
        "refresh_reason_code": reason,
    }


def request_refresh(repo):
    """Coalesce an advisory refresh in the existing cache, without new schema.

    The API writes only a wake-up marker. The controller owns both supplier GETs.
    No paid command, lease, budget, observation time or authorization changes.
    """
    now = repo.clock()
    if not _number(now):
        raise ValueError("inventory_invalid_clock")
    with repo.transaction() as connection:
        for provider in PROVIDERS:
            _cache_lock(connection, provider)
        stored = {
            row["provider"]: row
            for row in connection.execute(select(market_inventory)).mappings()
        }
        previous = [
            refresh_projection(stored.get(provider), now) for provider in PROVIDERS
        ]
        same_request = (
            previous[0]["refresh_request_id"] is not None
            and previous[0]["refresh_request_id"] == previous[1]["refresh_request_id"]
            and previous[0]["refresh_requested_at"]
            == previous[1]["refresh_requested_at"]
        )
        recent = same_request and 0 <= now - previous[0]["refresh_requested_at"] < 2
        pending = same_request and any(
            value["refresh_status"] == "pending" for value in previous
        )
        if recent or pending:
            return {
                "request_id": previous[0]["refresh_request_id"],
                "requested_at": previous[0]["refresh_requested_at"],
                "providers": list(PROVIDERS),
                "coalesced": True,
            }
        identity = uuid.uuid4().hex
        for provider in PROVIDERS:
            record = stored.get(provider)
            payload = (
                copy.deepcopy(record["payload"])
                if record
                else {
                    "provider": provider,
                    "status": "error",
                    "observed_at": 0,
                    "reason_code": "inventory_scanner_not_configured",
                    "offers": [],
                }
            )
            payload.update(
                refresh_request_id=identity,
                refresh_requested_at=now,
                refresh_previous_observed_at=record["observed_at"] if record else -1,
            )
            if record:
                connection.execute(
                    update(market_inventory)
                    .where(market_inventory.c.provider == provider)
                    .values(payload=payload)
                )
            else:
                connection.execute(
                    insert(market_inventory).values(
                        provider=provider, observed_at=0, payload=payload
                    )
                )
    return {
        "request_id": identity,
        "requested_at": now,
        "providers": list(PROVIDERS),
        "coalesced": False,
    }


def publish_observation(repo, observation):
    """Cache only the normalized contract; a failed refresh replaces success.

    Serialize cache writers independently of the capacity gate. An older
    response cannot overwrite a later observation. This touches no money/state.
    """
    provider = observation.get("provider")
    observed = observation.get("observed_at")
    if (
        provider not in PROVIDERS
        or not _number(observed)
        or observed > repo.clock() + 1
    ):
        raise ValueError("inventory_observation_invalid")
    status = observation.get("status")
    if status not in {"ok", "error"}:
        raise ValueError("inventory_observation_invalid")
    rows = observation.get("offers")
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError("inventory_observation_invalid")
    normalized = []
    for row in rows if status == "ok" else []:
        if not isinstance(row, dict) or row.get("provider") != provider:
            raise ValueError("inventory_observation_invalid")
        public = {
            key: copy.deepcopy(value) for key, value in row.items() if key in ROW_FIELDS
        }
        if (
            not isinstance(public.get("offer_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,200}", public["offer_id"])
            or not isinstance(public.get("gpu_type"), str)
            or not re.fullmatch(r"[A-Za-z0-9 ._()\-]{1,120}", public["gpu_type"])
        ):
            raise ValueError("inventory_observation_invalid")
        for key in ("gpu_count", "available_count"):
            if type(public.get(key)) is not int or not 0 <= public[key] <= 100000:
                raise ValueError("inventory_observation_invalid")
        for key in ("available_gpu_count", "min_gpu_count_for_rental"):
            if public.get(key) is not None and (
                type(public[key]) is not int or not 0 <= public[key] <= 100000
            ):
                raise ValueError("inventory_observation_invalid")
        for key in (
            "ram_gib",
            "disk_gib",
            "download_mbps",
            "hourly_cost_microusd",
            "price_per_gpu_hour_microusd",
            "cpu_cores",
        ):
            if public.get(key) is not None and not _number(public[key]):
                raise ValueError("inventory_observation_invalid")
        if public.get("country") is not None and not re.fullmatch(
            r"[A-Z]{2}", str(public["country"])
        ):
            raise ValueError("inventory_observation_invalid")
        if public.get("kind") not in {"vm", "bm", "executor"}:
            raise ValueError("inventory_observation_invalid")
        fields = public.get("unverified_fields", [])
        if not isinstance(fields, list) or any(
            x not in ROW_FIELDS | {"gpu_edition", "allocation", "vram_gib"}
            for x in fields
        ):
            raise ValueError("inventory_observation_invalid")
        normalized.append(public)
    payload = {
        "provider": provider,
        "status": status,
        "observed_at": observed,
        "reason_code": None if status == "ok" else "inventory_scan_failed",
        "offers": normalized,
    }
    with repo.transaction() as connection:
        # Read-only stock must work even before paid capacity is configured.
        # SQLite transaction() already uses BEGIN IMMEDIATE. PostgreSQL uses a
        # distinct per-provider cache lock, never the rental authority lock.
        _cache_lock(connection, provider)
        previous = (
            connection.execute(
                select(market_inventory).where(market_inventory.c.provider == provider)
            )
            .mappings()
            .first()
        )
        if previous and previous["observed_at"] > observed:
            return False
        if previous:
            for key in (
                "refresh_request_id",
                "refresh_requested_at",
                "refresh_previous_observed_at",
            ):
                if key in previous["payload"]:
                    payload[key] = previous["payload"][key]
        values = dict(observed_at=observed, payload=payload)
        if previous:
            connection.execute(
                update(market_inventory)
                .where(market_inventory.c.provider == provider)
                .values(**values)
            )
        else:
            connection.execute(
                insert(market_inventory).values(provider=provider, **values)
            )
    return True


def _filters(registry, chosen):
    if chosen.get("filters"):
        return chosen["filters"], "custom"
    try:
        binding = registry.resolve(chosen)
        return binding.filters, "deployment_binding"
    except ValueError:
        pass
    try:
        profile = get_profile(chosen["runtime_profile_id"])
    except ValueError:
        return None, "unknown"
    hardware = profile["hardware_filters"]
    return {
        "min_ram_gib": planning_ram_gib(
            profile["id"], math.ceil(profile["minimum_ram_bytes"] / 1024**3)
        ),
        "min_disk_gib": math.ceil(profile["minimum_disk_bytes"] / 1024**3),
        "min_download_mbps": hardware["minimum_download_mbps"],
        "max_price_per_gpu_hour_microusd": hardware[
            "maximum_price_per_gpu_hour_microusd"
        ],
        "allowed_countries": [],
    }, "profile_guidance"


def _constraints(row, filters):
    blockers = []
    for key, spec, label in (
        ("min_ram_gib", "ram_gib", "ram"),
        ("min_disk_gib", "disk_gib", "disk"),
        ("min_download_mbps", "download_mbps", "bandwidth"),
        ("min_cpu_cores", "cpu_cores", "cpu"),
    ):
        if filters.get(key) is not None:
            if row.get(spec) is None:
                blockers.append("inventory_unknown_" + label)
            elif row[spec] < filters[key]:
                blockers.append("inventory_" + label + "_below_minimum")
    if filters.get("max_price_per_gpu_hour_microusd") is not None:
        price = row.get("price_per_gpu_hour_microusd")
        if price is None:
            blockers.append("inventory_unknown_price")
        elif price > filters["max_price_per_gpu_hour_microusd"]:
            blockers.append("inventory_price_above_limit")
    if filters.get("allowed_countries"):
        if row.get("country") is None:
            blockers.append("inventory_unknown_country")
        elif row["country"] not in filters["allowed_countries"]:
            blockers.append("inventory_country_mismatch")
    if "allocation" in row.get("unverified_fields", []):
        blockers.append("inventory_unknown_allocation")
    return blockers


def _group(row):
    return row["provider"], row["gpu_type"], row["gpu_count"]


def _available_counts(rows):
    counts = {}
    for row in rows:
        key = _group(row)
        counts[key] = counts.get(key, 0) + row["available_count"]
    return counts


def _candidate(row, registry, chosen, filters, now, matching_count):
    choice = {
        **copy.deepcopy(chosen),
        "provider": row["provider"],
        "gpu_type": row["gpu_type"],
    }
    blockers = _constraints(row, filters)
    # Quantity is shared by compatible hosts, but each row retains its own
    # availability and price. A blocked row cannot contribute qualified stock.
    if not blockers and matching_count < chosen["node_count"]:
        blockers.append("inventory_insufficient_quantity")
    try:
        binding = registry.resolve(choice)
        qualified = binding.enabled and binding.expires_at > now + chosen["ttl_seconds"]
        if not qualified:
            blockers.append("operator_deployment_not_qualified")
    except ValueError:
        qualified = False
        blockers.append(
            "operator_provider_start_unqualified"
            if row["provider"] == "targon"
            else "operator_deployment_not_configured"
        )
    return {
        **row,
        "selection": choice,
        "qualification": "qualified" if qualified and not blockers else "unqualified",
        "deployment_qualified": qualified,
        "specs_confirmed": not any(
            code.startswith("inventory_unknown_") for code in blockers
        ),
        "blockers": blockers,
    }


def market_projection(connection, registry, chosen, now):
    """Read cache only. Unknown evidence never proves that a 5090 is absent."""
    stored = {
        row["provider"]: row
        for row in connection.execute(select(market_inventory)).mappings()
    }
    providers, rows = [], {}
    for provider in PROVIDERS:
        record = stored.get(provider)
        status, observed = "unconfigured", None
        reason = "inventory_scanner_not_configured"
        if record:
            observed = record["observed_at"]
            if not 0 <= now - observed <= FRESH_SECONDS:
                status, reason = "stale", "inventory_scan_stale"
            else:
                status = record["payload"]["status"]
                reason = record["payload"]["reason_code"]
                if status == "ok":
                    rows[provider] = record["payload"]["offers"]
        providers.append(
            dict(
                provider=provider,
                status=status,
                reason_code=reason,
                observed_at=observed,
                **refresh_projection(record, now),
            )
        )
    filters, basis = _filters(registry, chosen)
    selected = chosen.get("provider", "lium")
    selected_rows = [
        row
        for row in rows.get(selected, [])
        if row["gpu_type"] == chosen["gpu_type"]
        and row["gpu_count"] == chosen["gpu_count"]
        and row["available_count"] > 0
    ]
    offers, recommendations = [], []
    reason = "inventory_scan_unconfirmed"
    status = "unconfirmed"
    if filters is not None:
        matching_counts = _available_counts(
            row
            for provider_rows in rows.values()
            for row in provider_rows
            if not _constraints(row, filters)
        )
        offers = [
            _candidate(
                row, registry, chosen, filters, now, matching_counts.get(_group(row), 0)
            )
            for row in selected_rows
        ]
    if selected in rows and filters is not None:
        known_rejections, uncertain, matches = [], [], []
        # gpu_count is the requested allocation, not necessarily host size.
        # A larger executor might split. Without verified slice resources/prices
        # we cannot quote it, but must not declare that provider out of 5090s.
        for row in rows[selected]:
            if (
                row["gpu_type"] == chosen["gpu_type"]
                and (
                    row["gpu_count"] > chosen["gpu_count"]
                    or row["gpu_count"] == chosen["gpu_count"]
                    and row["available_count"] == 0
                    and "allocation" in row.get("unverified_fields", [])
                )
                and row.get("available_gpu_count", 0) >= chosen["gpu_count"]
                and (
                    row.get("min_gpu_count_for_rental") is None
                    or row["min_gpu_count_for_rental"] <= chosen["gpu_count"]
                )
            ):
                uncertain.append(row)
        for row in selected_rows:
            limits = _constraints(row, filters)
            # Concrete rejection is enough to exclude; missing other specs do
            # not rehabilitate a row known to be too small/expensive.
            if any(not x.startswith("inventory_unknown_") for x in limits):
                known_rejections.append(row)
            elif limits:
                uncertain.append(row)
            else:
                matches.append(row)
        enough = sum(row["available_count"] for row in matches) >= chosen["node_count"]
        status = "ok" if all(x["status"] == "ok" for x in providers) else "partial"
        if enough:
            reason = "inventory_matches_found"
        elif uncertain:
            reason, status = "inventory_specs_unconfirmed", "unconfirmed"
        else:
            reason = "inventory_no_matching_stock"
            if chosen["gpu_type"] == "RTX 5090" and chosen["gpu_count"] == 1:
                advisory_rows = []
                for provider in PROVIDERS:
                    for row in rows.get(provider, []):
                        if (
                            "RTX PRO 6000 Blackwell" not in row["gpu_type"]
                            or row["gpu_count"] != 1
                            or row["available_count"] == 0
                        ):
                            continue
                        blockers = _constraints(row, filters)
                        # Price/bandwidth can be explicitly reviewed. A larger
                        # GPU cannot fix a known RAM/disk/country mismatch.
                        if any(
                            code in blockers
                            for code in (
                                "inventory_ram_below_minimum",
                                "inventory_disk_below_minimum",
                                "inventory_cpu_below_minimum",
                                "inventory_country_mismatch",
                                "inventory_unknown_allocation",
                            )
                        ):
                            continue
                        advisory_rows.append(row)
                advisory_counts = _available_counts(advisory_rows)
                recommendations = [
                    _candidate(
                        row,
                        registry,
                        chosen,
                        filters,
                        now,
                        matching_counts.get(_group(row), 0),
                    )
                    for row in advisory_rows
                    if advisory_counts[_group(row)] >= chosen["node_count"]
                ]
                recommendations.sort(
                    key=lambda x: (
                        PROVIDERS.index(x["provider"]),
                        x.get("hourly_cost_microusd")
                        if x.get("hourly_cost_microusd") is not None
                        else math.inf,
                        x["offer_id"],
                    )
                )
    return {
        "status": status,
        "observed_at": now,
        "fresh_seconds": FRESH_SECONDS,
        "selection": copy.deepcopy(chosen),
        "selection_hash": request_hash(chosen),
        "filters": filters,
        "filter_basis": basis,
        "providers": providers,
        "offers": offers,
        "recommendations": recommendations,
        "reason_code": reason,
        "advisory_only": True,
    }
