"""Read exact operator-owned deployment bindings; no provider/runtime side effects."""
import json
import os
from pathlib import Path
import stat
import copy


def tested_envelope(profile_id, mode):
    """Compatibility name for supported mapping, not a historical allowlist.

This does not change a saved protected policy or renew its cost/time authority.
"""
    from .h3_profile_support import envelope
    return envelope(profile_id,mode)


def read_profiles(path):
    if path is None:
        return {}
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Execution profiles path must be absolute")
    try:
        with path.open("rb") as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or os.name != "nt" and meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError("Execution profiles must be operator-owned")
            raw = stream.read(1048577)
        value = json.loads(raw)
        if (len(raw) > 1048576 or not isinstance(value, dict)
                or set(value) != {"schema_version", "policies"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or not isinstance(value["policies"], list) or len(value["policies"]) > 32):
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
    return read_profiles(getattr(settings, "execution_profiles_file", None)).get((profile_id, recipe_id))


def default_profile_id(settings, profiles=None):
    """Only an explicitly configured, current FL binding can seed a new draft."""
    identity = getattr(settings, "default_deployment_profile_id", None)
    if identity is None:
        return None
    for profile in public_profiles(settings) if profiles is None else profiles:
        support = profile.get("generation_support", {}).get("fl", {})
        if profile["id"] == identity and support.get("configured") and support.get("enabled"):
            return identity
    return None


def public_profiles(settings):
    from .runtime_catalog import public_catalog
    from .inference.wangp_profile_compiler import control_schema
    from .h3_profile_support import limits as supported_limits, INPUT_SUPPORT, ADAPTER_GAPS, envelope
    import time
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
            constraints = copy.deepcopy(policy['envelope']) if policy else tested_envelope(profile['id'],mode)
            limits = supported_limits(mode)
            current = bool(policy and policy["enabled"]
                and policy["qualification"]["status"] in {"accepted", "runtime_required"}
                and policy["qualification"]["verified_at"] <= now
                and now + policy["reservation"]["expected_runtime_s"] < min(
                    policy["qualification"]["expires_at"], policy["reservation"]["expires_at"]))
            enabled = current and settings.generation_enabled and settings.execution_backend == "wangp-worker"
            modes[mode] = {"configured": policy is not None, "enabled": enabled,
                "reason": "仍需预检实际容量和账户额度" if enabled else "此配置尚未接入当前执行池",
                "capacity_checked": False, "controls": control_schema(profile["id"], mode),
                'constraints':constraints,'limits':limits,
                'support_basis':'implemented_pinned_model_api','input_support':copy.deepcopy(INPUT_SUPPORT[mode]),
                'adapter_gaps':list(ADAPTER_GAPS),'supported_constraints':envelope(profile['id'],mode),
                'execution_policy_constraints':copy.deepcopy(policy['envelope']) if policy else None,
                'custom_canvas_constraints':{'maximum_pixel_area':constraints['max_pixels'],
                    'minimum_aspect_ratio':.4,'maximum_aspect_ratio':2.5},
                'input_notes':'按模型和适配器支持范围使用；历史实测组合仅供时间参考。'
                    +'当前适配器分开FL/REF输入；参考视频须24fps原生帧网格且关闭原声，音频2–15秒。实际资源和费用范围仍须预检。',
                'joint_cases_scope':'historical_examples_not_admission_allowlist',
                'joint_cases':[{'input_roles':c['input_roles'],'width':c['width'],'height':c['height'],
                    'frames':c['frames'],'steps':c['steps']} for c in
                    profile['verified_cases']+profile.get('qualification_cases',[]) if c['mode']==mode]}
        profile["generation_support"] = modes
    return profiles
