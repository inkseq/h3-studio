"""Allowlisted admin inventory over existing readers; never adopt or call providers."""

import copy
from dataclasses import fields
import hashlib
from pathlib import Path

from sqlalchemy import select

from . import capacity_settings as capacity
from .repository import outbox, request_hash


def unit(key, value):
    if key.endswith("microusd"):
        return "micro_usd"
    if key.endswith(("seconds", "timeout")):
        return "seconds"
    if key.endswith("credits"):
        return "credits"
    if key.endswith("cents"):
        return "usd_cents"
    if type(value) is bool:
        return "boolean"
    return "configuration" if isinstance(value, (dict, list)) else "count_or_value"


def item(
    group,
    key,
    value,
    source,
    *,
    revision=None,
    effective=True,
    edit_path=None,
    latest_change=None,
    **extra,
):
    return {
        "id": group + "." + key,
        "group": group,
        "value": copy.deepcopy(value),
        "unit": unit(key, value),
        "source": source,
        "revision": revision,
        "effective": effective,
        "editable": edit_path is not None,
        "edit_path": edit_path,
        "latest_change": copy.deepcopy(latest_change),
        **extra,
    }


def latest(connection, tenant, event, aggregate=None):
    where = [
        outbox.c.event_type == event,
        outbox.c.payload["tenant_id"].as_string() == tenant,
    ]
    if aggregate is not None:
        where.append(outbox.c.aggregate_id == aggregate)
    row = (
        connection.execute(
            select(outbox)
            .where(*where)
            .order_by(outbox.c.created_at.desc(), outbox.c.id.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return {
        "actor_id": row["payload"].get("actor_id"),
        "observed_at": row["created_at"],
        "reason": row["payload"].get("reason"),
    }


def capacity_items(stored, changes):
    metadata = capacity.metadata(stored)
    return [
        item(
            "capacity",
            key,
            value,
            "database:spending_settings"
            if stored["version"]
            else "runtime_default:spending_settings",
            revision=stored["version"],
            latest_change=changes.get(key),
            edit_path="/v1/operator/admin/spending-settings",
            effective=not metadata["controls"]
            .get(key, {})
            .get("pending_implementation", False),
            adopted=metadata["adopted"],
        )
        for key, value in sorted(stored["value"].items())
        if key in capacity.DEFAULTS
    ]


def start_items(app, connection):
    from .capacity_start_rules import StartRules, EVENT
    from .capacity_lookahead_runtime import enabled
    from .dstack_workspace import DstackWorkspace

    service = getattr(app.state, "dstack_operator", None)
    if service is None:
        return [], ["start_rules:runtime_unconfigured"]
    rules = StartRules(DstackWorkspace(service))
    rows = []
    for profile in sorted(service.config.get("profiles", []), key=lambda p: p["id"]):
        identity, binding = rules._target(profile["id"])
        stored, _ = rules._load(connection, identity, binding)
        change = latest(connection, rules.tenant, EVENT, identity)
        rows.extend(
            item(
                "start_rules",
                profile["id"] + "." + key,
                value,
                "database:operator_policy"
                if stored["version"]
                else "code:capacity_start_rules.DEFAULTS",
                revision=stored["revision"],
                effective=enabled(service),
                edit_path="/v1/operator/dstack/admin/start-rules",
                latest_change=change,
                profile_id=profile["id"],
                field=key,
                unit=unit(key, value),
            )
            for key, value in sorted(stored["value"].items())
        )
    return rows, []


def credit_items(connection, tenant):
    from .credits import CreditError, credit_policy
    from .credits_pricing import PricingError, current
    from .credit_schema import prices
    from .payments_routes import BillingError, current_packs

    rows, unavailable = [], []
    try:
        policy, price = credit_policy(connection, tenant), current(connection, tenant)
        change = connection.execute(
            select(prices.c.published_by, prices.c.published_at).where(
                prices.c.tenant == tenant, prices.c.revision == price["revision"]
            )
        ).first()
        audit = (
            None
            if change is None
            else {
                "actor_id": change.published_by,
                "observed_at": change.published_at,
                "reason": None,
            }
        )
        rows.append(
            item(
                "credits",
                "charging_enabled",
                policy["enabled"],
                "database:credit_policy",
                revision=policy["active_revision"],
            )
        )
        values = {"rates": price["rates"], **price["configuration"]}
        allowed = {
            "rates",
            "welcome_credits",
            "image_credits",
            "resolution_multipliers",
            "expiry_policy",
            "fee_rate",
            "consumer_tax_rate",
            "quality_multipliers",
            "cost_multiplier",
            "credit_usd",
            "basis",
            "fee_rate_source",
        }
        rows.extend(
            item(
                "credits",
                key,
                values[key],
                "database:credit_prices"
                if price["revision"]
                else "code:credits_pricing.DEFAULT_CONFIGURATION",
                revision=price["revision"],
                latest_change=audit,
                effective=price["revision"] is not None,
                edit_path="/v1/admin/credits/prices",
            )
            for key in sorted(values.keys() & allowed)
        )
    except (CreditError, PricingError) as error:
        unavailable.append("credits:" + error.code)
    try:
        catalog = current_packs(connection, tenant)
        for pack in catalog["packs"]:
            for key in ("amount_cents", "credits", "bonus_percent"):
                rows.append(
                    item(
                        "packs",
                        pack["id"] + "." + key,
                        pack[key],
                        "database:billing_packs",
                        revision=catalog["revision"],
                        edit_path="/v1/admin/billing/packs",
                        pack_id=pack["id"],
                        field=key,
                        unit=unit(key, pack[key]),
                    )
                )
        if catalog["revision"] is None:
            unavailable.append("packs:catalog_unpublished")
    except BillingError as error:
        unavailable.append("packs:" + error.code)
    return rows, unavailable


def model_items(settings, now):
    from .execution_profiles import read_profiles
    from .execution_policy import authority_current
    from .h3_profile_support import limits

    path = settings.execution_profiles_file
    if path is None:
        return [], ["model_limits:host_unconfigured"]
    try:
        path = Path(path)
        before = path.stat()
        policies = read_profiles(path)
        raw = path.read_bytes()
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            return [], ["model_limits:host_changed_during_read"]
        digest = hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError):
        return [], ["model_limits:host_unavailable"]
    rows = []
    for (profile, recipe), policy in sorted(policies.items()):
        mode = "ref" if "ref2va" in recipe else "fl"
        group = profile + "." + mode
        rows.append(
            item(
                "model_limits",
                group + ".resource_envelope",
                policy["envelope"],
                "host:execution_profiles_file",
                revision=policy["revision"],
                effective=bool(policy["enabled"] and authority_current(policy, now)),
                file_sha256=digest,
                profile_id=profile,
                mode=mode,
            )
        )
        rows.append(
            item(
                "model_limits",
                group + ".reference_limits",
                limits(mode, profile),
                "code:h3_profile_support.limits",
                revision=request_hash(limits(mode, profile)),
                profile_id=profile,
                mode=mode,
            )
        )
    return rows, []


def switch_items(app, connection):
    from .content_policy import public_policy
    from .story_chat_settings import read
    from .modal_host import HostSpec

    settings, tenant = app.state.settings, app.state.settings.tenant_id
    content, story = public_policy(connection, tenant), read(connection, tenant)
    rows = [
        item(
            "content",
            "policy",
            content,
            "database:content_policy"
            if content["revision"]
            else "code:content_policy.DEFAULT_POLICY",
            revision=content["revision"],
            effective=content["revision"] is not None,
            edit_path="/v1/admin/content-policy",
        ),
        item(
            "story_chat",
            "pictures_enabled",
            story["pictures_enabled"],
            "database:story_chat_settings"
            if story["version"]
            else "code:story_chat_settings.DEFAULT",
            revision=story["version"],
            edit_path="/v1/admin/story-chat/settings",
            latest_change=None
            if story["updated_at"] is None
            else {"actor_id": None, "observed_at": story["updated_at"], "reason": None},
        ),
        item(
            "runtime",
            "stripe_mode",
            settings.stripe_mode,
            "runtime:settings.stripe_mode",
        ),
        item(
            "runtime",
            "generation_enabled",
            settings.generation_enabled,
            "runtime:settings.generation_enabled",
        ),
        item(
            "runtime",
            "startup_timeout_seconds",
            settings.capacity_startup_timeout_seconds,
            "runtime:settings.capacity_startup_timeout_seconds",
        ),
    ]
    # The app cannot attest the controller's separately mounted modal.json.
    # Label dataclass defaults as defaults, never as an active host observation.
    allowed = {
        "memory_mib",
        "cpu_limit",
        "max_containers",
        "max_inputs",
        "idle_timeout",
    }
    rows.extend(
        item(
            "modal_defaults",
            field.name,
            field.default,
            "code:modal_host.HostSpec",
            effective=False,
        )
        for field in fields(HostSpec)
        if field.name in allowed
    )
    return rows, ["modal_runtime:controller_readback_required"]


def build(app, connection, stored, changes):
    rows, unavailable = capacity_items(stored, changes), []
    for read in (
        lambda: start_items(app, connection),
        lambda: credit_items(connection, app.state.settings.tenant_id),
        lambda: model_items(app.state.settings, app.state.repository.clock()),
        lambda: switch_items(app, connection),
    ):
        found, missing = read()
        rows.extend(found)
        unavailable.extend(missing)
    present = {row["id"] for row in rows}
    unavailable.extend(
        "pending:" + field
        for field in (
            "first_video_max_seconds",
            "next_video_interval_seconds",
            "modal_trigger_seconds",
            "per_user_queued_shots",
            "fair_share",
            "member_priority",
            "membership_plans",
            "signup_mode",
            "busy_mode",
            "assistant_daily_limits",
        )
        if "capacity." + field not in present
    )
    return {
        "observed_at": app.state.repository.clock(),
        "items": rows,
        "unavailable": sorted(set(unavailable)),
    }
