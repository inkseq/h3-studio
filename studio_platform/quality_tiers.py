"""Trusted quality routing beside the immutable runtime catalog.

This metadata never changes runtime profiles, execution policies or their hashes.
Call only for new authoring requests, after checking original idempotent replays.
It reads protected bindings, never inventory or provider state.
"""

import copy
import time
from types import MappingProxyType

from .execution_profiles import read_profiles
from .execution_policy import authority_current

MAPPING_REVISION = "quality-tiers-v1"
TIERS = ("fast", "standard", "natural", "refine")
# Public quality options that bill as another tier's model class. Natural is the
# Pika accelerator on the Standard model (owner 2026-10-30); its extra GPU time
# is a profile credit multiplier, not a separate rate.
BILLING_TIERS = MappingProxyType({"natural": "standard"})
MODE_RECIPES = MappingProxyType({"fl": "h3-base-fl2va-v1", "ref": "h3-base-ref2va-v1"})
PROFILE_TIERS = MappingProxyType(
    {
        "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1": "standard",
        "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-lowram-v1": "refine",
        "h3-unpruned33b-bf16-qwenbf16-vaefp16-sdpa-p3-splitqkv-v2": "refine",
        "h3-pruned-rank8-int8-pro6000-quanto-int8-vae-int8-sdpa-p4-lowram-v1": "standard",
        "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-v2": "standard",
        "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3": "standard",
        "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-cgroups-v4": "standard",
        "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-single-fl-v2": "refine",
        "h3-pruned-rank8-int8-sdpa-p4-29gib-fl-r2-v5": "standard",
        "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-fl-r2-v3": "refine",
        "h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6": "standard",
        "h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7": "standard",
        "h3-pruned-rank8-int8-sage2-cu130-p4-29gib-taomate5-v8": "standard",
        "h3-pruned-rank8-int8-sage2-cu130-wgp9cb2-taomate5-v9": "standard",
        "h3-pruned-rank8-int8-sage2-cu130-wgp9cb2-pika6-v9": "natural",
        "h3-pruned-rank8-int8-pro6000-sdpa-p4-controls-v2": "standard",
        "h3-unpruned33b-int8-sdpa-p3-controls-v2": "refine",
        "h3-unpruned33b-bf16-sdpa-p3-controls-v3": "refine",
        "h3-unpruned33b-int8-sdpa-p3-144gib-single-controls-v4": "refine",
        "h3-unpruned33b-bf16-sdpa-p3-256gib-single-controls-v4": "refine",
    }
)
MODEL_TIERS = MappingProxyType(
    {
        "MiniMax-H3-Pruned-Rank8-INT8": "standard",
        "MiniMax-H3-Base-INT8": "refine",
        "MiniMax-H3-Base-BF16": "refine",
    }
)


class QualitySelectionError(Exception):
    def __init__(self, code, detail, status=422):
        self.code, self.detail, self.status = code, detail, status
        super().__init__(code)


def profile_tier(profile_id):
    return PROFILE_TIERS.get(profile_id) if isinstance(profile_id, str) else None


def billing_tier(tier):
    return BILLING_TIERS.get(tier, tier)


def model_tier(model_id):
    return MODEL_TIERS.get(model_id) if isinstance(model_id, str) else None


def profile_alias(tier):
    return "quality:" + _tier(tier)


def recipe_alias(mode):
    return "video-" + _mode(mode) + "-v1"


def recipe_mode(recipe_id):
    if not isinstance(recipe_id, str):
        return None
    for mode, internal in MODE_RECIPES.items():
        if recipe_id in {internal, recipe_alias(mode)}:
            return mode
    return None


def _tier(value):
    if not isinstance(value, str) or value not in TIERS:
        raise QualitySelectionError(
            "quality_tier_invalid", "Choose a supported quality tier."
        )
    return value


def _mode(value):
    if not isinstance(value, str) or value not in MODE_RECIPES:
        raise QualitySelectionError("quality_mode_invalid", "Choose FL or REF mode.")
    return value


def _unavailable():
    return QualitySelectionError(
        "quality_tier_unavailable", "This quality and mode are unavailable.", 409
    )


def _current(policy, now):
    return bool(
        policy
        and policy["enabled"]
        and authority_current(policy, now, policy["reservation"]["expected_runtime_s"])
    )


def resolve_selection(settings, tier, mode, *, now=None):
    """Resolve one current protected binding, independently of worker stock."""
    tier, mode = _tier(tier), _mode(mode)
    if tier == "fast":
        # There is no registered accelerator/runtime for this tier yet.
        raise _unavailable()
    try:
        policies = read_profiles(getattr(settings, "execution_profiles_file", None))
    except ValueError:
        raise _unavailable() from None
    now = time.time() if now is None else now
    recipe = MODE_RECIPES[mode]
    default = getattr(settings, "default_deployment_profile_id", None)
    if profile_tier(default) == tier:
        if not _current(policies.get((default, recipe)), now):
            raise _unavailable()
        identity = default
    else:
        candidates = [
            identity
            for (identity, selected_recipe), policy in policies.items()
            if selected_recipe == recipe
            and profile_tier(identity) == tier
            and _current(policy, now)
        ]
        if len(candidates) > 1:
            raise QualitySelectionError(
                "quality_tier_ambiguous",
                "This quality and mode need an administrator's configuration.",
                409,
            )
        if not candidates:
            raise _unavailable()
        identity = candidates[0]
    return {
        "tier": tier,
        "mapping_revision": MAPPING_REVISION,
        "mode": mode,
        "deployment_profile_id": identity,
        "recipe_id": recipe,
    }


def normalize_request(settings, body, *, allow_internal=True, now=None):
    """Return detached compiler input and optional frozen quality provenance.

    The integration caller derives allow_internal from a freshly authenticated
    human admin. Its compatible default leaves existing compiler callers alone;
    HTTP entry points must pass an explicit trusted authorization decision.
    """
    if not isinstance(body, dict):
        raise QualitySelectionError(
            "quality_selection_invalid", "Generation settings must be an object."
        )
    result = copy.deepcopy(body)
    tier = _tier(body["tier"]) if "tier" in body else None
    mode = _mode(body["mode"]) if "mode" in body else None
    profile = body.get("deployment_profile_id")
    public_profile = isinstance(profile, str) and profile.startswith("quality:")
    if public_profile:
        alias_tier = _tier(profile[len("quality:") :])
        if tier is not None and tier != alias_tier:
            raise QualitySelectionError(
                "quality_selection_conflict", "Quality selections disagree."
            )
        tier = alias_tier
    recipe = body.get("recipe_id")
    public_recipe = isinstance(recipe, str) and recipe.startswith("video-")
    if public_recipe and recipe not in {recipe_alias(m) for m in MODE_RECIPES}:
        raise QualitySelectionError("quality_mode_invalid", "Choose FL or REF mode.")
    inferred_mode = recipe_mode(recipe)
    if mode is not None and inferred_mode is not None and mode != inferred_mode:
        raise QualitySelectionError(
            "quality_selection_conflict", "Mode selections disagree."
        )
    mode = mode or inferred_mode
    # Existing Story clients still emit these exact mode-only recipe names
    # next to a neutral profile alias. They do not select an internal model;
    # resolve the explicit tier normally instead of rejecting that round trip.
    if tier is not None and recipe in MODE_RECIPES.values():
        public_recipe = True
    # Compatibility for old clients that selected ONLY FL/REF, not a model.
    # Never use this path for an explicit tier/profile/model, or fall through
    # after an unavailable configured default. New docs use tier/mode only.
    legacy_mode_only = (
        tier is None
        and (recipe in MODE_RECIPES.values() or recipe is None and mode in MODE_RECIPES)
        and profile is None
        and "model_id" not in body
        and "model" not in body
    )
    # Admins get the same default as everyone when they choose no model; only an
    # explicit internal selection routes elsewhere (owner 2026-10-29 paid test).
    if legacy_mode_only:
        default = getattr(settings, "default_deployment_profile_id", None)
        if default is None:
            result.pop("mode", None)
            result["recipe_id"] = MODE_RECIPES[mode]
            return result, None
        default_tier = profile_tier(default)
        if default_tier is None:
            raise _unavailable()
        selection = resolve_selection(settings, default_tier, mode, now=now)
        result.pop("mode", None)
        result["recipe_id"] = MODE_RECIPES[mode]
        result["deployment_profile_id"] = selection["deployment_profile_id"]
        return result, selection
    internal = (
        profile is not None
        and not public_profile
        or recipe is not None
        and not public_recipe
        or "model_id" in body
        or "model" in body
    )
    if tier is not None and internal:
        raise QualitySelectionError(
            "quality_selection_conflict", "Use one quality selection format."
        )
    if internal and allow_internal is not True:
        raise QualitySelectionError(
            "model_selection_admin_required",
            "Model selection requires a human administrator.",
            403,
        )
    if tier is None:
        if public_recipe or allow_internal is not True:
            raise QualitySelectionError(
                "quality_tier_required", "Choose a quality tier."
            )
        result.pop("mode", None)
        return result, None
    selection = resolve_selection(settings, tier, mode, now=now)
    result.pop("tier", None)
    result.pop("mode", None)
    result["deployment_profile_id"] = selection["deployment_profile_id"]
    result["recipe_id"] = selection["recipe_id"]
    return result, selection
