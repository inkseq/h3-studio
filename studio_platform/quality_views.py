"""Detached, typed creator views; never rewrite saved requests or arbitrary text.

Call at the existing authorized response boundary with the real hydrated
principal. Missing principals and every machine get the creator view. These
functions perform no database/provider reads or writes. Capability/availability
selection uses protected bindings only when the caller explicitly passes settings.
"""

from __future__ import annotations

import copy
import re

from .quality_tiers import (
    TIERS,
    model_tier,
    profile_alias,
    profile_tier,
    recipe_alias,
    recipe_mode,
)


PRIVATE_SELECTION = frozenset(
    {
        "model",
        "model_id",
        "model_type",
        "modelId",
        "modelType",
        "model_label",
        "configuration",
        "configuration_id",
        "configuration_hash",
        "config",
        "compiler_id",
        "compiler_epoch",
        "runtime",
        "runtime_manifest",
        "runtime_manifest_hash",
        "engine_manifest_digest",
        "backend",
        "pool",
        "worker_id",
        "source",
        "components",
        "weights_revision",
        "source_revision",
        "gpu_models",
        "gpu_count_options",
        "timing_hint",
        "verified_cases",
        "qualification_cases",
    }
)
RENDER_RECIPE = "chapter-roughcut-v1"


def is_admin(principal):
    return bool(
        principal is not None
        and getattr(principal, "machine", True) is False
        and getattr(principal, "role", None) == "admin"
        and getattr(principal, "status", None) == "active"
    )


def _pick(value, fields):
    return (
        {key: copy.deepcopy(item) for key, item in value.items() if key in fields}
        if isinstance(value, dict)
        else {}
    )


def _managed(value):
    """Existing trusted DTO, minus model/runtime slots; never filter user text."""
    return (
        {
            key: copy.deepcopy(item)
            for key, item in value.items()
            if key not in PRIVATE_SELECTION
        }
        if isinstance(value, dict)
        else {}
    )


def _tier(value):
    if not isinstance(value, dict):
        return None
    identity = value.get(
        "deployment_profile_id",
        value.get("deploymentProfileId", value.get("profile_id")),
    )
    if identity is not None:
        if isinstance(identity, str) and identity in {
            profile_alias(tier) for tier in TIERS
        }:
            return identity.split(":", 1)[1]
        return profile_tier(identity)
    frozen = value.get("quality_selection")
    if isinstance(frozen, dict):
        return _tier(frozen)
    if value.get("tier") in TIERS:
        return value["tier"]
    return model_tier(value.get("model_id", value.get("model")))


def _mode(value):
    if not isinstance(value, dict):
        return None
    return (
        value["mode"]
        if isinstance(value.get("mode"), str) and value["mode"] in {"fl", "ref"}
        else recipe_mode(value.get("recipe_id", value.get("recipeId")))
    )


def _identities(result, original):
    tier, mode = _tier(original), _mode(original)
    result["tier"] = tier
    for field in ("deployment_profile_id", "deploymentProfileId", "profile_id"):
        if field in original:
            result[field] = profile_alias(tier) if tier else "quality:unavailable"
    for field in ("recipe_id", "recipeId"):
        if field in original:
            result[field] = (
                RENDER_RECIPE
                if original[field] == RENDER_RECIPE
                else recipe_alias(mode)
                if mode
                else "video-unavailable-v1"
            )
    if mode:
        result["mode"] = mode
    return result


def controls(value):
    # DTO controls already passed the authoritative supported-control validator.
    # Do not create a second key list that can lag behind newly supported fields.
    return _managed(value)


def _reference(value):
    if isinstance(value, list):
        return [_reference(item) for item in value]
    if isinstance(value, dict):
        result = copy.deepcopy(value)
        for field in ("model_id", "model", "model_type", "deployment_profile_id"):
            result.pop(field, None)
        return result
    return copy.deepcopy(value)


def _inputs(value):
    if not isinstance(value, dict):
        return {}
    return {key: _reference(item) for key, item in value.items()}


def selection(value, principal=None):
    """Known authoring/default dictionaries, including Story camel-case keys."""
    if is_admin(principal):
        return copy.deepcopy(value)
    result = _identities(_managed(value), value)
    if isinstance(value, dict) and "controls" in value:
        result["controls"] = controls(value["controls"])
    if isinstance(value, dict) and "inputs" in value:
        result["inputs"] = _inputs(value["inputs"])
    if isinstance(value, dict) and isinstance(value.get("provenance"), list):
        result["provenance"] = [_reference(item) for item in value["provenance"]]
    if isinstance(value, dict) and isinstance(value.get("quality_selection"), dict):
        result["quality_selection"] = selection(value["quality_selection"], principal)
    return result


def request(value, principal=None):
    if is_admin(principal):
        return copy.deepcopy(value)
    result = _managed(value)
    if isinstance(value, dict) and isinstance(value.get("request"), dict):
        result["request"] = request(value["request"], principal)
    if isinstance(value, dict) and "controls" in value:
        result["controls"] = controls(value["controls"])
    if isinstance(value, dict) and "inputs" in value:
        result["inputs"] = _inputs(value["inputs"])
    for field in ("quality_selection", "authoring_recipe"):
        if isinstance(value.get(field), dict):
            result[field] = selection(value[field], principal)
    if isinstance(value.get("authoring_context"), dict):
        result["authoring_context"] = authoring_context(
            value["authoring_context"], principal
        )
    return _identities(result, value)


def diagnostic(value, principal=None):
    """Finite creator failure data; raw host/profile diagnostics stay internal."""
    if is_admin(principal):
        return copy.deepcopy(value)
    from .generation_diagnostics import (
        POLICY_DIAGNOSTICS,
        failure_reason,
        failure_stage,
    )

    if not isinstance(value, dict):
        return None
    code = failure_reason(value.get("reason_code", value.get("error_code")))
    result = _managed(value)
    for field in (
        "host",
        "profile_id",
        "deployment_profile_id",
        "deploymentProfileId",
        "detail",
        "details",
        "traceback",
        "stack",
        "exception",
        "stdout",
        "stderr",
        "raw",
    ):
        result.pop(field, None)
    result.update(reason_code=code, stage=failure_stage(code))
    en, zh = POLICY_DIAGNOSTICS.get(
        code,
        (
            "Generation did not finish. Review the original job before retrying.",
            "生成未完成，请核对原任务后再决定是否重试。",
        ),
    )
    result.update(message=zh, messages={"en": en, "zh": zh})
    return result


def error(value, principal=None):
    if is_admin(principal) or not isinstance(value, dict):
        return copy.deepcopy(value)
    from .generation_diagnostics import preflight_diagnostic

    original = value.get("code", value.get("error_code", value.get("detail")))
    result = copy.deepcopy(value)
    if isinstance(original, dict):
        result["detail"] = error(original, principal)
        return result
    if isinstance(original, list):
        result["detail"] = [
            {**_pick(item, {"loc", "type"}), "msg": "Invalid request field."}
            for item in original
            if isinstance(item, dict)
        ]
        return result
    if isinstance(original, str) and original in {
        "quality_tier_unavailable",
        "quality_tier_ambiguous",
        "quality_tier_invalid",
        "quality_mode_invalid",
        "quality_selection_invalid",
        "quality_selection_conflict",
        "quality_tier_required",
        "model_selection_admin_required",
    }:
        safe = {
            "quality_tier_unavailable": "This quality and mode are unavailable.",
            "quality_tier_ambiguous": "This quality and mode need an administrator's configuration.",
            "model_selection_admin_required": "Model selection requires a human administrator.",
        }
        code, message = (
            original,
            safe.get(original, "Choose a supported quality tier and mode."),
        )
    else:
        safe = preflight_diagnostic(ValueError(original))
        code, message = safe["error_code"], safe["error_message"]
        if (
            isinstance(original, str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", original)
            and not original.startswith(("minimax_", "h3_"))
        ):
            code = original  # Preserve trusted finite CAS/permission/readback codes.
    for field in (
        "model_id",
        "model",
        "modelId",
        "deployment_profile_id",
        "deploymentProfileId",
        "configuration_id",
        "traceback",
        "diagnostics",
        "details",
    ):
        result.pop(field, None)
    for field in ("code", "error_code"):
        if field in value:
            result[field] = code
    for field in ("detail", "message", "error_message"):
        if field in value:
            result[field] = (
                code if field == "detail" and value[field] == code else message
            )
    if "messages" in value:
        result["messages"] = {
            "en": "Review the original request and its error code before retrying.",
            "zh": message,
        }
    return result


def execution(value, principal=None):
    if is_admin(principal):
        return copy.deepcopy(value)
    result = _managed(value)
    if isinstance(value.get("quality_selection"), dict):
        result["quality_selection"] = selection(value["quality_selection"], principal)
    return _identities(result, value)


def artifact_metadata(value, principal=None):
    if is_admin(principal):
        return copy.deepcopy(value)
    result = _managed(value)
    result.pop("deployment_profile_id", None)
    result.pop("deploymentProfileId", None)
    result.pop("profile_id", None)
    if "recipe_id" in value:
        mode = recipe_mode(value["recipe_id"])
        result["recipe_id"] = recipe_alias(mode) if mode else "video-unavailable-v1"
    return result


def artifact(value, principal=None):
    if is_admin(principal):
        return copy.deepcopy(value)
    result = _managed(value)
    if isinstance(value, dict) and "metadata" in value:
        result["metadata"] = artifact_metadata(value["metadata"], principal)
    return result


def _result(value, principal):
    if not isinstance(value, dict):
        return copy.deepcopy(value) if value is None else {}
    result = _managed(value)
    if "failure_diagnostic" in value:
        result["failure_diagnostic"] = diagnostic(
            value["failure_diagnostic"], principal
        )
    return result


def job(value, principal=None):
    if is_admin(principal):
        return copy.deepcopy(value)
    result = _identities(_managed(value), value)
    if "effective_request" in value:
        result["effective_request"] = request(value["effective_request"], principal)
    if "execution" in value:
        result["execution"] = execution(value["execution"], principal)
    if "artifacts" in value:
        result["artifacts"] = [artifact(item, principal) for item in value["artifacts"]]
    if "result" in value:
        result["result"] = _result(value["result"], principal)
    if value.get("error_code") is not None:
        result["error_code"] = diagnostic({"error_code": value["error_code"]})[
            "reason_code"
        ]
    else:
        result["error_code"] = None
    if "failure_diagnostic" in value:
        result["failure_diagnostic"] = diagnostic(
            value["failure_diagnostic"], principal
        )
    if isinstance(value.get("wait_diagnostic"), dict):
        result["wait_diagnostic"] = _managed(value["wait_diagnostic"])
    return result


def plan(value, principal=None):
    if is_admin(principal):
        return copy.deepcopy(value)
    result = _identities(_managed(value), value)
    if "effective_request" in value:
        result["effective_request"] = request(value["effective_request"], principal)
    if "execution" in value:
        result["execution"] = execution(value["execution"], principal)
    if "request" in value:
        result["request"] = request(value["request"], principal)
    if "authoring_recipe" in value:
        result["authoring_recipe"] = selection(value["authoring_recipe"], principal)
    if "authoring_context" in value:
        result["authoring_context"] = authoring_context(
            value["authoring_context"], principal
        )
    return result


def _schema(value):
    if not isinstance(value, dict):
        return {}
    result = copy.deepcopy(value)
    for field in ("description", "title", "reason"):
        result.pop(field, None)
    for key in ("items", "additionalProperties"):
        if isinstance(value.get(key), dict):
            result[key] = _schema(value[key])
    if isinstance(value.get("properties"), dict):
        result["properties"] = {
            key: _schema(item) for key, item in value["properties"].items()
        }
    return result


def _support(value):
    result = _pick(value, {"configured", "enabled", "capacity_checked"})
    for field in ("limits", "custom_canvas_constraints", "input_support"):
        if isinstance(value.get(field), dict):
            result[field] = _managed(value[field])
    for field in ("constraints", "supported_constraints"):
        if isinstance(value.get(field), dict):
            result[field] = _envelope(value[field])
    result["controls"] = {
        key: _schema(item) for key, item in value.get("controls", {}).items()
    }
    result["reason_code"] = (
        "preflight_required" if value.get("enabled") else "quality_tier_unavailable"
    )
    return result


def _envelope(value):
    result = _managed(value)
    if isinstance(value.get("controls"), dict):
        result["controls"] = controls(value["controls"])
    if isinstance(value.get("input_limits"), dict):
        source = value["input_limits"]
        result["input_limits"] = _managed(source)
        if "guide_kinds" in source:
            result["input_limits"]["guide_kinds"] = [
                kind
                for kind in source["guide_kinds"]
                if kind in {"image", "video", "audio"}
            ]
        if "guide_recipe_ids" in source:
            result["input_limits"]["guide_recipe_ids"] = [
                recipe_alias(mode)
                for item in source["guide_recipe_ids"]
                if (mode := recipe_mode(item))
            ]
    return result


def recipe(value, principal=None):
    if is_admin(principal):
        return copy.deepcopy(value)
    result = _support(value)
    result.update(_pick(value, {"implemented", "enabled", "mode"}))
    mode = _mode(value)
    result.update(
        id=recipe_alias(mode) if mode else "video-unavailable-v1",
        label="Video · " + mode.upper() if mode else "Video",
        tier=None,
    )
    return result


def _selected(candidates, mode, default, tier, settings):
    if settings is not None:
        from .quality_tiers import QualitySelectionError, resolve_selection

        try:
            identity = resolve_selection(settings, tier, mode)["deployment_profile_id"]
        except QualitySelectionError as error:
            return None, error.code
        return next(
            (
                item
                for item in candidates
                if item.get("id", item.get("deployment_profile_id")) == identity
            ),
            None,
        ), "quality_tier_unavailable"
    enabled = [
        item
        for item in candidates
        if item.get("generation_support", {}).get(mode, {}).get("enabled")
    ]
    preferred = next((item for item in enabled if item.get("id") == default), None)
    return preferred or (
        enabled[0] if len(enabled) == 1 else None
    ), "quality_tier_ambiguous" if len(enabled) > 1 else "quality_tier_unavailable"


def capabilities(value, principal=None, *, settings=None):
    profiles = value.get("deployment_profiles", [])
    if is_admin(principal):
        result = copy.deepcopy(value)
        for profile in result.get("deployment_profiles", []):
            profile["tier"] = profile_tier(profile.get("id"))
        return result
    result = _pick(
        value,
        {
            "story_film",
            "library",
            "content_lifecycle",
            "upload_constraints",
            "execution_enabled",
            "simulation",
        },
    )
    result["capabilities_version"] = value.get("capabilities_version")
    result["recipes"] = [recipe(item, principal) for item in value.get("recipes", [])]
    result["deployment_profiles"] = []
    default = value.get("default_deployment_profile_id")
    for tier in TIERS:
        candidates = [item for item in profiles if profile_tier(item.get("id")) == tier]
        modes = {}
        for mode in ("fl", "ref"):
            chosen, reason = _selected(candidates, mode, default, tier, settings)
            if chosen is not None:
                modes[mode] = _support(chosen["generation_support"][mode])
            else:
                modes[mode] = {
                    "configured": False,
                    "enabled": False,
                    "capacity_checked": False,
                    "reason_code": reason,
                }
        result["deployment_profiles"].append(
            {
                "id": profile_alias(tier),
                "label": tier.title(),
                "tier": tier,
                "generation_support": modes,
            }
        )
    result["default_deployment_profile_id"] = (
        profile_alias(profile_tier(default)) if profile_tier(default) else None
    )
    return result


def availability(value, principal=None, *, settings=None):
    if is_admin(principal):
        result = copy.deepcopy(value)
        for profile in result.get("profiles", []):
            profile["tier"] = profile_tier(profile.get("deployment_profile_id"))
        return result
    result = _pick(
        value,
        {"version", "observed_at", "expires_at", "poll_after_seconds", "advisory_only"},
    )
    result["profiles"] = []
    for tier in TIERS:
        candidates = [
            item
            for item in value.get("profiles", [])
            if profile_tier(item.get("deployment_profile_id")) == tier
        ]
        modes = {}
        for mode in ("fl", "ref"):
            chosen, reason = _selected(candidates, mode, None, tier, settings)
            if chosen is not None:
                modes[mode] = _pick(
                    chosen.get("modes", {}).get(mode, {}),
                    {"state", "available", "reason_code"},
                )
                modes[mode]["reason_code"] = {
                    "ready": "capacity_ready",
                    "busy": "capacity_busy",
                    "starting": "capacity_preparing",
                    "unknown": "capacity_unknown",
                }.get(modes[mode].get("state"), "quality_tier_unavailable")
                modes[mode]["recipe_id"] = recipe_alias(mode)
            else:
                modes[mode] = {
                    "state": "unavailable",
                    "available": False,
                    "reason_code": reason,
                    "recipe_id": recipe_alias(mode),
                }
        result["profiles"].append(
            {"deployment_profile_id": profile_alias(tier), "tier": tier, "modes": modes}
        )
    return result


def _identity_schema(value, aliases, lookup):
    """Project one known identity field, including nullable Pydantic variants."""
    result = copy.deepcopy(value)

    def visit(field):
        field.pop("description", None)
        if "enum" in field:
            field["enum"] = list(aliases) + ([None] if None in field["enum"] else [])
        for name in ("default", "const"):
            if name in field:
                field[name] = lookup(field[name])
        for name in ("allOf", "anyOf", "oneOf"):
            for variant in field.get(name, []):
                if isinstance(variant, dict):
                    visit(variant)

    visit(result)
    nullable = "null" in value.get("type", []) or any(
        variant.get("type") == "null"
        for name in ("anyOf", "oneOf")
        for variant in value.get(name, [])
        if isinstance(variant, dict)
    )
    result["enum"] = list(aliases) + ([None] if nullable else [])
    return result


def _profile_schema_alias(value):
    if isinstance(value, str) and value.removeprefix("quality:") in TIERS:
        return profile_alias(value.removeprefix("quality:"))
    tier = profile_tier(value)
    return profile_alias(tier) if tier else None


def schema(value, principal=None, settings=None):
    """Only JSON-schema identity enums; unrelated assistant-model enums survive."""
    result = copy.deepcopy(value)
    if is_admin(principal) or not isinstance(result, dict):
        return result
    properties = result.get("properties", {})
    for name, field in list(properties.items()):
        if name in {"deployment_profile_id", "deploymentProfileId"}:
            properties[name] = _identity_schema(
                field,
                [profile_alias(tier) for tier in TIERS],
                _profile_schema_alias,
            )
        elif name in {"recipe_id", "recipeId"}:
            properties[name] = _identity_schema(
                field,
                [recipe_alias(mode) for mode in ("fl", "ref")],
                lambda raw: recipe_alias(mode) if (mode := recipe_mode(raw)) else None,
            )
        elif name == "model_id" and any(
            model_tier(item) for item in field.get("enum", [])
        ):
            properties.pop(name)
            if isinstance(result.get("required"), list):
                result["required"] = [key for key in result["required"] if key != name]
        else:
            properties[name] = schema(field, principal, settings)
    for container in ("$defs", "definitions"):
        if isinstance(result.get(container), dict):
            result[container] = {
                key: schema(item, principal, settings)
                for key, item in result[container].items()
            }
    for container in ("allOf", "anyOf", "oneOf"):
        if isinstance(result.get(container), list):
            result[container] = [
                schema(item, principal, settings) for item in result[container]
            ]
    if isinstance(result.get("items"), dict):
        result["items"] = schema(result["items"], principal, settings)
    if isinstance(result.get("operation_schemas"), dict):
        # Guided operations form a named map of complete JSON Schema roots.
        result["operation_schemas"] = {
            name: schema(operation, principal, settings)
            for name, operation in result["operation_schemas"].items()
        }
    if isinstance(result.get("card_authoring"), dict):
        for name in ("create_request", "revision_request", "response"):
            if isinstance(result["card_authoring"].get(name), dict):
                result["card_authoring"][name] = schema(
                    result["card_authoring"][name], principal, settings
                )
    if isinstance(result.get("capabilities"), dict):
        result["capabilities"] = capabilities(
            result["capabilities"], principal, settings=settings
        )
    if isinstance(result.get("default_next_settings"), dict):
        result["default_next_settings"] = selection(
            result["default_next_settings"], principal
        )
    return result


def revision(value, principal=None):
    if is_admin(principal):
        return copy.deepcopy(value)
    result = selection(value, principal)
    result.update(_pick(value, {"card_id", "turn_id", "items", "web_url"}))
    if isinstance(value.get("composer_snapshot"), dict):
        result["composer_snapshot"] = {
            "bindings": [
                _reference(item)
                for item in value["composer_snapshot"].get("bindings", [])
            ]
        }
    elif "composer_snapshot" in value:
        result["composer_snapshot"] = None
    return result


def take(value, principal=None):
    if is_admin(principal):
        return copy.deepcopy(value)
    result = _managed(value)
    if "recipe" in value:
        result["recipe"] = selection(value["recipe"], principal)
    if value.get("job") is not None:
        result["job"] = job(value["job"], principal)
    return result


def project(value, principal=None):
    """Only known Story generation slots; arbitrary user document text is intact."""
    result = copy.deepcopy(value)
    if is_admin(principal) or not isinstance(result, dict):
        return result
    payload = result.get("project", result.get("payload", result))
    if isinstance(payload.get("entities"), list):
        payload["entities"] = [entity(item, principal) for item in payload["entities"]]
    defaults = payload.get("journey", {}).get("storyboard", {})
    if isinstance(defaults.get("defaults"), dict):
        defaults["defaults"] = selection(defaults["defaults"], principal)
    return result


def entity(value, principal=None):
    result = copy.deepcopy(value)
    if is_admin(principal) or not isinstance(result, dict):
        return result
    data = result.get("data", {})
    if isinstance(data.get("h3"), dict):
        data["h3"] = selection(data["h3"], principal)
    context = data.get("restoredRecipeContext", {})
    if isinstance(context.get("original_authoring_context"), dict):
        context["original_authoring_context"] = authoring_context(
            context["original_authoring_context"], principal
        )
    return result


def film(value, principal=None):
    if is_admin(principal):
        return copy.deepcopy(value)
    result = _managed(value)
    for key in ("generation", "chapters"):
        result[key] = [plan(item, principal) for item in value.get(key, [])]
    if isinstance(value.get("export"), dict):
        result["export"] = plan(value["export"], principal)
    if value.get("error_code"):
        result["error_code"] = diagnostic({"error_code": value["error_code"]})[
            "reason_code"
        ]
    return result


def authoring_context(value, principal=None):
    if is_admin(principal) or not isinstance(value, dict):
        return copy.deepcopy(value)
    result = copy.deepcopy(value)
    for field in ("effective", "global", "story", "defaults", "global_defaults"):
        if isinstance(value.get(field), dict):
            result[field] = selection(value[field], principal)
    for field in ("shot", "scene"):
        if isinstance(value.get(field), dict):
            result[field] = (
                entity(value[field], principal)
                if isinstance(value[field].get("data"), dict)
                else selection(value[field], principal)
            )
    if isinstance(value.get("profiles"), list):
        result["profiles"] = [entity(item, principal) for item in value["profiles"]]
    if isinstance(value.get("storyboard"), dict) and isinstance(
        value["storyboard"].get("defaults"), dict
    ):
        result["storyboard"]["defaults"] = selection(
            value["storyboard"]["defaults"], principal
        )
    return result


def defaults(value, principal=None):
    """Plain authoring defaults or the existing layered effective-project view."""
    if isinstance(value, dict) and any(
        isinstance(value.get(field), dict)
        for field in ("effective", "global", "story", "shot")
    ):
        return authoring_context(value, principal)
    return selection(value, principal)


def envelope(value, principal=None, settings=None):
    """Known JSON response containers, including original replay envelopes.

    The HTTP caller selects generation/Story routes. Media, streams, auth and
    billing never pass here. Unknown/user-authored fields are copied intact;
    only the named generation slots below select another typed projection.
    """
    if is_admin(principal):
        return copy.deepcopy(value)
    if isinstance(value, list):
        return [envelope(item, principal, settings) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    if "deployment_profiles" in value:
        return capabilities(value, principal, settings=settings)
    result = copy.deepcopy(value)
    singles = {
        "job": job,
        "plan": plan,
        "retained_plan": plan,
        "project": project,
        "entity": entity,
        "revision": revision,
        "take": take,
        "film": film,
        "artifact": artifact,
        "effective_request": request,
        "execution": execution,
        "authoring_recipe": selection,
        "authoring_context": authoring_context,
        "original_authoring_context": authoring_context,
        "next_settings": selection,
        "default_next_settings": selection,
        "defaults": defaults,
        "draft": selection,
        "recipe": selection,
        "effective_recipe": selection,
        "h3": selection,
        "failure_diagnostic": diagnostic,
    }
    for key, projector in singles.items():
        if isinstance(value.get(key), dict):
            result[key] = projector(value[key], principal)
    plurals = {
        "jobs": job,
        "plans": plan,
        "revisions": revision,
        "takes": take,
        "films": film,
        "artifacts": artifact,
    }
    for key, projector in plurals.items():
        if isinstance(value.get(key), list):
            result[key] = [projector(item, principal) for item in value[key]]
    for key in (
        "session",
        "submission",
        "preflight",
        "data",
        "response",
        "result",
        "record",
        "batch",
    ):
        if isinstance(value.get(key), dict):
            result[key] = envelope(value[key], principal, settings)
    if isinstance(value.get("error"), dict):
        result["error"] = error(value["error"], principal)
    if "detail" in value or "code" in value and "message" in value:
        return error(result, principal)
    if value.get("error_code"):
        result = error(result, principal)
    for key in ("items", "submissions", "preflights", "events", "versions", "batches"):
        if isinstance(value.get(key), list):
            result[key] = [envelope(item, principal, settings) for item in value[key]]
    if isinstance(value.get("capabilities"), dict):
        result["capabilities"] = capabilities(
            value["capabilities"], principal, settings=settings
        )
    if "generation" in value and "chapters" in value:
        return film(result, principal)
    if (
        "entities" in value
        or isinstance(value.get("payload"), dict)
        and "entities" in value["payload"]
    ):
        return project(result, principal)
    if "type" in value and isinstance(value.get("data"), dict):
        return entity(result, principal)
    if (
        "status" in value
        and "plan_id" in value
        and ("artifacts" in value or "result" in value)
    ):
        return job(result, principal)
    if "metadata" in value and "job_id" in value and "content_url" in value:
        return artifact(result, principal)
    if "card_id" in value and "input_hash" in value and "controls" in value:
        return revision(result, principal)
    if (
        "effective_request" in value
        or "execution" in value
        or "plan_id" in value
        and "request_hash" in value
    ):
        return plan(result, principal)
    if "recipe_id" in value or "deployment_profile_id" in value:
        result = selection(result, principal)
    return result
