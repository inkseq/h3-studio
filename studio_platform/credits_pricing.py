"""Immutable customer prices and server-compiled per-job credit quotes."""

import copy
from fractions import Fraction
import math

from sqlalchemy import select

from comfy_workflow import FPS

from .credit_schema import CreditSchemaError, prices, ready
from .identity_schema import users

DEFAULT_RATES = {"fast": 2, "standard": 4, "refine": 12}
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
        or set(value) != set(DEFAULT_CONFIGURATION)
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
    return copy.deepcopy(value)


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
    return {key: value[key] for key in ("revision", "rates")}


def public_quote(execution):
    value = execution.get("credit_quote")
    if not isinstance(value, dict) or value.get("enforced") is not True:
        return {}
    return {key: value[key] for key in ("credits", "price_revision", "breakdown")}


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
        "credits": sum(plan["credits"] for plan in quoted) if known else None,
        "price_revision": revisions[0] if len(revisions) == 1 else None,
        "price_revisions": revisions,
        "breakdown": [item for plan in quoted for item in plan.get("breakdown", [])],
    }


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
    output = compiled.get("output_spec", {})
    frames, fps = output.get("frames"), output.get("fps", FPS)
    if (
        type(frames) is not int
        or not 1 <= frames <= 10_000_000
        or type(fps) is not int
        or not 1 <= fps <= 240
    ):
        raise PricingError(
            "credit_quote_invalid", "Compiled output duration is unavailable."
        )
    from .render_plans import RECIPE as RENDER_RECIPE

    render = (
        compiled.get("recipe_id") == RENDER_RECIPE
        and isinstance(execution, dict)
        and execution.get("backend") == "cpu-render"
    )
    request = compiled.get("request", {})
    width, height = output.get("width"), output.get("height")
    standard = (
        request.get("model") == "MiniMax-H3-Pruned-Rank8-INT8"
        and type(width) is int
        and type(height) is int
        and min(width, height) <= 480
        and width * height <= 480 * 480 * 1.75
    )
    # No TaoMate recipe is registered in the current server catalog. Arbitrary
    # low step counts never buy the future named accelerator's fast rate.
    tier = "render" if render else "standard" if standard else "refine"
    rate = 0 if render else price["rates"][tier]
    amount = math.ceil(Fraction(rate * frames, fps))
    seconds = frames / fps
    return {
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
        "breakdown": [
            {
                "tier": tier,
                "rate": rate,
                "output_seconds": seconds,
                "frames": frames,
                "fps": fps,
                "credits": amount,
            }
        ],
    }


def hide_dollar_estimate(execution, principal):
    return bool(public_quote(execution)) and (
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
