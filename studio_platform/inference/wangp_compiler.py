"""Pure, deliberately narrow mapping to the pinned WanGP H3 FL2VA engine.

This is a new-request recipe, not a migration of accepted Comfy requests.
Neither importing nor normalizing starts a runtime or accesses storage.
"""

from __future__ import annotations

import copy
import hashlib
import re

from .protocol import BackendError
from .wangp_contract import (
    EngineManifest,
    InputDescriptor,
    PreparedRequest,
    StoredInput,
    canonical_json,
)
from ..storage import key_belongs_to, validate_key

COMPILER_ID = "sixnine-h3-fl2va-bf16-50-v1"
PROFILE_ID = "h3-fl2va-bf16-50-sdpa-p4-v1"
MODEL_TYPE = "minimax_h3_fl2va"
MODEL_ID = "MiniMax-H3-Base-BF16"
# The pinned runtime calls numpy.random.seed before H3 inference. Unlike the
# Comfy contract, that entry point only accepts an unsigned 32-bit seed.
MAX_SEED = (1 << 32) - 1
FIXED_CONTROLS = {
    "steps": 50,
    "sampler_name": "euler",
    "scheduler": "auto",
    "denoise": 1,
    "shift_video": 12,
    "shift_audio": 3,
    "video_decode": "tiled",
    "video_tile_size": 256,
    "video_overlap": 64,
    "audio_decode": "normal",
    "encoder_device": "default",
    "generate_audio": True,
    "export_crf": 18,
}


def control_schema():
    """Machine-readable first-recipe envelope; no claim of full H3 parity."""
    result = {
        key: {
            "type": "boolean"
            if isinstance(value, bool)
            else "string"
            if isinstance(value, str)
            else "number"
            if key in {"denoise", "shift_video", "shift_audio"}
            else "integer",
            "default": value,
            "enum": [value],
        }
        for key, value in FIXED_CONTROLS.items()
    }
    result.update(
        {
            "duration": {
                "type": "integer",
                "default": 5,
                "minimum": 4,
                "maximum": 15,
                "multipleOf": 1,
            },
            "resolution": {
                "type": "string",
                "default": "768P",
                "enum": ["480P", "576P", "768P", "custom"],
            },
            "aspect_ratio": {
                "type": "string",
                "default": "16:9",
                "enum": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
            },
            "width": {
                "type": "integer",
                "minimum": 256,
                "maximum": 1536,
                "multipleOf": 32,
            },
            "height": {
                "type": "integer",
                "minimum": 256,
                "maximum": 1536,
                "multipleOf": 32,
            },
            "seed": {
                "type": ["string", "null"],
                "default": None,
                "pattern": "^[0-9]{1,20}$",
                "maximum_decimal": str(MAX_SEED),
            },
        }
    )
    return result


def normalize_request(request, metadata, output_spec):
    # fmt: off
    """Validate a new request before admission; reject unimplemented controls.

Native frames come from the existing immutable output specification. No trim,
quantization, scheduler substitution or best-effort control dropping occurs.
"""
    # fmt: on
    if (
        not isinstance(request, dict)
        or not isinstance(metadata, dict)
        or not isinstance(output_spec, dict)
    ):
        raise ValueError("wangp_invalid_request")
    allowed = set(control_schema()) | {
        "backend",
        "model",
        "mode",
        "prompt",
        "inputs",
        "video_audio",
    }
    if set(request) - allowed:
        raise ValueError("wangp_unsupported_controls")
    value = copy.deepcopy(request)
    value["backend"] = "wangp-local"
    if value.get("mode") != "fl" or value.get("model") != MODEL_ID:
        raise ValueError("wangp_fl2va_base_only")
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
        raise ValueError("wangp_invalid_prompt")
    # Null shift means the already-defined H3 default; preserve it as explicit.
    for key, expected in FIXED_CONTROLS.items():
        actual = value.get(key, expected)
        if key in {"shift_video", "shift_audio"} and actual is None:
            actual = expected
        if actual != expected or isinstance(actual, bool) != isinstance(expected, bool):
            raise ValueError("wangp_unsupported_" + key)
        value[key] = expected
    seed = value.get("seed", "0")
    if (
        isinstance(seed, bool)
        or not isinstance(seed, (int, str))
        or not re.fullmatch(r"[0-9]{1,20}", str(seed))
    ):
        raise ValueError("wangp_invalid_seed")
    if not 0 <= int(seed) <= MAX_SEED:
        raise ValueError("wangp_invalid_seed")
    value["seed"] = str(int(seed))
    duration = value.get("duration", 5)
    if type(duration) is not int or not 4 <= duration <= 15:
        raise ValueError("wangp_invalid_duration")
    value.setdefault("duration", duration)
    value.setdefault("resolution", "768P")
    value.setdefault("aspect_ratio", "16:9")
    from comfy_workflow import native_output_spec

    if native_output_spec(value) != output_spec:
        raise ValueError("wangp_output_spec_mismatch")
    frames = output_spec.get("frames")
    if type(frames) is not int or not 107 <= frames <= 362 or frames % 17 != 5:
        raise ValueError("wangp_invalid_native_frames")
    inputs = value.get("inputs", {})
    if not isinstance(inputs, dict) or set(inputs) - {
        "images",
        "videos",
        "audios",
        "first_frame",
        "last_frame",
    }:
        raise ValueError("wangp_invalid_inputs")
    if any(inputs.get(key) not in (None, []) for key in ("images", "videos", "audios")):
        raise ValueError("wangp_first_last_images_only")
    if value.get("video_audio", {}) != {}:
        raise ValueError("wangp_reference_audio_unsupported")
    ids = []
    for field in ("first_frame", "last_frame"):
        asset_id = inputs.get(field)
        if asset_id is not None:
            if not isinstance(asset_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_.-]{1,200}", asset_id
            ):
                raise ValueError("wangp_invalid_input_identity")
            meta = metadata.get(asset_id)
            if not isinstance(meta, dict) or meta.get("kind") != "image":
                raise ValueError("wangp_input_must_be_image")
            ids.append(asset_id)
    if len(set(ids)) != len(ids) or set(ids) != set(metadata):
        raise ValueError("wangp_input_snapshot_mismatch")
    value["inputs"] = {
        "images": [],
        "videos": [],
        "audios": [],
        "first_frame": inputs.get("first_frame"),
        "last_frame": inputs.get("last_frame"),
    }
    value["video_audio"] = {}
    return value


def compile_settings(request, metadata, output_spec, handles):
    value = normalize_request(request, metadata, output_spec)
    if set(handles) != set(metadata):
        raise ValueError("wangp_input_handles_mismatch")
    first, last = (value["inputs"][key] for key in ("first_frame", "last_frame"))
    # Opaque handles are resolved to private local paths by the host, never URLs.
    return {
        "model_type": MODEL_TYPE,
        "config": "bf16,bf16",
        "image_mode": 0,
        "prompt": value["prompt"],
        "negative_prompt": "",
        "alt_prompt": "",
        "resolution": f"{output_spec['width']}x{output_spec['height']}",
        "video_length": output_spec["frames"],
        "force_fps": "24",
        "num_inference_steps": 50,
        "seed": int(value["seed"]),
        "guidance_scale": 1.0,
        "guidance_phases": 1,
        "flow_shift": 12.0,
        "sample_solver": "euler",
        "denoising_strength": 1.0,
        "image_prompt_type": ("S" if first else "T") + ("E" if last else ""),
        "image_start": handles.get(first),
        "image_end": handles.get(last),
        "video_prompt_type": "",
        "audio_prompt_type": "",
        "image_refs": None,
        "video_source": None,
        "video_guide": None,
        "video_guide2": None,
        "video_guide3": None,
        "audio_guide": None,
        "audio_guide2": None,
        "audio_guide3": None,
        "audio_source": None,
        "repeat_generation": 1,
        "batch_size": 1,
        "multi_prompts_gen_type": "FG",
        "multi_images_gen_type": 0,
        "prompt_enhancer": "",
        "activated_loras": [],
        "skip_steps_cache_type": "",
        "override_attention": "sdpa",
        "override_profile": 4,
        "guidance2_scale": 1.0,
        "guidance3_scale": 1.0,
        "sliding_window_size": 362,
        "sliding_window_overlap": 18,
        "sliding_window_discard_last_frames": 0,
        "sliding_window_trim_first_frames": 0,
        "temporal_upsampling": "",
        "spatial_upsampling": "",
        "postprocess_audio": "",
        "custom_settings": {"audio_refinement": "none"},
        "_api": {
            "return_audio": True,
            "return_video_uint8": False,
            "return_side_files": False,
        },
    }


class H3FL2VACompiler:
    """Stage hash-bound immutable assets through an injected private transport."""

    def __init__(self, manifest: EngineManifest, stage_input):
        doc = manifest.document
        if (
            doc["compiler_id"] != COMPILER_ID
            or doc["profile_id"] != PROFILE_ID
            or doc.get("synthetic")
        ):
            raise ValueError("wangp_compiler_manifest_mismatch")
        self.manifest, self.stage_input = manifest, stage_input

    def __call__(self, job, tag, store, heartbeat):
        try:
            compiled = job["request"]
            request, output = compiled["request"], compiled["output_spec"]
            assets = compiled.get("assets", {})
            metadata = {key: val["metadata"] for key, val in assets.items()}
            normalize_request(request, metadata, output)
            if (
                job["execution_plan"].get("engine_manifest_digest")
                != self.manifest.digest
            ):
                raise ValueError("wangp_manifest_binding_mismatch")
            descriptors, handles, keys = [], {}, {}
            # Validate every ownership and snapshot identity before any upload.
            for asset_id, snapshot in assets.items():
                model = snapshot["model"]
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
                item = InputDescriptor(asset_id, handle, "image", sha, size)
                descriptors.append(item)
                handles[asset_id], keys[asset_id] = handle, key
            settings = compile_settings(request, metadata, output, handles)
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
                with StoredInput(store, keys[item.asset_id]) as source:
                    if self.stage_input(item, source, heartbeat=heartbeat) != item:
                        raise ValueError("wangp_staged_input_mismatch")
            return prepared
        except (KeyError, TypeError, ValueError) as error:
            code = str(error)
            if not re.fullmatch(r"wangp_[a-z0-9_]+", code):
                code = "wangp_invalid_compiled_request"
            raise BackendError(code) from None
