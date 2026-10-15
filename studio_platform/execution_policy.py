"""Operator-owned admission policy; never accept routing, price or budgets from users.

This file describes approved configuration envelopes, not automatic performance
discovery. A historical benchmark cannot create one. No cloud resources or SDK
requests are made by policy evaluation.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import re
from pathlib import Path
import stat
import string

from .capabilities import MODEL, RECIPES
from .control import WorkerControl, REAL_GPU_BACKENDS
from .inference.outputs import validate_delivery_policy, native_delivery_spec, delivery_spec, GPU_R2_DELIVERY
from .inference.protocol import BackendError
from .repository import BudgetExceeded, NotFound, identifier, request_hash
from .qualification_profiles import (FL50_PROFILE, MULTIMODAL_PROFILE, QUEUED_TASK_PROFILE,
    RUNTIME_PROFILES, MULTIMODAL_INPUT_LIMITS, PROFILE_RECIPES)


POLICY = "self-hosted-default"
FIELDS = {"id", "revision", "enabled", "model_id", "backend", "pool", "configuration_id",
          "recipe_ids", "qualification", "envelope", "reservation", "budget_accounts"}
ENVELOPE = {"max_pixels", "max_duration_seconds", "max_steps", "max_reference_files",
            "max_guides", "allow_first_last", "allow_audio", "controls"}
INPUT_LIMITS = {"max_images", "max_videos", "max_audios", "max_image_pixels", "max_video_pixels",
                "max_video_duration_seconds", "max_audio_duration_seconds", "guide_kinds",
                "guide_recipe_ids", "max_guide_time_seconds", "allow_video_audio"}


def positive(value, maximum):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum


def validate_policy(value):
    """Reject ambiguous/misspelled operator settings rather than broadening them."""
    if not isinstance(value, dict) or not FIELDS <= set(value) or set(value) - FIELDS - {"engine_manifest_digest", "output_delivery", "deployment_profile_id", "dispatch_backend"}:
        raise ValueError("Invalid execution policy fields")
    if value.get("dispatch_backend", "legacy") not in {"legacy", "hatchet-v1"}:
        raise ValueError("Invalid dispatch backend")
    if value.get("dispatch_backend") == "hatchet-v1" and value.get("backend") != "wangp-worker":
        raise ValueError("Hatchet dispatch requires the explicit WanGP execution path")
    expected_model = MODEL
    if "deployment_profile_id" in value:
        from .runtime_catalog import get_profile
        expected_model = get_profile(value["deployment_profile_id"])["model_id"]
        if value["backend"] != "wangp-worker" or len(value["recipe_ids"]) != 1:
            raise ValueError("Deployment profiles require an explicit WanGP mode")
    if (value["id"] != POLICY or value["model_id"] != expected_model or value["backend"] not in REAL_GPU_BACKENDS
            or type(value["enabled"]) is not bool):
        raise ValueError("Invalid execution policy identity")
    validate_delivery_policy(value["backend"], value.get("output_delivery", ""))
    if value.get("output_delivery") == GPU_R2_DELIVERY and not value.get("deployment_profile_id"):
        raise ValueError("gpu_delivery_requires_explicit_deployment_profile")
    if "output_delivery" in value and not value["output_delivery"]:
        raise ValueError("Explicit output delivery must name a supported policy")
    if value["backend"] == "wangp-worker":
        if not isinstance(value.get("engine_manifest_digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["engine_manifest_digest"]):
            raise ValueError("Explicit engine manifest required")
    elif "engine_manifest_digest" in value:
        raise ValueError("Unexpected engine manifest for legacy policy")
    for field in ("revision", "pool", "configuration_id"):
        identifier(value[field])
    recipes = value["recipe_ids"]
    if "output_delivery" in value and recipes not in (["h3-base-fl2va-v1"], ["h3-base-ref2va-v1"]):
        raise ValueError("Native delivery requires one explicitly bound WanGP recipe")
    if not isinstance(recipes, list) or not recipes or len(set(recipes)) != len(recipes) or any(r not in RECIPES for r in recipes):
        raise ValueError("Invalid execution policy recipes")
    qualification = value["qualification"]
    qualification_fields = {"status", "evidence_id", "verified_at", "expires_at"}
    if (not isinstance(qualification, dict) or not qualification_fields <= set(qualification)
            or set(qualification) - qualification_fields - {"profile"}
            or qualification["status"] not in {"unverified", "accepted", "runtime_required"}
            or "profile" in qualification and qualification["profile"] not in PROFILE_RECIPES
            or qualification["status"] == "runtime_required" and qualification.get("profile") not in RUNTIME_PROFILES
            or qualification.get("profile") == QUEUED_TASK_PROFILE and qualification["status"] != "runtime_required"
            or not positive(qualification["verified_at"], 1e12)
            or not positive(qualification["expires_at"], 1e12)
            or qualification["verified_at"] >= qualification["expires_at"]):
        raise ValueError("Invalid execution qualification")
    identifier(qualification["evidence_id"])
    envelope = value["envelope"]
    if not isinstance(envelope, dict) or not ENVELOPE <= set(envelope) or set(envelope) - ENVELOPE - {"input_limits"}:
        raise ValueError("Invalid execution envelope")
    for field, maximum in (("max_pixels", 768*1344), ("max_duration_seconds", 16), ("max_steps", 1000)):
        if not positive(envelope[field], maximum):
            raise ValueError("Invalid execution envelope limit")
    for field, maximum in (("max_reference_files", 12), ("max_guides", 8)):
        if type(envelope[field]) is not int or not 0 <= envelope[field] <= maximum:
            raise ValueError("Invalid execution input envelope")
    if any(type(envelope[field]) is not bool for field in ("allow_first_last", "allow_audio")):
        raise ValueError("Invalid execution feature envelope")
    if "input_limits" in envelope:
        limits = envelope["input_limits"]
        if not isinstance(limits, dict) or set(limits) != INPUT_LIMITS:
            raise ValueError("Explicit per-kind input limits required")
        from .h3_profile_support import full_controls
        full_native = bool(value.get('deployment_profile_id') and full_controls(value['deployment_profile_id']))
        for field, maximum in (("max_images", 17 if full_native else 9), ("max_videos", 3), ("max_audios", 3)):
            if type(limits[field]) is not int or not 0 <= limits[field] <= maximum:
                raise ValueError("Invalid per-kind input count")
        for field, maximum in (("max_image_pixels", 5760**2), ("max_video_pixels", 5760**2),
                ("max_video_duration_seconds", 362/24 if 'deployment_profile_id' in value else 15),
                ("max_audio_duration_seconds", 15), ("max_guide_time_seconds", 362/24 if full_native else 15)):
            if not positive(limits[field], maximum):
                raise ValueError("Invalid per-kind input size or duration")
        for field, allowed in (("guide_kinds", {"image", "video", "audio"}), ("guide_recipe_ids", set(recipes))):
            options = limits[field]
            if (not isinstance(options, list) or any(not isinstance(item, str) for item in options)
                    or len(set(options)) != len(options) or not set(options) <= allowed):
                raise ValueError("Invalid qualified guide scope")
        if type(limits["allow_video_audio"]) is not bool:
            raise ValueError("Invalid reference video audio feature")
    if qualification.get("profile") in RUNTIME_PROFILES and "deployment_profile_id" not in value:
        limits = envelope.get("input_limits")
        if limits is None or not set(recipes) <= set(PROFILE_RECIPES[qualification["profile"]]):
            raise ValueError("Runtime qualification requires its explicit input scope")
        maxima = dict(MULTIMODAL_INPUT_LIMITS)
        if "deployment_profile_id" in value:
            maxima.update(max_audio_duration_seconds=5.2)
        for field, maximum in maxima.items():
            if isinstance(maximum, list):
                outside = not set(limits[field]) <= set(maximum)
            elif isinstance(maximum, bool):
                outside = limits[field] and not maximum
            else:
                outside = limits[field] > maximum
            if outside:
                raise ValueError("Input limits exceed the selected qualification suite")
    # Every non-numeric generation family must be explicitly qualified. Numeric
    # decoder tiling/export parameters remain visible and are never overridden.
    controls = envelope["controls"]
    required_controls = {"sampler_name", "scheduler", "video_decode", "audio_decode", "encoder_device", "ref_image_size"}
    if value["backend"] == "wangp-worker":
        # The first WanGP recipe has no generic reference-image resizing
        # control. Do not require a fabricated Comfy default in its snapshot.
        required_controls.remove("ref_image_size")
    if not isinstance(controls, dict) or set(controls) != required_controls:
        raise ValueError("Explicit execution control families required")
    if any(not isinstance(options, list) or not options or any(not isinstance(x, str) or len(x)>80 for x in options) for options in controls.values()):
        raise ValueError("Invalid execution control values")
    if "deployment_profile_id" in value:
        from .runtime_catalog import engine_manifest, get_profile, profile_output_delivery
        from .h3_profile_support import envelope as support_envelope
        mode = "fl" if recipes == ["h3-base-fl2va-v1"] else "ref"
        profile = get_profile(value["deployment_profile_id"])
        candidates = profile.get("qualification_cases", [])
        if candidates and (profile.get("validation", {}).get("scope") != "pending_hardware_qualification"
                or profile.get("validation", {}).get("production_adapter_verified") is not False
                or qualification["status"] != "runtime_required"
                or any("measurements" in case for case in candidates)):
            raise ValueError("Deployment profile candidate qualification is not pending")
        supported = support_envelope(profile['id'],mode)
        if (value.get("output_delivery") != profile_output_delivery(profile["id"])
                or qualification.get("profile") != QUEUED_TASK_PROFILE
                or value["engine_manifest_digest"] != engine_manifest(profile["id"], mode).digest
                or envelope["max_pixels"] > supported['max_pixels']
                or envelope["max_steps"] > supported['max_steps']
                or envelope["max_duration_seconds"] > supported['max_duration_seconds'] + 1e-6
                or envelope["max_reference_files"] > supported['max_reference_files']
                or envelope["max_guides"] > supported['max_guides']
                or envelope["allow_first_last"] and not supported['allow_first_last']):
            raise ValueError("Deployment profile policy exceeds implemented model support")
        for field,options in controls.items():
            if not set(options) <= set(supported['controls'][field]):
                raise ValueError('Deployment profile policy includes unmapped controls')
        limits = envelope.get('input_limits')
        if limits is None:
            raise ValueError('Deployment profile requires explicit input limits')
        # FL policies historically carry unused REF-limit fields. Preserve their
        # serialized identity; the compiler rejects every actual REF input in FL.
        for field,maximum in supported['input_limits'].items() if (mode=='ref' or profile.get('compiler_epoch')=='full-controls-v2') else ():
            current = limits[field]
            outside = not set(current)<=set(maximum) if isinstance(maximum,list) else (
                current and not maximum if isinstance(maximum,bool) else current>maximum)
            if outside:
                raise ValueError('Deployment profile policy exceeds implemented input support')
        # Saved resource/cost policy remains authoritative. Measurements neither
        # constrain combinations nor promise performance or worker readiness.
    elif value["backend"] == "wangp-worker" and recipes == ["h3-base-ref2va-v1"]:
        from .inference.wangp_ref_compiler import validate_envelope
        if (recipes != ["h3-base-ref2va-v1"] or value.get("output_delivery") != "native-frames-v1"
                or qualification.get("profile") != QUEUED_TASK_PROFILE):
            raise ValueError("wangp_ref_explicit_recipe_and_native_delivery_required")
        validate_envelope(envelope)
    quote = value["reservation"]
    quote_fields = {"cost_microusd", "expected_runtime_s", "expires_at", "source_id"}
    if (not isinstance(quote, dict) or not quote_fields <= set(quote)
            or set(quote) - quote_fields - {"duration_reference_seconds"}
            or type(quote["cost_microusd"]) is not int or not 0 < quote["cost_microusd"] <= 1000000000
            or not positive(quote["expected_runtime_s"], 86400) or not positive(quote["expires_at"], 1e12)):
        raise ValueError("Invalid execution reservation")
    if "duration_reference_seconds" in quote:
        reference = quote["duration_reference_seconds"]
        if not positive(reference, 16):
            raise ValueError("Invalid duration allowance reference")
        # Keep even the largest derived allowance within the existing money /
        # runtime field limits. A tiny denominator must not create overflow.
        factor = max(1, envelope["max_duration_seconds"] / reference)
        if (not math.isfinite(factor) or quote["cost_microusd"] * factor > 1000000000
                or quote["expected_runtime_s"] * factor > 86400):
            raise ValueError("Duration allowance exceeds reservation limits")
    identifier(quote["source_id"])
    accounts = value["budget_accounts"]
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 8 or len(set(accounts)) != len(accounts):
        raise ValueError("Explicit execution budget accounts required")
    for template in accounts:
        if not isinstance(template, str) or len(template) > 200:
            raise ValueError("Invalid budget account template")
        try:
            for _, field, format_spec, conversion in string.Formatter().parse(template):
                if field is not None and (field not in {"tenant_id", "owner_id", "project_id"} or format_spec or conversion):
                    raise ValueError("Invalid budget account template")
            identifier(template.format(tenant_id="tenant", owner_id="owner", project_id="project"))
        except (KeyError, IndexError):
            raise ValueError("Invalid budget account template") from None
    return value


def reservation_for_duration(policy, actual_duration):
    """Apply an explicit operator allowance; this is not a speed prediction.

    Old policies remain byte-for-byte equivalent in their reservation values.
    With the opt-in reference, use the compiled native duration (including H3
    frame snapping), never a UI edit length. Short clips / fewer steps / lower
    resolutions cannot lower the operator's original minimum reservation.
    """
    quote = dict(policy["reservation"])
    reference = quote.get("duration_reference_seconds")
    if reference is None:
        return quote
    if not positive(actual_duration, 16):
        raise ValueError("Invalid native duration for reservation")
    factor = max(1, actual_duration / reference)
    quote["cost_microusd"] = math.ceil(quote["cost_microusd"] * factor)
    quote["expected_runtime_s"] = math.ceil(quote["expected_runtime_s"] * factor)
    return quote


def input_envelope_blockers(compiled, limits):
    """Check server-inspected metadata, never client labels or source filenames.

    Per-kind counts include unique references and guide media. First/last frames
    have their own two slots; their images still obey the image pixel bound and
    the existing aggregate asset cap. Duration bounds apply to TOTAL distinct
    media of each kind, checking both inspected source and normalized duration.
    """
    if limits is None:
        return []
    request, assets = compiled["request"], compiled["assets"]
    inputs, guides = request["inputs"], request.get("guides", [])
    blockers = []
    kinds = {"image": set(inputs["images"]), "video": set(inputs["videos"]), "audio": set(inputs["audios"])}
    metadata = {}
    for asset_id, asset in assets.items():
        meta = asset.get("metadata") if isinstance(asset, dict) else None
        if not isinstance(meta, dict) or meta.get("kind") not in kinds:
            blockers.append("参考素材缺少服务端已核验的类型信息，暂不能确认执行范围")
        else:
            metadata[asset_id] = meta
    if guides and compiled["recipe_id"] not in limits["guide_recipe_ids"]:
        blockers.append("当前生成方式尚未开放时间锚点；可保留锚点或明确移除后重新预检")
    for guide in guides:
        meta = metadata.get(guide["media_id"], {})
        kind = meta.get("kind")
        if kind in kinds:
            kinds[kind].add(guide["media_id"])
        if kind not in limits["guide_kinds"]:
            blockers.append("当前执行池仅接受以下时间锚点类型：" + " / ".join(limits["guide_kinds"]))
        if guide["time_seconds"] > limits["max_guide_time_seconds"]:
            blockers.append(f"时间锚点须位于 {limits['max_guide_time_seconds']:g} 秒以内")
    names = {"image": "图片参考", "video": "视频参考", "audio": "独立音频参考"}
    for kind, ids in kinds.items():
        maximum = limits["max_" + {"image": "images", "video": "videos", "audio": "audios"}[kind]]
        if len(ids) > maximum:
            blockers.append(f"当前使用 {len(ids)} 份{names[kind]}（含同类锚点）；执行池最多接受 {maximum} 份")
        if kind in {"video", "audio"}:
            maximum = limits[f"max_{kind}_duration_seconds"]
            durations, sources = [], []
            for asset_id in ids:
                meta = metadata.get(asset_id, {})
                duration, source = meta.get("duration"), meta.get("source_duration", meta.get("duration"))
                if not positive(duration, 86400) or not positive(source, 86400):
                    blockers.append(f"{names[kind]}缺少服务端已核验的时长，暂不能确认执行范围")
                    continue
                durations.append(duration)
                sources.append(source)
            if max(sum(durations), sum(sources)) > maximum + 1e-6:
                blockers.append(f"{names[kind]}总时长超过执行池的 {maximum:g} 秒上限（原选段与模型副本均需满足）")
    for meta in metadata.values():
        kind = meta["kind"]
        if kind in {"image", "video"}:
            width, height = meta.get("width"), meta.get("height")
            if not positive(width, 5760) or not positive(height, 5760):
                blockers.append(f"{names[kind]}缺少服务端已核验的尺寸，暂不能确认执行范围")
            elif width * height > limits[f"max_{kind}_pixels"]:
                label = "图片输入（含首尾帧与锚点）" if kind == "image" else names[kind]
                blockers.append(f"{label} {width:g}×{height:g} 超过执行池的 {limits[f'max_{kind}_pixels']:g} 像素上限")
    if not limits["allow_video_audio"]:
        enabled = any(metadata.get(asset_id, {}).get("has_audio") and request["video_audio"].get(asset_id, True)
            for asset_id in inputs["videos"])
        enabled = enabled or any(metadata.get(guide["media_id"], {}).get("kind") == "video"
            and metadata[guide["media_id"]].get("has_audio") and guide.get("use_audio") for guide in guides)
        if enabled:
            blockers.append("当前执行池尚未开放参考视频原声；请明确关闭原声或保留设置等待相符配置")
    return list(dict.fromkeys(blockers))


def read_policy(path):
    if path is None:
        return None
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Execution policy path must be absolute")
    try:
        with path.open("rb") as source:
            meta = os.fstat(source.fileno())
            if not stat.S_ISREG(meta.st_mode) or os.name != "nt" and meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ValueError("Execution policy must be an operator-owned file")
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError("Execution policy file exceeds limit")
        return validate_policy(json.loads(raw))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, KeyError):
        raise ValueError("Execution policy file is unavailable or invalid") from None


@dataclass(frozen=True)
class Admission:
    execution: dict
    cost: int
    expires_at: float
    estimate: dict


class ExecutionPolicies:
    def __init__(self, settings, repository):
        self.settings, self.repo = settings, repository
        self.control = WorkerControl(repository)

    def evaluate(self, compiled, scope, fingerprint):
        from .render_plans import RECIPE, MODEL as RENDER_MODEL, configuration_for, POOL
        if compiled["recipe_id"] == RECIPE:
            configuration = configuration_for(compiled)
            blockers = list(compiled.get("render_blockers", []))
            if not self.settings.render_enabled:
                blockers.append("章节粗剪执行尚未开启，时间线和素材仍可保存")
            else:
                capacity = self.control.pool_status(POOL, model_id=RENDER_MODEL,
                    configuration_id=configuration, recipe_id=RECIPE, backend="cpu-render")
                if not capacity["ready"]+capacity["busy"]:
                    blockers.append("章节粗剪工作机尚未就绪，请稍后重试")
            execution = {"pool": POOL, "backend": "cpu-render", "configuration_id": configuration,
                "quote_known": True, "enabled": not blockers, "blockers": blockers,
                "admission_state": "blocked" if blockers else "queued",
                "fingerprint": fingerprint, "budget_account_ids": [], "expected_runtime_s": 1800}
            return Admission(execution, 0, self.repo.clock()+900,
                {"currency": "USD", "cost_microusd": 0, "source": "operator-included-cpu-render",
                 "kind": "included", "description": "当前粗剪不单独计费；服务器资源仍有运行成本"})
        now = self.repo.clock()
        backend = self.settings.execution_backend
        base = {"pool": compiled["recipe_id"], "backend": backend, "expected_runtime_s": 600,
                "quote_known": False, "enabled": False, "blockers": [], "fingerprint": fingerprint,
                "budget_account_ids": [], "admission_state": "blocked"}
        unknown = {"currency": "USD", "cost_microusd": None, "source": "unknown", "kind": "unknown"}
        if not self.settings.generation_enabled:
            base["blockers"].append("生成执行尚未开启；项目和素材可以继续保存")
            return Admission(base, 0, now+900, unknown)
        if backend == "mock":
            base.update(pool="mock", expected_runtime_s=1, quote_known=True, enabled=True, admission_state="queued")
            return Admission(base, 0, now+900,
                {"currency": "USD", "cost_microusd": 0, "source": "mock", "kind": "simulation"})
        if backend not in REAL_GPU_BACKENDS:
            base["blockers"].append("尚未接入此执行方式")
            return Admission(base, 0, now+900, unknown)
        try:
            from .execution_profiles import selected_policy
            policy = selected_policy(self.settings, profile_id=compiled.get("deployment_profile_id"), recipe_id=compiled["recipe_id"])
        except ValueError:
            policy = None
        if policy is None:
            base["blockers"].append("缺少有效的执行池验收与费用策略，暂不发起生成")
            return Admission(base, 0, now+900, unknown)
        if policy["backend"] != backend:
            base["blockers"].append("执行策略与所选引擎不一致，请等待匹配配置")
            return Admission(base, 0, now+900, unknown)
        qualification, envelope = policy["qualification"], policy["envelope"]
        quote = reservation_for_duration(policy, compiled["output_spec"]["actual_duration"])
        blockers = base["blockers"]
        from .control import require_delivery_configuration
        from .repository import Conflict
        try:
            with self.repo.engine.connect() as connection:
                require_delivery_configuration(connection, backend, policy["configuration_id"], policy.get("output_delivery", ""))
        except Conflict:
            blockers.append("此执行配置已绑定另一种成片时长策略，请使用独立验收的配置")
        if not policy["enabled"]:
            blockers.append("操作员已暂停此执行策略")
        if qualification["status"] not in {"accepted", "runtime_required"} or not qualification["verified_at"] <= now < qualification["expires_at"]:
            blockers.append("此执行配置尚未验收或验收记录已过期")
        if quote["expires_at"] <= now:
            blockers.append("费用预留策略已过期，请等待更新")
        # A live heartbeat is not enough if the operator's serving window is
        # about to close. Do not accept a NEW task that cannot fit its own
        # conservative runtime. Running attempts still reconcile normally.
        latest_start = min(qualification["expires_at"], quote["expires_at"]) - quote["expected_runtime_s"]
        if latest_start <= now:
            blockers.append("当前服务时段剩余时间不足以完成此配方，请等待服务续期后重新预检")
        if compiled["recipe_id"] not in policy["recipe_ids"] or compiled["request"]["model"] != policy["model_id"]:
            blockers.append("执行池未验收此模型或配方")
        if backend == "wangp-worker" and compiled["recipe_id"] == "h3-base-ref2va-v1" and policy["recipe_ids"] != ["h3-base-ref2va-v1"]:
            blockers.append("参考模式需要独立绑定的 Ref2VA 执行配置；不能沿用首尾帧执行池")
        request, output = compiled["request"], compiled["output_spec"]
        refs = len(set(a for a in compiled["assets"]))
        profile_id = compiled.get('deployment_profile_id')
        if profile_id:
            from .h3_profile_support import full_controls
            if full_controls(profile_id):
                from .inference.wangp_full_compiler import reference_usage
                refs = reference_usage(request, {key:item['metadata'] for key,item in compiled['assets'].items()},
                    output, profile_id)['reference_rows']
        if output["width"]*output["height"] > envelope["max_pixels"]:
            blockers.append(f"当前画面 {output['width']}×{output['height']} 超出执行池的 {envelope['max_pixels']} 像素上限")
        if output["actual_duration"] > envelope["max_duration_seconds"]:
            blockers.append(f"当前采样时长 {output['actual_duration']:g} 秒超过执行池的 {envelope['max_duration_seconds']:g} 秒上限")
        if request["steps"] > envelope["max_steps"]:
            blockers.append(f"当前采样 {request['steps']} 步；执行池最多接受 {envelope['max_steps']:g} 步")
        if refs > envelope["max_reference_files"]:
            blockers.append(f"当前使用 {refs} 份参考文件；执行池最多接受 {envelope['max_reference_files']} 份，素材仍可保存")
        guides = request.get("guides", [])
        if len(guides) > envelope["max_guides"]:
            blockers.append(f"当前使用 {len(guides)} 个时间锚点；执行池最多接受 {envelope['max_guides']} 个")
        if request["generate_audio"] and not envelope["allow_audio"]:
            blockers.append("当前执行池尚未开放声音生成，请保留设置或明确关闭生成声音")
        if (request["inputs"]["first_frame"] or request["inputs"]["last_frame"]) and not envelope["allow_first_last"]:
            blockers.append("当前执行池尚未开放首尾帧输入；现有图片会保留，可明确移除关联后使用纯文字生成")
        blockers.extend(input_envelope_blockers(compiled, envelope.get("input_limits")))
        labels = {"sampler_name": "采样器", "scheduler": "调度器", "video_decode": "视频 VAE 解码",
            "audio_decode": "音频 VAE 解码", "encoder_device": "编码器设备", "ref_image_size": "参考图尺寸"}
        for field, options in envelope["controls"].items():
            if request[field] not in options:
                blockers.append(f"{labels[field]}当前为 {request[field]}；执行池仅接受 {' / '.join(options)}")
        capacity = {"ready": 0, "busy": 0}
        account_ids = [value.format(**scope.__dict__) for value in policy["budget_accounts"]]
        for account_id in account_ids:
            try:
                identifier(account_id)
                account = self.repo.get_budget(account_id)
                if (account["tenant_id"] != scope.tenant_id
                        or account["owner_id"] is not None and account["owner_id"] != scope.owner_id
                        or account["project_id"] is not None and account["project_id"] != scope.project_id):
                    raise NotFound("budget_not_found")
                if account["spent_microusd"] + account["reserved_microusd"] + quote["cost_microusd"] > account["limit_microusd"]:
                    raise BudgetExceeded("budget_exceeded")
            except BudgetExceeded:
                base["budget_error_code"] = "budget_exceeded"
                blockers.append("额度不足；可用额度不足以预留本次生成，请联系管理员调整额度。")
                break
            except (NotFound, ValueError):
                blockers.append("当前项目的生成预算未配置，请联系管理员。")
                break
        approval, cold_latest_start, member_warm = None, None, False
        if not blockers:
            capacity = self.control.pool_status(policy["pool"], model_id=policy["model_id"],
                configuration_id=policy["configuration_id"], recipe_id=compiled["recipe_id"], backend=backend,
                **({"engine_manifest_digest": policy["engine_manifest_digest"]} if backend == "wangp-worker" else {}),
                expected_runtime_s=quote["expected_runtime_s"], deployment_profile_id=policy.get("deployment_profile_id"),
                dispatch_backend=policy.get("dispatch_backend", "legacy"),
                **({"output_delivery": policy["output_delivery"]} if "output_delivery" in policy else {}))
            if capacity["ready"] + capacity["busy"] > 0:
                from .capacity import pool_members_require_warm_binding
                if pool_members_require_warm_binding(self.repo, scope.tenant_id, policy["pool"], policy["configuration_id"]):
                    candidate = self.repo.find_capacity_approval(scope, pool=policy["pool"], model_id=policy["model_id"],
                        configuration_id=policy["configuration_id"], recipe_id=compiled["recipe_id"], policy_hash=request_hash(policy))
                    member_warm = bool(candidate and candidate["payload"].get("pool_controller") == "continuing-two-members-v1"
                        and self.capacity_approval_current(candidate["payload"]))
                    if not member_warm:
                        blockers.append("此双节点执行池尚未接入完整的持续服务，请保留任务并等待启用")
        if not blockers and (capacity["ready"] + capacity["busy"] == 0 or member_warm):
            # Only an independently approved, current launch can admit a wait.
            # Empty approvals / gates=0 retain the original blocked behavior.
            approval = self.repo.find_capacity_approval(scope, pool=policy["pool"], model_id=policy["model_id"],
                configuration_id=policy["configuration_id"], recipe_id=compiled["recipe_id"], policy_hash=request_hash(policy))
            if approval is not None and not self.capacity_approval_current(approval["payload"]):
                approval = None
            if approval is None:
                blockers.extend(f"执行机暂不接收本任务：{reason}" for reason in sorted(capacity.get("reason_counts", {})))
                blockers.append("暂无已登记且心跳有效的匹配工作机，也无有效的独立冷启动审批")
            else:
                # A provider starting/running record is not a qualified slot.
                # Until a matching worker is ready, conservatively retain the
                # full approved boot allowance rather than inventing progress.
                # Warm ready/busy capacity above never pays this allowance.
                scale = approval["payload"]["scale_policy"]
                deadlines = [qualification["expires_at"], quote["expires_at"], approval["expires_at"], scale["hard_deadline"]]
                from sqlalchemy import select
                from .repository import capacity_cycles, instance_intents
                with self.repo.engine.connect() as connection:
                    actual_deadline = connection.execute(select(instance_intents.c.hard_deadline)
                        .join(capacity_cycles, capacity_cycles.c.intent_id == instance_intents.c.id)
                        .where(capacity_cycles.c.approval_id == approval["id"])).scalar_one_or_none()
                if actual_deadline is not None:
                    deadlines.append(actual_deadline)
                cold_latest_start = min(deadlines) - (0 if member_warm else scale["cold_start_s"]) - quote["expected_runtime_s"]
                if cold_latest_start <= now:
                    blockers.append("剩余运行窗口不足以启动并完成本次生成，请等待服务续期后重新预检")
        quote_known = quote["expires_at"] > now
        base.update(pool=policy["pool"], configuration_id=policy["configuration_id"], policy_revision=policy["revision"],
            policy_hash=request_hash(policy), expected_runtime_s=quote["expected_runtime_s"], quote_known=quote_known,
            enabled=not blockers, budget_account_ids=account_ids, qualification_evidence_id=qualification["evidence_id"],
            qualification_expires_at=qualification["expires_at"], quote_expires_at=quote["expires_at"],
            registered_healthy_slots=capacity["ready"]+capacity["busy"])
        base["admission_state"] = "blocked" if blockers else "waiting_capacity" if approval else "queued"
        if backend == "wangp-worker":
            base["engine_manifest_digest"] = policy["engine_manifest_digest"]
        if "dispatch_backend" in policy:
            base["dispatch_backend"] = policy["dispatch_backend"]
        if compiled.get("deployment_profile_id") is not None:
            base["deployment_profile_id"] = compiled["deployment_profile_id"]
        if "output_delivery" in policy:
            base.update(output_delivery=policy["output_delivery"],
                        delivery_spec=native_delivery_spec(compiled, policy["output_delivery"]))
        if approval:
            base.update(capacity_approval_id=approval["id"], capacity_approval_hash=approval["approval_hash"],
                **({"capacity_binding": "pool-members-v1"} if "pool_members" in approval["payload"] else {}))
        expiry = min(now+900, latest_start) if not blockers else now+900
        if approval:
            if not blockers:
                expiry = min(expiry, cold_latest_start)
        estimate = {"currency": "USD", "cost_microusd": quote["cost_microusd"] if quote_known else None,
                    "source": quote["source_id"] if quote_known else "unknown", "kind": "budget_reservation",
                    "actual_charge_known": False, "description": "运营配置的预算预留额；实际费用另行核对，不代表最终账单"}
        if "duration_reference_seconds" in quote:
            reference = quote["duration_reference_seconds"]
            estimate.update(estimate_basis="operator_allowance", duration_reference_seconds=reference,
                native_duration_seconds=output["actual_duration"],
                duration_scale_factor=max(1, output["actual_duration"] / reference),
                expected_runtime_s=quote["expected_runtime_s"], performance_scaling_verified=False,
                description="运营时长预留：按原生帧时长比例向上增加额度与执行时间，不降低原基线；不是已测速度或最终账单")
        return Admission(base, quote["cost_microusd"] if quote_known else 0, expiry, estimate)

    def ensure_current(self, plan, scope):
        previous = plan["execution_plan"]
        current = self.evaluate(plan["request"], scope, previous["fingerprint"])
        if current.execution.get("budget_error_code") == "budget_exceeded":
            raise BudgetExceeded("budget_exceeded")
        if (not previous.get("enabled") or not current.execution["enabled"]
                or previous.get("policy_hash") != current.execution.get("policy_hash")
                or previous.get("backend") != current.execution["backend"]
                or previous.get("output_delivery") != current.execution.get("output_delivery")
                or previous.get("delivery_spec") != current.execution.get("delivery_spec")
                or previous.get("backend") == "wangp-worker"
                   and previous.get("engine_manifest_digest") != current.execution.get("engine_manifest_digest")):
            from .repository import Conflict
            raise Conflict("execution_policy_changed_or_unavailable")
        if previous.get("capacity_approval_id") and not self._capacity_reference_current(previous, scope.tenant_id):
            from .repository import Conflict
            raise Conflict("capacity_approval_unavailable")
        return tuple(current.execution["budget_account_ids"])

    def capacity_approval_current(self, payload):
        """Pure current operator-file check; safe inside a ledger transaction."""
        if not self.settings.generation_enabled or self.settings.execution_backend not in REAL_GPU_BACKENDS:
            return False
        try:
            from .execution_profiles import read_profiles
            try:
                legacy = read_policy(self.settings.execution_policy_file)
            except (ValueError, OSError):
                legacy = None
            policies = [legacy]
            policies.extend(read_profiles(getattr(self.settings, "execution_profiles_file", None)).values())
            policy = next((p for p in policies if p and request_hash(p) == payload["policy_hash"]), None)
            if policy is None:
                return False
            now, qualification, quote = self.repo.clock(), policy["qualification"], policy["reservation"]
            from .capacity import _matches_engine
            return bool(policy["backend"] == self.settings.execution_backend and _matches_engine(payload, policy)
                and policy["enabled"] and request_hash(policy) == payload["policy_hash"]
                and policy["model_id"] == payload["model_id"] and policy["pool"] == payload["pool"]
                and policy["configuration_id"] == payload["configuration_id"]
                and set(payload["recipe_ids"]) <= set(policy["recipe_ids"])
                and qualification["status"] in {"accepted", "runtime_required"} and qualification["verified_at"] <= now < qualification["expires_at"]
                and qualification["evidence_id"] == payload["qualification_evidence_id"]
                and qualification["expires_at"] == payload["qualification_expires_at"]
                and now < quote["expires_at"] == payload["quote_expires_at"]
                and payload["expires_at"] > now)
        except (ValueError, KeyError, TypeError, BackendError):
            return False

    def _capacity_reference_current(self, execution, tenant_id):
        from sqlalchemy import select
        from .repository import capacity_approvals
        with self.repo.engine.connect() as connection:
            approval = connection.execute(select(capacity_approvals).where(
                capacity_approvals.c.id == execution["capacity_approval_id"], capacity_approvals.c.tenant_id == tenant_id)).mappings().first()
        return bool(approval and approval["enabled"] == 1
            and approval["approval_hash"] == execution.get("capacity_approval_hash")
            and approval["expires_at"] > self.repo.clock() and self.capacity_approval_current(approval["payload"]))

    def submission_allowed(self, job):
        """New submission guard; cold-start revocation also applies after queueing."""
        return bool(self.activation_allowed(job) and (not job["execution_plan"].get("capacity_approval_id")
            or self._capacity_reference_current(job["execution_plan"], job["tenant_id"])))

    def activation_allowed(self, job):
        """Re-check revocation immediately before a NEW upstream submission.

        Running/unknown submissions must still reconcile after policy expiry.
        The caller only uses this guard on a proven, unsubmitted attempt. Budget
        is already reserved and is not counted a second time here.
        """
        execution = job.get("execution_plan", {})
        from .render_plans import RECIPE, MODEL as RENDER_MODEL, configuration_for, POOL
        if job.get("request", {}).get("recipe_id") == RECIPE:
            try:
                configuration = configuration_for(job["request"])
            except (ValueError, AttributeError, TypeError):
                return False
            return bool(self.settings.render_enabled and execution.get("enabled") is True
                and execution.get("backend") == "cpu-render" and execution.get("pool") == POOL
                and execution.get("configuration_id") == configuration
                and job["request"]["request"].get("model") == RENDER_MODEL
                and not job["request"].get("render_blockers"))
        if (not self.settings.generation_enabled or self.settings.execution_backend != execution.get("backend")
                or execution.get("enabled") is not True):
            return False
        if self.settings.execution_backend == "mock":
            return True
        if self.settings.execution_backend not in REAL_GPU_BACKENDS:
            return False
        try:
            from .execution_profiles import selected_policy
            policy = selected_policy(self.settings, profile_id=job.get("request", {}).get("deployment_profile_id"),
                recipe_id=job.get("request", {}).get("recipe_id"))
            if policy is None:
                return False
            qualification = policy["qualification"]
            delivery_spec(job)
            quote = reservation_for_duration(policy,
                job.get("request", {}).get("output_spec", {}).get("actual_duration"))
            now = self.repo.clock()
            return bool(policy["backend"] == self.settings.execution_backend
                and execution.get("deployment_profile_id") == job.get("request", {}).get("deployment_profile_id")
                and execution.get("output_delivery", "") == policy.get("output_delivery", "")
                and (policy["backend"] != "wangp-worker" or execution.get("engine_manifest_digest") == policy["engine_manifest_digest"])
                and policy["enabled"] and execution.get("policy_hash") == request_hash(policy)
                and qualification["status"] in {"accepted", "runtime_required"}
                and qualification["verified_at"] <= now < qualification["expires_at"]
                and now < quote["expires_at"]
                and now + quote["expected_runtime_s"] < min(qualification["expires_at"], quote["expires_at"])
                and job["request"]["recipe_id"] in policy["recipe_ids"]
                and job["request"]["request"]["model"] == policy["model_id"]
                and execution.get("pool") == policy["pool"]
                and execution.get("configuration_id") == policy["configuration_id"])
        except (ValueError, KeyError, TypeError, BackendError):
            return False
