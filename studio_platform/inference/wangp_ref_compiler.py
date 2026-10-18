"""Explicit Ref2VA qualification recipe; importing enables no operating policy.

Pinned Base/BF16/50 steps, one small reference per kind. These are conservative
qualification bounds, not the upstream model's complete capability limits.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re

from .protocol import BackendError
from .wangp_compiler import (
    MODEL_ID,
    normalize_request as normalize_fl,
    compile_settings as compile_fl,
)
from .wangp_contract import InputDescriptor, PreparedRequest, canonical_json
from ..storage import key_belongs_to, validate_key

COMPILER_ID = "sixnine-h3-ref2va-bf16-50-smallrefs-v1"
PROFILE_ID = "h3-ref2va-bf16-50-sdpa-p4-smallrefs-v1"
MODEL_TYPE = "minimax_h3_ref2va"
MODEL_REVISION = "adc81ccb71352192214d83d5fafb9487e860be39"
TRANSFORMER = {
    "path": "MiniMax-H3-Ref2VA_bf16.safetensors",
    "size_bytes": 66280486944,
    "sha256": "ca877ed2b1bf72cfda76fe38117832544d6c0530abcfb9463e114cd767a39516",
}
MAX_IMAGE_PIXELS = 832 * 480
MAX_VIDEO_FRAMES = 73  # Normalization of an explicit 2--3-second source selection.
MAX_AUDIO_SECONDS = 3
RECIPE_ID = "h3-base-ref2va-v1"


def control_schema():
    from .wangp_compiler import control_schema as fl_schema

    schema = fl_schema()
    schema["duration"].update(default=5, minimum=5, maximum=5)
    schema["resolution"].update(default="480P", enum=["480P"])
    schema["aspect_ratio"].update(default="16:9", enum=["16:9"])
    schema["width"].update(minimum=832, maximum=832)
    schema["height"].update(minimum=480, maximum=480)
    return schema


def validate_envelope(envelope):
    """Additional bounds after the common operator policy type validation."""
    controls = {
        "sampler_name": ["euler"],
        "scheduler": ["auto"],
        "video_decode": ["tiled"],
        "audio_decode": ["normal"],
        "encoder_device": ["default"],
    }
    limits = envelope.get("input_limits", {})
    maxima = {
        "max_images": 1,
        "max_videos": 1,
        "max_audios": 1,
        "max_image_pixels": MAX_IMAGE_PIXELS,
        "max_video_pixels": MAX_IMAGE_PIXELS,
        "max_video_duration_seconds": MAX_VIDEO_FRAMES / 24,
        "max_audio_duration_seconds": MAX_AUDIO_SECONDS,
    }
    if (
        envelope["controls"] != controls
        or envelope["max_pixels"] > MAX_IMAGE_PIXELS
        or envelope["max_duration_seconds"] > 124 / 24
        or envelope["max_steps"] != 50
        or envelope["max_reference_files"] > 3
        or envelope["max_guides"] != 0
        or envelope["allow_first_last"] is not False
        or envelope["allow_audio"] is not True
        or any(
            key not in limits or limits[key] > maximum
            for key, maximum in maxima.items()
        )
        or limits.get("guide_kinds") != []
        or limits.get("guide_recipe_ids") != []
        or limits.get("allow_video_audio") is not False
    ):
        raise ValueError("wangp_ref_policy_outside_qualification_envelope")
    return envelope


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def normalize_request(request, metadata, output_spec):
    if (
        not isinstance(request, dict)
        or request.get("model") != MODEL_ID
        or request.get("mode") != "ref"
    ):
        raise ValueError("wangp_ref2va_base_only")
    if not isinstance(metadata, dict):
        raise ValueError("wangp_ref_metadata_invalid")
    if (
        request.get("resolution", "480P") != "480P"
        or request.get("aspect_ratio", "16:9") != "16:9"
        or any(
            field in request
            and (type(request[field]) is not int or request[field] != expected)
            for field, expected in (("width", 832), ("height", 480))
        )
    ):
        raise ValueError("wangp_ref_qualification_output_exceeded")
    inputs = copy.deepcopy(request.get("inputs", {}))
    if (
        not isinstance(inputs, dict)
        or set(inputs) - {"images", "videos", "audios", "first_frame", "last_frame"}
        or inputs.get("first_frame") is not None
        or inputs.get("last_frame") is not None
    ):
        raise ValueError("wangp_ref_first_last_or_guides_unsupported")
    ids = []
    for plural, kind in (("images", "image"), ("videos", "video"), ("audios", "audio")):
        values = inputs.setdefault(plural, [])
        if not isinstance(values, list) or len(values) > 1:
            raise ValueError("wangp_ref_qualification_count_exceeded")
        for asset_id in values:
            if not isinstance(asset_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_.-]{1,200}", asset_id
            ):
                raise ValueError("wangp_invalid_input_identity")
            meta = metadata.get(asset_id)
            if (
                not isinstance(meta, dict)
                or meta.get("kind") != kind
                or meta.get("model_ready") is not True
            ):
                raise ValueError("wangp_ref_media_not_ready")
            if kind in {"image", "video"}:
                w, h = meta.get("width"), meta.get("height")
                if (
                    type(w) is not int
                    or type(h) is not int
                    or not 256 <= min(w, h)
                    or max(w, h) > 832
                    or w * h > MAX_IMAGE_PIXELS
                    or not 0.4 <= w / h <= 2.5
                ):
                    raise ValueError("wangp_ref_qualification_pixels_exceeded")
            if kind == "video":
                frames, duration, selected = (
                    meta.get("frame_count"),
                    meta.get("duration"),
                    meta.get("source_duration"),
                )
                if (
                    meta.get("has_audio") is not False
                    or meta.get("fps") != 24
                    or type(frames) is not int
                    or not 56 <= frames <= MAX_VIDEO_FRAMES
                    or frames % 17 != 5
                    or not _number(duration)
                    or abs(duration - frames / 24) > 1e-6
                    or not _number(selected)
                    or not 2 <= selected <= MAX_AUDIO_SECONDS
                ):
                    raise ValueError("wangp_ref_silent_selected_video_required")
            if kind == "audio":
                duration = meta.get("duration")
                if (
                    not _number(duration)
                    or not 2 <= duration <= MAX_AUDIO_SECONDS
                    or meta.get("sample_rate") != 32000
                    or meta.get("channels") != 2
                ):
                    raise ValueError("wangp_ref_selected_audio_required")
            ids.append(asset_id)
    if not ids or len(set(ids)) != len(ids) or set(ids) != set(metadata):
        raise ValueError("wangp_ref_input_snapshot_mismatch")
    if len(inputs["audios"]) > len(inputs["images"]) + len(inputs["videos"]):
        raise ValueError("wangp_ref_audio_requires_visual_reference")
    expected_audio = {key: False for key in inputs["videos"]}
    if request.get("video_audio", {}) != expected_audio or any(
        type(v) is not bool for v in request.get("video_audio", {}).values()
    ):
        raise ValueError("wangp_ref_soundtrack_unsupported")
    # Reuse the immutable FL Base control validator, after validating EVERY
    # reference field above. No control/prompt/shape is silently discarded.
    base = {
        "resolution": "480P",
        **request,
        "mode": "fl",
        "inputs": {},
        "video_audio": {},
    }
    value = normalize_fl(base, {}, output_spec)
    if value["duration"] != 5 or (
        output_spec["width"],
        output_spec["height"],
        output_spec["frames"],
    ) != (832, 480, 124):
        raise ValueError("wangp_ref_qualification_output_exceeded")
    value.update(
        mode="ref",
        inputs={**inputs, "first_frame": None, "last_frame": None},
        video_audio=expected_audio,
    )
    return value


def compile_settings(request, metadata, output_spec, handles):
    value = normalize_request(request, metadata, output_spec)
    if set(handles) != set(metadata):
        raise ValueError("wangp_input_handles_mismatch")
    settings = compile_fl(
        {**value, "mode": "fl", "inputs": {}, "video_audio": {}}, {}, output_spec, {}
    )
    images, videos, audios = (
        value["inputs"][kind] for kind in ("images", "videos", "audios")
    )
    settings.update(
        model_type=MODEL_TYPE,
        image_refs=[handles[key] for key in images] or None,
        video_prompt_type=("I" if images else "") + ("V-U" if videos else ""),
        video_guide=handles[videos[0]] if videos else None,
        audio_prompt_type="A" if audios else "",
        audio_guide=handles[audios[0]] if audios else None,
        image_refs_relative_size=100,
        remove_background_images_ref=0,
    )
    return settings


class H3Ref2VACompiler:
    def __init__(self, manifest, stage_input):
        doc = manifest.document
        transformer = doc["components"].get("transformer", {})
        if (
            doc["compiler_id"] != COMPILER_ID
            or doc["profile_id"] != PROFILE_ID
            or doc.get("synthetic")
            or doc["topology"] != {"slots": 1, "gpus": 1}
            or transformer
            != {
                "repository": "DeepBeepMeep/MiniMax-H3",
                "revision": MODEL_REVISION,
                "precision": "bf16",
                "files": [TRANSFORMER],
            }
        ):
            raise ValueError("wangp_ref_compiler_manifest_mismatch")
        self.manifest, self.stage_input = manifest, stage_input

    def __call__(self, job, tag, store, heartbeat):
        try:
            compiled = job["request"]
            assets, request, output = (
                compiled.get("assets", {}),
                compiled["request"],
                compiled["output_spec"],
            )
            metadata = {key: val["metadata"] for key, val in assets.items()}
            value = normalize_request(request, metadata, output)
            if (
                compiled.get("recipe_id") != RECIPE_ID
                or job["execution_plan"].get("engine_manifest_digest")
                != self.manifest.digest
            ):
                raise ValueError("wangp_manifest_binding_mismatch")
            descriptors, handles, keys = [], {}, {}
            for plural, kind in (
                ("images", "image"),
                ("videos", "video"),
                ("audios", "audio"),
            ):
                for asset_id in value["inputs"][plural]:
                    model = assets[asset_id]["model"]
                    key = validate_key(model["key"])
                    if not key_belongs_to(key, job["owner_id"]):
                        raise ValueError("wangp_asset_owner_mismatch")
                    sha, size = model["sha256"], model["size_bytes"]
                    handle = (
                        "input-"
                        + hashlib.sha256(
                            (tag + "\0" + asset_id + "\0" + sha).encode()
                        ).hexdigest()
                    )
                    descriptors.append(
                        InputDescriptor(asset_id, handle, kind, sha, size)
                    )
                    handles[asset_id], keys[asset_id] = handle, key
            settings = compile_settings(value, metadata, output, handles)
            settings["output_filename"] = "sixnine-" + tag
            prepared = PreparedRequest(
                job["id"],
                tag,
                job["request_hash"],
                self.manifest.digest,
                canonical_json(settings),
                canonical_json(output),
                True,
                tuple(descriptors),
            )
            for item in descriptors:
                heartbeat()
                with store.open(keys[item.asset_id]) as source:
                    if self.stage_input(item, source, heartbeat=heartbeat) != item:
                        raise ValueError("wangp_staged_input_mismatch")
            return prepared
        except (KeyError, TypeError, ValueError) as error:
            code = str(error)
            if not re.fullmatch(r"wangp_[a-z0-9_]+", code):
                code = "wangp_invalid_compiled_request"
            raise BackendError(code) from None
