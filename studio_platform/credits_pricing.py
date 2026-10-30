"""Immutable customer prices and server-compiled per-job credit quotes."""

import copy
from fractions import Fraction
import math

from sqlalchemy import select

from comfy_workflow import FPS

from .credit_schema import CreditSchemaError, prices, ready
from .identity_schema import users

DEFAULT_RATES = {"fast": 2, "standard": 4, "refine": 12}
# Runtime profiles that cost more GPU time than their tier's rate covers. Users
# see only the larger usage, never a per-clip price (owner 2026-10-30): Pika's
# accelerator measured about +30% GPU time against TaoMate on the same runtime.
PROFILE_CREDIT_MULTIPLIERS = {
    "h3-pruned-rank8-int8-sage2-cu130-wgp9cb2-pika6-v9": Fraction(13, 10),
}
RECOMMENDED_IMAGE_CREDITS = 5
DEFAULT_CONFIGURATION = {
    "cost_multiplier": 3,
    "fee_rate": 0.07,
    "fee_rate_source": "estimated_mor_ceiling_not_actual_fee",
    "credit_usd": 0.01,
    "welcome_credits": 0,
    "basis": {
        "fast": {"gpu": "RTX 5090", "seconds_per_5s_clip": 29, "hourly_usd": None},
        "standard": {"gpu": "RTX 5090", "seconds_per_5s_clip": 87, "hourly_usd": None},
        "refine": {"gpu": "H200", "seconds_per_5s_clip": 217, "hourly_usd": None},
    },
}


class PricingError(Exception):
    def __init__(self, code, detail, status=422):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(code)


def require_ready(connection):
    try:
        if not ready(connection):
            raise PricingError("credits_not_ready", "Credits are not ready.", 503)
    except CreditSchemaError:
        raise PricingError("credits_not_ready", "Credits are not ready.", 503) from None


def rates(value):
    if (
        type(value) is not dict
        or set(value) != set(DEFAULT_RATES)
        or any(
            type(rate) is not int or not 1 <= rate <= 1_000_000
            for rate in value.values()
        )
    ):
        raise PricingError(
            "credit_prices_invalid", "Provide positive integer rates for all tiers."
        )
    return dict(value)


def _number(value, minimum, maximum):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and minimum <= value <= maximum
    )


def configuration(value):
    if (
        type(value) is not dict
        or not set(DEFAULT_CONFIGURATION) <= set(value)
        or set(value)
        - set(DEFAULT_CONFIGURATION)
        - {
            "image_credits",
            "resolution_multipliers",
            "expiry_policy",
            "reference_video_surcharge_bps_per_second",
        }
        or (
            "image_credits" in value
            and (
                type(value["image_credits"]) is not int
                or not 1 <= value["image_credits"] <= 1_000_000
            )
        )
        or not _number(value["cost_multiplier"], 0, 1000)
        or not _number(value["fee_rate"], 0, 1)
        or value["credit_usd"] != 0.01
        or type(value["welcome_credits"]) is not int
        or not 0 <= value["welcome_credits"] <= 1_000_000_000
        or value["fee_rate_source"] != DEFAULT_CONFIGURATION["fee_rate_source"]
        or value["basis"] != DEFAULT_CONFIGURATION["basis"]
    ):
        raise PricingError(
            "credit_prices_invalid", "Credit price configuration is invalid."
        )
    if "resolution_multipliers" in value:
        multipliers = value["resolution_multipliers"]
        if (
            type(multipliers) is not dict
            or set(multipliers) != {"480P", "768P"}
            or any(
                type(n) is not int or not 1 <= n <= 100 for n in multipliers.values()
            )
            or multipliers["768P"] < multipliers["480P"]
        ):
            raise PricingError(
                "credit_prices_invalid", "Resolution prices are invalid."
            )
    if "expiry_policy" in value:
        from .credit_lots import POLICY

        if (
            type(value["expiry_policy"]) is not dict
            or value["expiry_policy"] != POLICY
            or any(type(n) is not int for n in value["expiry_policy"].values())
        ):
            raise PricingError(
                "credit_prices_invalid", "Credit expiry policy is invalid."
            )
    from .reference_video_pricing import validate

    validate(value)
    return copy.deepcopy(value)


def resolution_price(config, output):
    """Apply only an adopted revision to trusted compiled dimensions, not labels."""
    multipliers = config.get("resolution_multipliers")
    if multipliers is None:
        return 1, {}
    width, height = output.get("width"), output.get("height")
    if (
        type(width) is not int
        or type(height) is not int
        or min(width, height) <= 0
        or min(width, height) > 768
    ):
        raise PricingError(
            "credit_quote_invalid", "Compiled resolution is unavailable."
        )
    resolution = "480P" if min(width, height) <= 480 else "768P"
    multiplier = multipliers[resolution]
    return multiplier, {"resolution": resolution, "resolution_multiplier": multiplier}


def current(connection, tenant):
    require_ready(connection)
    from .credits import credit_policy

    policy = credit_policy(connection, tenant)
    revision = policy["active_revision"]
    if revision is None:
        if policy["enabled"]:
            raise PricingError(
                "credit_prices_unavailable", "Credit prices are unavailable.", 503
            )
        return {
            "revision": None,
            "rates": dict(DEFAULT_RATES),
            "configuration": copy.deepcopy(DEFAULT_CONFIGURATION),
        }
    row = (
        connection.execute(
            select(prices).where(
                prices.c.tenant == tenant, prices.c.revision == revision
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise PricingError(
            "credit_prices_unavailable", "Credit prices are unavailable.", 503
        )
    return {
        "revision": revision,
        "rates": rates(row["rates"]),
        "configuration": configuration(row["configuration"]),
    }


def public_prices(connection, tenant):
    value = current(connection, tenant)
    result = {key: value[key] for key in ("revision", "rates")}
    if "resolution_multipliers" in value["configuration"]:
        result["resolution_multipliers"] = copy.deepcopy(
            value["configuration"]["resolution_multipliers"]
        )
    if "expiry_policy" in value["configuration"]:
        result["expiry_policy"] = copy.deepcopy(value["configuration"]["expiry_policy"])
    from .reference_video_pricing import FIELD

    if FIELD in value["configuration"]:
        result[FIELD] = copy.deepcopy(value["configuration"][FIELD])
    return result


def image_price(connection, tenant):
    """An explicit audited image price, never a default adopted by a read.

    Legacy immutable configurations are deliberately left without this field.
    The image route must refuse a charged request when this returns None while
    charging is enabled; None is not authority to generate for free.
    """
    from .credits import credit_policy

    if not credit_policy(connection, tenant)["enabled"]:
        return None
    price = current(connection, tenant)
    amount = price["configuration"].get("image_credits")
    if amount is None or price["revision"] is None:
        return None
    return {"credits": amount, "price_revision": price["revision"]}


def admin_margin_estimate(price, catalog):
    """Tax-exclusive pack estimate, never a fee invoice or customer charge.

    Pack credits already INCLUDE the published bonus. Existing immutable price
    configurations and accepted quotes are not rewritten by this projection.
    Missing measured GPU prices remain unknown, with useful break-even bounds.
    """
    config = price["configuration"]
    tax = Fraction(0)
    fee = max(Fraction(7, 100), Fraction(str(config["fee_rate"])))
    fixed_fee = Fraction(1, 5)
    packs = []
    for pack in catalog["packs"]:
        gross = Fraction(pack["amount_cents"], 100)
        net = gross / (1 + tax) - gross * fee - fixed_fee
        packs.append(
            {
                "pack_id": pack["id"],
                "amount_cents": pack["amount_cents"],
                "credits": pack["credits"],
                "bonus_percent": pack["bonus_percent"],
                "estimated_net_usd": float(net),
                "estimated_net_usd_per_credit": float(net / pack["credits"]),
            }
        )
    worst = min(
        packs,
        key=lambda item: (item["estimated_net_usd_per_credit"], item["pack_id"]),
        default=None,
    )
    net_credit = Fraction(str(worst["estimated_net_usd_per_credit"])) if worst else None
    tiers = {}
    for tier, rate in price["rates"].items():
        basis = config["basis"][tier]
        revenue = net_credit * rate * 5 if net_credit is not None else None
        hourly = basis["hourly_usd"]
        multiplier = Fraction(str(config["cost_multiplier"]))
        duration = basis["seconds_per_5s_clip"]
        cost = (
            Fraction(str(hourly)) * duration * multiplier / 3600
            if hourly is not None
            else None
        )
        margin = (
            (revenue - cost) / revenue
            if revenue is not None and revenue > 0 and cost is not None
            else None
        )
        break_even = (
            revenue * 3600 / (duration * multiplier)
            if revenue is not None and revenue > 0 and multiplier > 0
            else None
        )
        tiers[tier] = {
            "credits_per_5s_clip": rate * 5,
            "estimated_net_usd_per_5s_clip": float(revenue)
            if revenue is not None
            else None,
            "estimated_cost_usd_per_5s_clip": float(cost) if cost is not None else None,
            "estimated_margin": float(margin) if margin is not None else None,
            "below_30_percent": margin < Fraction(3, 10)
            if margin is not None
            else None,
            "break_even_gpu_hourly_usd": float(break_even)
            if break_even is not None
            else None,
            "max_gpu_hourly_usd_for_30_percent": float(break_even * Fraction(7, 10))
            if break_even is not None
            else None,
            "status": "catalog_unconfigured"
            if not worst
            else "non_positive_net"
            if revenue <= 0
            else "gpu_cost_unmeasured"
            if hourly is None
            else "estimated",
        }
    return {
        "estimated": True,
        "currency": "usd",
        "pack_revision": catalog["revision"],
        "assumptions": {
            "tax_inclusive": False,
            "consumer_tax_rate": float(tax),
            "processing_and_mor_fee_rate": float(fee),
            "fixed_fee_usd_per_pack": float(fixed_fee),
            "source": "owner_CR74_exclusive_estimated_fees_not_actual_invoice",
            "pack_credits_include_bonus": True,
        },
        "packs": packs,
        "worst_case_pack_id": worst["pack_id"] if worst else None,
        "worst_case_net_usd_per_credit": float(net_credit)
        if net_credit is not None
        else None,
        "tiers": tiers,
    }


def public_quote(execution):
    value = execution.get("credit_quote")
    if not isinstance(value, dict) or value.get("enforced") is not True:
        return {"admission": "usd"}
    return {
        **{key: value[key] for key in ("credits", "price_revision", "breakdown")},
        "admission": "credits" if value.get("version") == 2 else "usd",
    }


def aggregate_quotes(plans, *, complete=True):
    """Pure projection: sum actual per-job quotes, never multiply an offer/take."""
    quoted = [plan for plan in plans if plan and "price_revision" in plan]
    if not quoted:
        return {}
    known = (
        complete
        and len(quoted) == len(plans)
        and all(
            type(plan.get("credits")) is int and plan["credits"] >= 0 for plan in quoted
        )
    )
    revisions = sorted({plan["price_revision"] for plan in quoted})
    return {
        "admission": "credits"
        if all(plan.get("admission") == "credits" for plan in quoted)
        else "usd",
        "credits": sum(plan["credits"] for plan in quoted) if known else None,
        "price_revision": revisions[0] if len(revisions) == 1 else None,
        "price_revisions": revisions,
        "breakdown": [item for plan in quoted for item in plan.get("breakdown", [])],
    }


def get_tier(compiled, *, execution=None):
    """Classify a fresh server compilation, never caller labels or step counts."""
    from .quality_tiers import billing_tier, model_tier, profile_tier
    from .render_plans import RECIPE as RENDER_RECIPE

    if (
        compiled.get("recipe_id") == RENDER_RECIPE
        and isinstance(execution, dict)
        and execution.get("backend") == "cpu-render"
    ):
        return "render"
    request = compiled.get("request", {})
    tier = model_tier(request.get("model"))
    identity = compiled.get("deployment_profile_id")
    if identity is not None:
        from .runtime_catalog import get_profile

        if billing_tier(profile_tier(identity)) != tier:
            raise PricingError(
                "credit_quote_invalid", "Compiled quality is unavailable."
            )
        try:
            matches = get_profile(identity)["model_id"] == request.get("model")
        except ValueError:
            matches = False
        if not matches:
            raise PricingError(
                "credit_quote_invalid", "Compiled quality is unavailable."
            )
    if tier is None:
        raise PricingError("credit_quote_invalid", "Compiled quality is unavailable.")
    return tier


def quote(connection, tenant, owner, compiled, *, execution=None):
    """Quote accepted server compilation, never a client tier or take multiplier."""
    from .credits import credit_policy

    if not credit_policy(connection, tenant)["enabled"]:
        return None
    price = current(connection, tenant)
    user = connection.execute(
        select(users.c.id, users.c.status).where(
            users.c.tenant == tenant, users.c.owner == owner
        )
    ).first()
    if user is None or user.status != "active":
        raise PricingError("account_unavailable", "The account is unavailable.", 403)
    from .render_plans import RECIPE as RENDER_RECIPE

    render = (
        compiled.get("recipe_id") == RENDER_RECIPE
        and isinstance(execution, dict)
        and execution.get("backend") == "cpu-render"
    )
    from . import enhance_plans

    enhance = (
        compiled.get("recipe_id") == enhance_plans.RECIPE
        and isinstance(execution, dict)
        and execution.get("backend") == enhance_plans.BACKEND
    )
    output = compiled.get("output_spec", {})
    # CPU chapter/Film compilers use frame_count; generation still requires
    # its original frames contract, even when a caller supplies a render tier.
    frame_key, other_key = (
        ("frame_count", "frames") if render or enhance else ("frames", "frame_count")
    )
    frames, fps = output.get(frame_key), output.get("fps", FPS)
    if (
        type(frames) is not int
        or not 1 <= frames <= 10_000_000
        or type(fps) is not int
        or not 1 <= fps <= 240
        or (
            other_key in output
            and (type(output[other_key]) is not int or output[other_key] != frames)
        )
    ):
        raise PricingError(
            "credit_quote_invalid", "Compiled output duration is unavailable."
        )
    if enhance:
        # Owner 2026-10-30 (#795): fixed credits per clip, then per extra second.
        tier = "enhance-" + compiled.get("request", {}).get("tier", "")
        if tier[len("enhance-") :] not in enhance_plans.PRICES:
            raise PricingError("credit_quote_invalid", "Enhance tier is unavailable.")
    else:
        tier = get_tier(compiled, execution=execution)
    multiplier, resolution = (
        (1, {})
        if render or enhance
        else resolution_price(price["configuration"], output)
    )
    rate = 0 if render or enhance else price["rates"][tier] * multiplier
    from .reference_video_pricing import price as reference_price

    amount, reference = (
        (0, {})
        if render
        else (enhance_plans.credits(tier[len("enhance-") :], frames / fps), {})
        if enhance
        else reference_price(
            price["configuration"],
            compiled,
            Fraction(rate * frames, fps)
            * PROFILE_CREDIT_MULTIPLIERS.get(compiled.get("deployment_profile_id"), 1),
            resolution.get("resolution"),
        )
    )
    seconds = frames / fps
    result = {
        "version": 1,
        "enforced": True,
        "user_id": user.id,
        "credits": amount,
        "price_revision": price["revision"],
        "tier": tier,
        "output_seconds": seconds,
        "frames": frames,
        "fps": fps,
        "takes": 1,
        **resolution,
        **reference,
        "breakdown": [
            {
                "tier": tier,
                "rate": rate,
                "output_seconds": seconds,
                "frames": frames,
                "fps": fps,
                "credits": amount,
                **resolution,
                **reference,
            }
        ],
    }
    from .credit_admission import ready, roles

    if ready(connection) and amount > 0:
        result.update(
            version=2,
            admission="credits",
            budget_rows=roles(connection, tenant, owner, execution or {}),
        )
    return result


def hide_dollar_estimate(execution, principal):
    return "credits" in public_quote(execution) and (
        principal is None or principal.machine or principal.role != "admin"
    )


def user_estimate(value, *, hide_dollars):
    if not hide_dollars:
        return value
    return {
        key: item
        for key, item in value.items()
        if "usd" not in key.lower() and "cost" not in key.lower()
    }


def user_result(value, *, hide_dollars):
    """Remove provider currency fields from display only, preserving stored facts."""
    if not hide_dollars:
        return value
    if isinstance(value, dict):
        return {
            key: user_result(item, hide_dollars=True)
            for key, item in value.items()
            if 'usd' not in key.lower()
        }
    if isinstance(value, list):
        return [user_result(item, hide_dollars=True) for item in value]
    return value
