"""One capability contract for guided UI, canvas and machine API."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import time

from comfy_workflow import (
    COMFY_COMMIT,
    ASPECT_RATIOS,
    RESOLUTIONS,
    SAMPLER_NAMES,
    SCHEDULER_NAMES,
    controls_metadata,
    native_output_spec,
    validate_controls,
    build_workflow,
)

VERSION = "sixnine-h3-base-20261004-v1"
MODEL = "MiniMax-H3-Base-BF16"
RECIPES = {"h3-base-fl2va-v1": "fl", "h3-base-ref2va-v1": "ref"}


def public_version(value):
    """Opaque creator revision; the immutable internal compiler version stays raw."""
    if not isinstance(value, str):
        return copy.deepcopy(value)
    if re.fullmatch(r"video-capabilities-[0-9a-f]{64}", value):
        return value
    return "video-capabilities-" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def version_matches(supplied, current=VERSION):
    """Accept only this exact internal revision or its deterministic public alias."""
    return isinstance(supplied, str) and supplied in (current, public_version(current))


LIMITS = {
    "max_images": 9,
    "max_videos": 3,
    "max_audios": 3,
    "max_total_files": 12,
    "min_clip_duration": 2,
    "max_clip_duration": 15,
    "max_total_video_duration": 15,
    "max_total_audio_duration": 15,
    "max_guides": 8,
}


def control_schema():
    meta = controls_metadata()
    props = {}
    for field, val in meta["ranges"].items():
        if field in {"custom_area", "custom_aspect_ratio", "guides", "seed"}:
            continue
        props[field] = {
            "type": "integer"
            if field not in {"denoise", "shift_video", "shift_audio"}
            else "number",
            "minimum": val["min"],
            "maximum": val["max"],
        }
        if "step" in val:
            props[field]["multipleOf"] = val["step"]
        if "default" in val:
            props[field]["default"] = val["default"]
    enums = {
        "resolution": (RESOLUTIONS, "768P"),
        "aspect_ratio": (ASPECT_RATIOS, "16:9"),
        "sampler_name": (SAMPLER_NAMES, "res_multistep"),
        "scheduler": (("auto", *SCHEDULER_NAMES), "auto"),
        "ref_image_size": (("match", "max"), "max"),
        "video_decode": (("normal", "tiled"), "normal"),
        "audio_decode": (("normal",), "normal"),
        "encoder_device": (("default", "cpu"), "default"),
    }
    for field, (choices, default) in enums.items():
        props[field] = {"type": "string", "enum": list(choices), "default": default}
    props["seed"] = {
        "type": ["string", "null"],
        "default": None,
        "pattern": "^[0-9]{1,20}$",
        "maximum_decimal": "18446744073709551615",
        "description": "留空由服务生成，完整64位十进制字符串",
    }
    props["generate_audio"] = {"type": "boolean", "default": True}
    for field in ("shift_video", "shift_audio"):
        props[field].update(type=["number", "null"], default=None, experimental=True)
    props["video_audio"] = {
        "type": "object",
        "additionalProperties": {"type": "boolean"},
        "default": {},
    }
    props["guides"] = {
        "type": "array",
        "maxItems": 8,
        "default": [],
        "items": {
            "type": "object",
            "required": ["media_id", "time_seconds"],
            "additionalProperties": False,
            "properties": {
                "media_id": {"type": "string"},
                "time_seconds": {"type": "number", "minimum": 0},
                "use_audio": {"type": "boolean", "default": False},
            },
        },
    }
    for field in ("audio_tile_size", "audio_overlap"):
        props[field]["available"] = False
        props[field]["reason"] = "当前H3音频VAE分块不兼容；参数不开放修改"
    return props


def capabilities(settings):
    from .media import EXTENSIONS

    offline = not settings.generation_enabled
    recipes = [
        {
            "id": key,
            "label": "H3 首尾帧 / 文生音视频" if mode == "fl" else "H3 全能参考",
            "mode": mode,
            "model_id": MODEL,
            "implemented": True,
            "enabled": settings.generation_enabled,
            "capacity_state": "offline" if offline else "requires_worker",
            "validation_level": "historical_inference_not_current_pool_validation",
            "controls": control_schema(),
            "limits": dict(LIMITS),
            "custom_canvas_constraints": {
                "maximum_pixel_area": 768 * 1344,
                "minimum_aspect_ratio": 0.4,
                "maximum_aspect_ratio": 2.5,
                "description": "自定义宽高须为32的倍数，总面积不超过768×1344，宽高比0.4–2.5",
            },
            "source": {"comfyui_revision": COMFY_COMMIT},
        }
        for key, mode in RECIPES.items()
    ]
    if settings.execution_backend == "wangp-worker":
        from .inference.wangp_compiler import control_schema as wangp_controls
        from .inference.wangp_ref_compiler import control_schema as ref_controls
        from .inference.wangp_contract import UPSTREAM_REVISION

        for recipe in recipes:
            supported = recipe["mode"] == "fl"
            recipe.update(
                implemented=True,
                enabled=settings.generation_enabled and supported,
                controls=wangp_controls() if supported else ref_controls(),
                validation_level="implementation_only_not_gpu_qualified",
                source={"wangp_revision": UPSTREAM_REVISION},
            )
            if supported:
                recipe["limits"] = {
                    **LIMITS,
                    "max_images": 0,
                    "max_videos": 0,
                    "max_audios": 0,
                    "max_total_files": 2,
                    "max_guides": 0,
                }
            else:
                recipe["limits"] = {
                    **LIMITS,
                    "max_images": 1,
                    "max_videos": 1,
                    "max_audios": 1,
                    "max_total_files": 3,
                    "max_guides": 0,
                    "max_clip_duration": 3,
                    "max_total_video_duration": 73 / 24,
                    "max_total_audio_duration": 3,
                }
                recipe["custom_canvas_constraints"].update(
                    maximum_pixel_area=832 * 480,
                    description="First REF qualification requires 832x480 output; wider controls are not enabled.",
                )
    for recipe in recipes:
        recipe["execution_support"] = {
            "status": "disabled"
            if offline
            else "simulation"
            if settings.execution_backend == "mock"
            else "unavailable",
            "reason": "生成执行尚未开启，作品与素材可以继续编辑。"
            if offline
            else "当前为模拟流程，不代表真实模型生成。"
            if settings.execution_backend == "mock"
            else "当前执行范围尚未确认；模型支持的输入不代表已在此云端开放。",
            "capacity_checked": False,
            "preflight_required": True,
        }
    if settings.generation_enabled and settings.execution_backend in {
        "comfy-worker",
        "wangp-worker",
    }:
        # Public operational defaults are separate from model defaults. Never
        # serialize the operator policy (budget identities/pool bindings), and
        # never modify compile_request or a caller's explicitly chosen values.
        from .execution_policy import read_policy, authority_current, authority_deadline
        from .qualification_profiles import QUEUED_TASK_PROFILE

        try:
            policy = read_policy(settings.execution_policy_file)
        except ValueError:
            policy = None
        now = time.time()
        if (
            policy
            and policy["enabled"]
            and policy["backend"] == settings.execution_backend
            and authority_current(
                policy, now, policy["reservation"]["expected_runtime_s"]
            )
        ):
            envelope = policy["envelope"]["controls"]
            preset = {
                field: value
                for field, value in (
                    ("encoder_device", "cpu"),
                    ("video_decode", "tiled"),
                )
                if envelope[field] == [value]
            }
            if settings.execution_backend == "wangp-worker":
                preset = {}
            available_recipes = [
                recipe
                for recipe in recipes
                if recipe["implemented"]
                and recipe["id"] in policy["recipe_ids"]
                and not (
                    settings.execution_backend == "wangp-worker"
                    and recipe["mode"] == "ref"
                    and policy["recipe_ids"] != [recipe["id"]]
                )
            ]
            available = [recipe["label"] for recipe in available_recipes]
            runtime_required = policy["qualification"]["status"] == "runtime_required"
            queued_task_validation = (
                policy["qualification"].get("profile") == QUEUED_TASK_PROFILE
            )
            for recipe in recipes:
                qualified = recipe in available_recipes
                if (
                    settings.execution_backend == "wangp-worker"
                    and recipe["mode"] == "ref"
                ):
                    recipe["enabled"] = qualified
                recipe["execution_support"] = {
                    "status": ("runtime_required" if runtime_required else "qualified")
                    if qualified
                    else "not_qualified",
                    "reason": (
                        "可提交，GPU完成启动检查后直接执行队列中的真实任务；成功结果直接交付，失败会反馈原因并暂停该工作机接新任务。仍需预检账户额度与容量窗口。"
                        if queued_task_validation
                        else "可提交，GPU启动后先验证当前模式；验证通过后才执行原任务。仍需预检账户额度与容量窗口。"
                        if runtime_required
                        else "此模式已有受限的执行范围；符合范围后仍需预检账户额度与实际计算容量。"
                    )
                    if qualified
                    else "此生成方式尚未在当前云端开放。当前仅开放："
                    + "、".join(available)
                    + "。素材和设置可以继续保存。",
                    "capacity_checked": False,
                    "preflight_required": True,
                    "runtime_verification_required": runtime_required,
                    "verification_method": "queued_user_task"
                    if queued_task_validation
                    else "startup_suite"
                    if runtime_required
                    else "historical_qualification",
                    "available_recipe_ids": [item["id"] for item in available_recipes],
                    "expires_at": (
                        authority_deadline(policy)
                        - policy["reservation"]["expected_runtime_s"]
                        if authority_deadline(policy) is not None
                        else None
                    ),
                }
                if qualified:
                    if "output_delivery" in policy:
                        recipe["execution_support"]["output_delivery"] = {
                            "policy": policy["output_delivery"],
                            "fps": 24,
                            "duration_source": "generation_plan.execution.delivery_spec.duration_s",
                            "semantics": "Preserve all native frames and generated audio; requested integer seconds may round upward. No retiming or padding.",
                        }
                    # This allowlisted envelope contains input/control limits,
                    # never account, worker, approval or budget identities.
                    recipe["execution_support"]["constraints"] = copy.deepcopy(
                        policy["envelope"]
                    )
                    if "duration_reference_seconds" in policy["reservation"]:
                        # Publish only these documented duration semantics,
                        # never the full operator reservation / policy object.
                        recipe["execution_support"]["duration_allowance"] = {
                            "reference_native_duration_seconds": policy["reservation"][
                                "duration_reference_seconds"
                            ],
                            "baseline_runtime_allowance_s": policy["reservation"][
                                "expected_runtime_s"
                            ],
                            "scale_rule": "max(1, native_duration_seconds / reference_native_duration_seconds)",
                            "rounding": "runtime and microusd reservations round upward",
                            "estimate_basis": "operator_allowance",
                            "performance_scaling_verified": False,
                            "expires_at_semantics": "baseline_latest_start; request_specific_preflight_required",
                            "native_sampling_duration_source": "generation_plan.output_spec.actual_duration",
                            "requested_export_duration_source": "generation_plan.execution.delivery_spec.duration_s"
                            if "output_delivery" in policy
                            else "controls.duration",
                            "actual_output_duration_source": "verified_artifact_metadata",
                            "description": "expires_at是基线预留的最晚开始时间；具体时长和冷启动必须预检。额度与运行时间按原生补帧采样时长向上预留，不代表已测速度或最终费用。原生采样时长与请求导出时长分别处理，真实成片以核验文件为准。",
                        }
                    if "input_limits" in policy["envelope"]:
                        recipe["execution_support"]["input_limit_semantics"] = {
                            "counts": "unique reference and guide assets per kind; first/last frames have separate slots",
                            "pixels": "every image/video asset, including first/last frames and guides",
                            "duration": "total distinct assets per kind; both source selections and normalized copies must fit",
                            "metadata": "server-inspected model input metadata; client-provided media labels are not evidence",
                        }
                    if preset:
                        recipe["deployment_preset"] = {
                            "label": "当前云端显存预设",
                            "controls": dict(preset),
                            "applies_to": "unset_controls_only",
                            "source": "current_operator_execution_policy",
                            "description": "当前执行池要求这些控制值。新镜头的未设置项采用此预设；已有明确选择保留。API调用请显式传入，最终以预检为准。",
                        }
    upload_geometry = {
        "min_side": 256,
        "max_side": 5760,
        "min_aspect_ratio": 0.4,
        "max_aspect_ratio": 2.5,
    }
    from .execution_profiles import public_profiles, default_profile_id

    profiles = public_profiles(settings)
    return {
        "capabilities_version": VERSION,
        "story_film": {
            "enabled": settings.generation_queue == "dbos" and settings.render_enabled,
            "film_cuts": settings.generation_queue == "dbos"
            and settings.render_enabled,
            "workflow_version": "inkseq-film-v1",
            "preflight": "/v1/projects/{project_id}/films/preflights",
            "submit": "/v1/projects/{project_id}/films",
            "read": "/v1/projects/{project_id}/films/{run_id}",
            "cancel": "/v1/projects/{project_id}/films/{run_id}/cancel",
            "resume": "/v1/projects/{project_id}/films/{run_id}/resume",
        },
        "recipes": recipes,
        "deployment_profiles": profiles,
        "library": __import__(
            "studio_platform.library", fromlist=["contract"]
        ).contract(),
        "content_lifecycle": {
            "version": 1,
            "preview": "/v1/trash/preview",
            "actions": "/v1/trash/actions",
            "list": "/v1/trash",
            "target_kinds": [
                "project",
                "entity",
                "look",
                "location_version",
                "asset",
                "artifact",
                "take",
            ],
            "automatic_expiry": False,
            "permanent_confirmation_required": True,
            "conversation_history": "/v1/quick-chat/sessions/history/preview",
        },
        "default_deployment_profile_id": default_profile_id(settings, profiles),
        "upload_constraints": {
            "max_bytes": settings.max_upload_bytes,
            "allowed_extensions": dict(EXTENSIONS),
            "image": {**upload_geometry, "single_frame_only": True},
            "video": {
                **upload_geometry,
                "min_source_duration_seconds": 0.1,
                "max_source_duration_seconds": 3600,
            },
            "audio": {
                "min_source_duration_seconds": 0.1,
                "max_source_duration_seconds": 3600,
            },
            "scope": "upload_inspection_only",
            "description": "上传校验与模型输入、执行池范围分别检查。上传成功不代表可直接生成；视频/音频可能需要明确选择2–15秒片段。实际执行还须通过配方与execution_support范围及生成预检。",
        },
        "execution_enabled": settings.generation_enabled,
        "execution_backend": settings.execution_backend,
        "simulation": settings.execution_backend == "mock",
        "unavailable": controls_metadata()["unavailable"],
        "notes": [
            "是否已实现、历史推理证据、当前执行容量分别表示。",
            "音频分块不可用；快速VDN和外部API没有暗中替代原版模型。",
        ],
    }


def compile_request(body: dict, resolve_asset, *, backend="comfy-worker"):
    """Validate immutable inputs without loading weights or accessing providers."""
    permitted = {
        "client_ref",
        "recipe_id",
        "capabilities_version",
        "prompt",
        "inputs",
        "controls",
        "execution_policy_id",
        "client_edit",
        "deployment_profile_id",
    }
    if not isinstance(body, dict) or set(body) - permitted:
        raise ValueError("计划包含不支持的字段")
    if body.get("execution_policy_id", "self-hosted-default") != "self-hosted-default":
        raise ValueError("尚未接入此执行策略；不会静默换服务商或模型")
    if not isinstance(body.get("client_edit", {}), dict):
        raise ValueError("剪辑意图必须为对象")
    if not version_matches(body.get("capabilities_version", VERSION), VERSION):
        raise ValueError("生成能力已变化，请刷新后重新预检")
    recipe = body.get("recipe_id")
    if not isinstance(recipe, str) or recipe not in RECIPES:
        raise ValueError("未接入此配方；内部recipe ID不能替代上游model ID")
    profile_id = body.get("deployment_profile_id")
    profile = None
    if profile_id is not None:
        from .runtime_catalog import get_profile

        profile = get_profile(profile_id)
        if backend != "wangp-worker":
            raise ValueError("所选部署配方需要 WanGP 执行服务；不会改用其他模型")
    ref = body.get("client_ref")
    if not isinstance(ref, dict) or set(ref) - {
        "project_id",
        "chapter_id",
        "scene_id",
        "shot_id",
        "shot_version",
        "source_hash",
    }:
        raise ValueError("client_ref来源关联无效")
    for key in ("project_id", "shot_id"):
        if not isinstance(ref.get(key), str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,160}", ref[key]
        ):
            raise ValueError("项目和镜头ID无效")
    for key in ("chapter_id", "scene_id"):
        if ref.get(key) is not None and (
            not isinstance(ref[key], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", ref[key])
        ):
            raise ValueError("章节或场景ID无效")
    if ref.get("source_hash") is not None and (
        not isinstance(ref["source_hash"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", ref["source_hash"])
    ):
        raise ValueError("来源快照校验值无效")
    if type(ref.get("shot_version")) is not int or ref["shot_version"] < 1:
        raise ValueError("镜头版本无效")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
        raise ValueError("请输入1至12000字符的单镜提示词")
    controls = copy.deepcopy(body.get("controls", {}))
    if profile is not None:
        from .inference.wangp_profile_dispatch import control_schema as profile_schema

        selected_schema = lambda: profile_schema(profile_id, RECIPES[recipe])
    elif backend == "wangp-worker":
        if RECIPES[recipe] == "ref":
            from .inference.wangp_ref_compiler import control_schema as selected_schema
        else:
            from .inference.wangp_compiler import control_schema as selected_schema
    else:
        selected_schema = control_schema
    if not isinstance(controls, dict) or set(controls) - set(selected_schema()):
        raise ValueError("控制项包含未支持的字段")
    for field in ("audio_tile_size", "audio_overlap"):
        if field in controls:
            raise ValueError("音频分块控制尚不可用")
    if controls.get("seed") in (None, ""):
        controls["seed"] = str(
            secrets.randbits(32 if backend == "wangp-worker" else 64)
        )
    raw_inputs = body.get("inputs", {})
    if not isinstance(raw_inputs, dict) or set(raw_inputs) - {
        "images",
        "videos",
        "audios",
        "first_frame",
        "last_frame",
        "guides",
    }:
        raise ValueError("输入素材区域无效")
    inputs = {
        "images": [],
        "videos": [],
        "audios": [],
        "first_frame": None,
        "last_frame": None,
    }
    video_audio = copy.deepcopy(controls.get("video_audio", {}))
    if not isinstance(video_audio, dict):
        raise ValueError("视频原声开关必须为对象")
    for kind in ("images", "videos", "audios"):
        values = raw_inputs.get(kind, [])
        if not isinstance(values, list) or len(values) > LIMITS["max_" + kind]:
            raise ValueError("素材数量超过此输入区域的限制")
        for value in values:
            if isinstance(value, dict):
                if set(value) - {"asset_id", "purpose", "include_audio"}:
                    raise ValueError("素材引用有未支持字段；选段应先创建派生素材")
                asset_id = value.get("asset_id")
                if not isinstance(asset_id, str) or not 1 <= len(asset_id) <= 160:
                    raise ValueError("素材引用需要asset_id")
                if "purpose" in value and (
                    not isinstance(value["purpose"], str) or len(value["purpose"]) > 120
                ):
                    raise ValueError("素材用途说明无效")
                if "include_audio" in value and (
                    kind != "videos" or type(value["include_audio"]) is not bool
                ):
                    raise ValueError("仅参考视频支持布尔类型的原声开关")
                if kind == "videos" and "include_audio" in value:
                    video_audio[asset_id] = value["include_audio"]
            else:
                asset_id = value
            if not isinstance(asset_id, str) or not 1 <= len(asset_id) <= 160:
                raise ValueError("素材引用需要asset_id")
            inputs[kind].append(asset_id)
    for name in ("first_frame", "last_frame"):
        val = raw_inputs.get(name)
        if isinstance(val, dict) and set(val) != {"asset_id"}:
            raise ValueError("首尾帧只接受asset_id")
        inputs[name] = val.get("asset_id") if isinstance(val, dict) else val
        if inputs[name] is not None and not isinstance(inputs[name], str):
            raise ValueError("首尾帧引用无效")
    if "guides" in raw_inputs:
        if "guides" in controls:
            raise ValueError("时间锚点不能重复在inputs和controls中定义")
        # Saved drafts include an empty guide region. It is an absence of
        # conditioning, not a request for a control unsupported by WanGP.
        # Nonempty or malformed values must still be rejected, never dropped.
        if backend != "wangp-worker" or raw_inputs["guides"] != []:
            controls["guides"] = raw_inputs["guides"]
    if (
        not isinstance(controls.get("guides", []), list)
        or len(controls.get("guides", [])) > LIMITS["max_guides"]
    ):
        raise ValueError("时间锚点必须是数组")
    ids = [
        *inputs["images"],
        *inputs["videos"],
        *inputs["audios"],
        *[x for x in (inputs["first_frame"], inputs["last_frame"]) if x],
    ]
    full_native = (
        profile is not None and profile.get('compiler_epoch') == 'full-controls-v2'
    )
    if len(ids) != len(set(ids)) and not full_native:
        raise ValueError("同一参考素材不能重复添加；时间锚点可复用")
    if len(ids) > LIMITS["max_total_files"] and not full_native:
        raise ValueError("参考素材总数不能超过12份")
    for guide in controls.get("guides", []):
        if not isinstance(guide, dict) or not isinstance(guide.get("media_id"), str):
            raise ValueError("时间锚点需要media_id")
        ids.append(guide["media_id"])
    assets = {key: resolve_asset(key) for key in dict.fromkeys(ids)}
    metadata = {key: val["metadata"] for key, val in assets.items()}
    request = {
        "backend": "comfy-local",
        "model": profile["model_id"] if profile else MODEL,
        "mode": RECIPES[recipe],
        "prompt": prompt.strip(),
        "duration": 5,
        "resolution": "480P"
        if profile is not None or backend == "wangp-worker" and RECIPES[recipe] == "ref"
        else "768P",
        "aspect_ratio": "16:9",
        "generate_audio": True,
        **controls,
        "inputs": inputs,
        "video_audio": video_audio,
    }
    if (
        type(request["duration"]) is not int
        or type(request["generate_audio"]) is not bool
    ):
        raise ValueError("生成时长须为整数秒，声音开关须为布尔值")
    spec = native_output_spec(request)
    if profile is not None:
        from .inference.wangp_profile_dispatch import (
            normalize_request as normalize_profile,
        )

        request = normalize_profile(request, metadata, spec, profile_id)
    elif backend == "wangp-worker":
        if RECIPES[recipe] == "ref":
            from .inference.wangp_ref_compiler import normalize_request
        else:
            from .inference.wangp_compiler import normalize_request
        request = normalize_request(request, metadata, spec)
    else:
        validated = validate_controls(request, metadata, spec)
        request.update(validated)
        request["seed"] = str(request["seed"])
        # Offline graph construction exercises the historical pinned validator.
        build_workflow(
            request,
            metadata,
            {
                key: key
                + "."
                + {"image": "png", "video": "mp4", "audio": "wav"}[
                    metadata[key]["kind"]
                ]
                for key in metadata
            },
        )
    normalized = {
        "recipe_id": recipe,
        "capabilities_version": VERSION,
        "client_ref": ref,
        "request": request,
        "output_spec": spec,
        "assets": assets,
        "client_edit": body.get("client_edit", {}),
        "execution_policy_id": body.get("execution_policy_id", "self-hosted-default"),
    }
    if profile_id is not None:
        normalized["deployment_profile_id"] = profile_id
    fingerprint = hashlib.sha256(
        json.dumps(
            normalized,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    return normalized, fingerprint
