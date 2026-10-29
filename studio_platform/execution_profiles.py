"""Read exact operator-owned deployment bindings; no provider/runtime side effects."""

import json
import os
from pathlib import Path
import stat
import copy


def tested_envelope(profile_id, mode):
    # fmt: off
    """Compatibility name for supported mapping, not a historical allowlist.

This does not change a saved protected policy or renew its cost/time authority.
"""
    # fmt: on
    from .h3_profile_support import envelope

    return envelope(profile_id, mode)


# One validated parse per file version: capabilities/availability polls read
# this on every request. The key is the exact stat identity, so an operator's
# replacement or edit is still seen on the next call.
_PROFILES_CACHE = {}


def read_profiles(path):
    if path is None:
        return {}
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Execution profiles path must be absolute")
    try:
        meta = os.stat(path)
    except OSError:
        raise ValueError("Execution profiles unavailable or invalid") from None
    key = (
        str(path),
        meta.st_dev,
        meta.st_ino,
        meta.st_mode,
        meta.st_size,
        meta.st_mtime_ns,
        meta.st_ctime_ns,
    )
    cached = _PROFILES_CACHE.get(str(path))
    if cached is not None and cached[0] == key:
        return copy.deepcopy(cached[1])
    result = _read_profiles(path)
    _PROFILES_CACHE[str(path)] = (key, result)
    return copy.deepcopy(result)


def _read_profiles(path):
    try:
        with path.open("rb") as stream:
            meta = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(meta.st_mode)
                or os.name != "nt"
                and meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise ValueError("Execution profiles must be operator-owned")
            raw = stream.read(1048577)
        value = json.loads(raw)
        if (
            len(raw) > 1048576
            or not isinstance(value, dict)
            or set(value) != {"schema_version", "policies"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or not isinstance(value["policies"], list)
            or len(value["policies"]) > 64
        ):
            raise ValueError("Invalid execution profiles")
        from .execution_policy import validate_policy

        result = {}
        for item in value["policies"]:
            policy = validate_policy(item)
            if "deployment_profile_id" not in policy or len(policy["recipe_ids"]) != 1:
                raise ValueError("Profile requires one explicit mode")
            key = (policy["deployment_profile_id"], policy["recipe_ids"][0])
            if key in result:
                raise ValueError("Ambiguous execution profile")
            result[key] = policy
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, KeyError):
        raise ValueError("Execution profiles unavailable or invalid") from None


def selected_policy(settings, *, profile_id=None, recipe_id=None):
    if profile_id is None:
        from .execution_policy import read_policy

        return read_policy(settings.execution_policy_file)
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("Invalid deployment profile")
    return read_profiles(getattr(settings, "execution_profiles_file", None)).get(
        (profile_id, recipe_id)
    )


def default_profile_id(settings, profiles=None):
    """Only an explicitly configured, current FL binding can seed a new draft."""
    identity = getattr(settings, "default_deployment_profile_id", None)
    if identity is None:
        return None
    for profile in public_profiles(settings) if profiles is None else profiles:
        support = profile.get("generation_support", {}).get("fl", {})
        if (
            profile["id"] == identity
            and support.get("configured")
            and support.get("enabled")
        ):
            return identity
    return None


def public_profiles(settings):
    from .runtime_catalog import public_catalog
    from .inference.wangp_profile_dispatch import control_schema
    from .h3_profile_support import (
        limits as supported_limits,
        input_support,
        adapter_gaps,
        envelope,
        full_controls,
    )
    import time
    from .execution_policy import authority_current

    profiles = public_catalog()["profiles"]
    try:
        policies = read_profiles(getattr(settings, "execution_profiles_file", None))
    except ValueError:
        policies = {}
    now = time.time()
    for profile in profiles:
        modes = {}
        for model in profile["models"]:
            mode, recipe = model["mode"], model["generation_recipe_id"]
            policy = policies.get((profile["id"], recipe))
            constraints = (
                copy.deepcopy(policy['envelope'])
                if policy
                else tested_envelope(profile['id'], mode)
            )
            limits = supported_limits(mode, profile['id'])
            current = bool(
                policy
                and policy["enabled"]
                and authority_current(
                    policy, now, policy["reservation"]["expected_runtime_s"]
                )
            )
            enabled = (
                current
                and settings.generation_enabled
                and settings.execution_backend == "wangp-worker"
            )
            modes[mode] = {
                "configured": policy is not None,
                "enabled": enabled,
                "reason": "仍需预检实际容量和账户额度"
                if enabled
                else "此配置尚未接入当前执行池",
                "capacity_checked": False,
                "controls": control_schema(profile["id"], mode),
                'constraints': constraints,
                'limits': limits,
                'support_basis': 'implemented_pinned_model_api',
                'input_support': input_support(profile['id'], mode),
                'adapter_gaps': adapter_gaps(profile['id']),
                'supported_constraints': envelope(profile['id'], mode),
                'execution_policy_constraints': copy.deepcopy(policy['envelope'])
                if policy
                else None,
                'custom_canvas_constraints': {
                    'maximum_pixel_area': constraints['max_pixels'],
                    'minimum_aspect_ratio': 0.4,
                    'maximum_aspect_ratio': 2.5,
                },
                'input_notes': (
                    '按原生输入用途与数量限制使用，历史案例不限制新组合。视频原声逐项选择；视频/音频选段2–15秒，时间图片按目标帧注入。'
                    if full_controls(profile['id'])
                    else '按模型和适配器支持范围使用；当前旧适配器分开FL/REF输入，视频须关闭原声。'
                )
                + '实际资源和费用仍须预检；FL视频须明确选择去噪或上下文用途。',
                'joint_cases_scope': 'historical_examples_not_admission_allowlist',
                'joint_cases': [
                    {
                        'input_roles': c['input_roles'],
                        'width': c['width'],
                        'height': c['height'],
                        'frames': c['frames'],
                        'steps': c['steps'],
                    }
                    for c in profile['verified_cases']
                    + profile.get('qualification_cases', [])
                    if c['mode'] == mode
                ],
            }
        profile["generation_support"] = modes
    return profiles
