"""Resolve new creator intent without rewriting retained recipes or receipts."""

import copy

from .identity import is_admin
from .quality_tiers import (
    MODE_RECIPES,
    TIERS,
    QualitySelectionError,
    model_tier,
    normalize_request,
    profile_alias,
    profile_tier,
    recipe_alias,
    recipe_mode,
)

SELECTOR_NAMES = {
    "recipeId": "recipe_id",
    "recipe_id": "recipe_id",
    "deploymentProfileId": "deployment_profile_id",
    "deployment_profile_id": "deployment_profile_id",
    "modelId": "model_id",
    "model_id": "model_id",
    "model": "model",
    "inputMode": "mode",
    "mode": "mode",
    "tier": "tier",
}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _entities(value):
    entities = _dict(value).get("entities", [])
    return entities if isinstance(entities, list) else []


def _storyboard(value):
    return _dict(_dict(_dict(value).get("journey")).get("storyboard"))


def normalize(settings, repo, principal, value, *, stored=False, partial=False):
    internal = is_admin(principal, repo, settings)
    body = copy.deepcopy(value)
    if (
        partial
        and isinstance(body, dict)
        and not {
            "tier",
            "mode",
            "recipe_id",
            "deployment_profile_id",
            "model",
            "model_id",
        }.intersection(body)
    ):
        return body, None
    if stored and not internal:
        # A stored snapshot is trusted historical input, not permission for a
        # caller to select an internal model. New plans resolve its tier again;
        # accepted plan/job replays never pass through this function.
        profile = body.get("deployment_profile_id")
        tier = profile_tier(profile)
        mode = recipe_mode(body.get("recipe_id"))
        if tier is not None and mode is not None:
            body["deployment_profile_id"] = "quality:" + tier
            body["recipe_id"] = recipe_alias(mode)
            body.pop("model", None)
            body.pop("model_id", None)
    resolved, selection = normalize_request(settings, body, allow_internal=internal)
    if stored or selection is not None or not internal:
        from .creator_controls import controls

        if "controls" in resolved:
            resolved["controls"] = controls(
                resolved.get("deployment_profile_id"), resolved["controls"]
            )
    return resolved, selection


def public_mode_edit(value):
    """The old card editor copied only a neutral mode, not model authority."""
    return (
        isinstance(value, dict)
        and isinstance(value.get("recipe_id"), str)
        and value.get("recipe_id") in {recipe_alias(mode) for mode in MODE_RECIPES}
        and "tier" not in value
        and value.get("deployment_profile_id") is None
        and "model_id" not in value
        and "model" not in value
    )


def inherit_revision_mode(value, original):
    """Use only an authorized same-card original, before normal selection checks.

    This is new authoring, not replay: a known original tier resolves through
    today's protected binding. Unknown original profiles never select a default.
    No original revision, request fingerprint, or accepted job is changed.
    """
    result = copy.deepcopy(value)
    if not public_mode_edit(value):
        return result
    mode = recipe_mode(original.get("recipe_id"))
    if (
        mode is None
        or recipe_mode(value["recipe_id"]) != mode
        or "mode" in value
        and value["mode"] != mode
    ):
        return result
    identity = original.get("deployment_profile_id")
    if identity is None:
        # Compatibility only: the server, not the caller, chose this legacy
        # unprofiled recipe. The usual mode-only normalizer still applies.
        result["recipe_id"] = MODE_RECIPES[mode]
    else:
        tier = profile_tier(identity)
        if tier is None:
            raise QualitySelectionError(
                "quality_tier_unavailable",
                "This quality and mode are unavailable.",
                409,
            )
        result.update(tier=tier, mode=mode)
    return result


def _selectors(value):
    selected = {}
    for source, target in SELECTOR_NAMES.items():
        if source not in value:
            continue
        if source == "inputMode" and (
            value.get("recipeId") or value.get("recipe_id") or "mode" in value
        ):
            # Historical h3 kept this fallback after an explicit recipe change.
            # It is not a second new-mode selection and cannot override recipeId.
            continue
        if target in selected and selected[target] != value[source]:
            raise QualitySelectionError(
                "quality_selection_conflict", "Quality selections disagree."
            )
        selected[target] = value[source]
    return selected


def _public_selectors(value):
    """Recognize an unchanged readback without resolving today's routing."""
    result = _selectors(value)
    identity = result.get("deployment_profile_id")
    if identity is not None:
        tier = profile_tier(identity)
        if isinstance(identity, str) and identity in {
            profile_alias(item) for item in TIERS
        }:
            tier = identity.split(":", 1)[1]
    else:
        tier = model_tier(result.get("model_id", result.get("model")))
    mode = result.get("mode")
    if not isinstance(mode, str) or mode not in {"fl", "ref"}:
        mode = recipe_mode(result.get("recipe_id"))
    result.pop("model_id", None)
    result.pop("model", None)
    if "deployment_profile_id" in result:
        result["deployment_profile_id"] = (
            profile_alias(tier) if tier else "quality:unavailable"
        )
    if "recipe_id" in result:
        result["recipe_id"] = recipe_alias(mode) if mode else "video-unavailable-v1"
    result["tier"] = tier
    if mode:
        result["mode"] = mode
    return result


def _unchanged(value, previous):
    if not isinstance(previous, dict):
        return False
    selected = {key: item for key, item in value.items() if key in SELECTOR_NAMES}
    original = {key: item for key, item in previous.items() if key in SELECTOR_NAMES}
    if selected == original:
        return True
    # The public serializer adds tier/mode to an existing selection. Compare its
    # exact neutral identity, never use inventory/defaults to reinterpret it.
    try:
        return _selectors(value) == _public_selectors(previous)
    except QualitySelectionError:
        return False


def _restore_selectors(value, previous):
    result = {key: item for key, item in value.items() if key not in SELECTOR_NAMES}
    result.update(
        {
            key: copy.deepcopy(item)
            for key, item in previous.items()
            if key in SELECTOR_NAMES
        }
    )
    return result


def selector_baseline(value):
    """Bounded in-memory identity snapshot before a generic document mutation."""
    defaults = _dict(_storyboard(value).get("defaults"))
    return {
        "entities": [
            {
                "id": entity.get("id"),
                "type": "shot",
                "data": {
                    "h3": {
                        key: copy.deepcopy(item)
                        for key, item in entity["data"]["h3"].items()
                        if key in SELECTOR_NAMES
                    }
                },
            }
            for entity in _entities(value)
            if isinstance(entity, dict)
            and entity.get("type") == "shot"
            and isinstance(_dict(entity.get("data")).get("h3"), dict)
        ],
        "journey": {
            "storyboard": {
                "defaults": {
                    key: copy.deepcopy(item)
                    for key, item in defaults.items()
                    if key in SELECTOR_NAMES
                }
            }
        },
    }


def project(settings, repo, principal, value, *, stored=False, baseline=None):
    """Translate new managed selections; preserve unchanged stored identities.

    baseline is the authorized locked original, never a caller-supplied snapshot.
    It protects ordinary title/parameter edits and neutral readback round trips
    without reinterpreting historical routing or permitting new raw selections.
    """
    result = copy.deepcopy(value)
    previous_entities = {
        entity.get("id"): entity
        for entity in _entities(baseline)
        if isinstance(entity, dict)
    }
    for entity in _entities(result):
        if not isinstance(entity, dict) or entity.get("type") != "shot":
            continue
        h3 = _dict(entity.get("data")).get("h3")
        if not isinstance(h3, dict):
            continue
        if not SELECTOR_NAMES.keys() & h3.keys():
            if stored and "controls" in h3:
                from .creator_controls import controls

                h3["controls"] = controls(
                    settings.default_deployment_profile_id, h3["controls"]
                )
            continue
        previous_entity = previous_entities.get(entity.get("id"), {})
        previous = (
            _dict(previous_entity.get("data")).get("h3")
            if previous_entity.get("type") == "shot"
            else None
        )
        if not stored and _unchanged(h3, previous):
            entity["data"]["h3"] = _restore_selectors(h3, previous)
            continue
        selected = _selectors(h3)
        resolved, _ = normalize(settings, repo, principal, selected, stored=stored)
        if stored and "controls" in h3:
            from .creator_controls import controls

            h3["controls"] = controls(
                resolved.get("deployment_profile_id"), h3["controls"]
            )
        for key in SELECTOR_NAMES:
            h3.pop(key, None)
        for source, target in (
            ("recipeId", "recipe_id"),
            ("deploymentProfileId", "deployment_profile_id"),
            ("modelId", "model_id"),
            ("model", "model"),
        ):
            if target in resolved:
                h3[source] = resolved[target]
        if "recipe_id" not in resolved and "mode" in selected:
            h3["inputMode"] = selected["mode"]
    if not stored:
        storyboard = _storyboard(result)
        defaults = storyboard.get("defaults")
        if isinstance(defaults, dict) and SELECTOR_NAMES.keys() & defaults.keys():
            previous = _storyboard(baseline).get("defaults")
            if _unchanged(defaults, previous):
                storyboard["defaults"] = _restore_selectors(defaults, previous)
            else:
                selected = {
                    **{
                        key: item
                        for key, item in defaults.items()
                        if key not in SELECTOR_NAMES
                    },
                    **_selectors(defaults),
                }
                storyboard["defaults"], _ = normalize(
                    settings, repo, principal, selected, partial=True
                )
    return result
