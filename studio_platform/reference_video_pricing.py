"""Exact reference-video prices from the existing validated native compiler."""

from fractions import Fraction
import math

FIELD = "reference_video_surcharge_bps_per_second"
RECOMMENDED = {"480P": 1000, "768P": 2500}


def validate(config):
    from .credits_pricing import PricingError

    if FIELD not in config:
        return
    value = config[FIELD]
    if (
        not isinstance(value, dict)
        or set(value) != set(RECOMMENDED)
        or any(type(n) is not int or not 0 <= n <= 100_000 for n in value.values())
        or value["768P"] < value["480P"]
        or "resolution_multipliers" not in config
    ):
        raise PricingError(
            "credit_prices_invalid", "Reference-video prices are invalid."
        )


def used_frames(compiled):
    """No requested/source durations, labels, or per-file rounding enter billing."""
    from .h3_profile_support import full_controls
    from .inference.wangp_full_compiler import reference_usage

    assets = compiled.get("assets", {})
    request = compiled.get("request", {})
    has_video = bool(request.get("inputs", {}).get("videos")) or any(
        item.get("metadata", {}).get("kind") == "video" for item in assets.values()
    )
    if not has_video:
        return 0
    profile = compiled.get("deployment_profile_id")
    if not profile or not full_controls(profile):
        raise ValueError("reference_video_compiler_unpriced")
    return reference_usage(
        request,
        {key: item["metadata"] for key, item in assets.items()},
        compiled["output_spec"],
        profile,
    )["reference_video_frames"]


def price(config, compiled, base, resolution):
    from .credits_pricing import PricingError

    if FIELD not in config:
        return math.ceil(base), {}
    try:
        frames = used_frames(compiled)
        bps = config[FIELD][resolution]
    except (ValueError, KeyError, TypeError) as error:
        raise PricingError(
            "credit_quote_invalid", "Compiled reference-video duration is unavailable."
        ) from error
    if frames and resolution == "768P" and compiled["output_spec"]["frames"] > 124:
        # Owner's #739 qualification gate: the measured Sharp reference-video
        # cases are five-second shots. Preserve accepted quotes; do not admit
        # a new longer shot under this policy before its staging measurement.
        raise PricingError(
            "reference_video_duration_unqualified",
            "Longer Sharp reference-video shots are awaiting qualification.",
        )
    seconds = Fraction(frames, 24)
    k = Fraction(bps, 10_000)
    multiplier = 1 + k * seconds
    total = math.ceil(base * multiplier)
    return total, {
        "base_credits": math.ceil(base),
        "reference_video_credits": total - math.ceil(base),
        "reference_video_frames": frames,
        "reference_video_seconds": float(seconds),
        "reference_video_k": float(k),
        "reference_video_multiplier": float(multiplier),
        "denoise_control_unpriced": compiled.get("request", {}).get(
            "video_conditioning"
        )
        == "denoise",
    }
