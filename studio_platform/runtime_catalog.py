"""Versioned native-runtime evidence and explicit, inert deployment recipes.

Historical measurements neither authorize capacity nor qualify an adapter. This
module performs local metadata reads only; it never imports a model runtime.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

PROFILE_IDS = (
    "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1",
    "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-lowram-v1",
    "h3-unpruned33b-bf16-qwenbf16-vaefp16-sdpa-p3-splitqkv-v2",
    "h3-pruned-rank8-int8-pro6000-quanto-int8-vae-int8-sdpa-p4-lowram-v1",
    "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-v2",
    "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-total-v3",
    "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-29gib-cgroups-v4",
    "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-single-fl-v2",
    "h3-pruned-rank8-int8-sdpa-p4-29gib-fl-r2-v5",
    "h3-unpruned33b-int8-qwenbf16-vaefp16-sdpa-p3-144gib-fl-r2-v3",
    "h3-pruned-rank8-int8-sdpa-p4-29gib-controls-v6",
    "h3-pruned-rank8-int8-pro6000-sdpa-p4-controls-v2",
    "h3-unpruned33b-int8-sdpa-p3-controls-v2",
    "h3-unpruned33b-bf16-sdpa-p3-controls-v3",
    "h3-unpruned33b-int8-sdpa-p3-144gib-single-controls-v4",
    "h3-unpruned33b-bf16-sdpa-p3-256gib-single-controls-v4",
    "h3-pruned-rank8-int8-sdpa-p4-29gib-taomate5-v7",
    "h3-pruned-rank8-int8-sage2-cu130-p4-29gib-taomate5-v8",
)
PROFILE_DIRECTORY = (
    Path(__file__).resolve().parents[1] / "deploy" / "wangp" / "profiles"
)
COMPILER_ID = "sixnine-h3-native-profile-v1"
FULL_COMPILER_ID = "sixnine-h3-native-profile-v2"


def compiler_id(profile):
    epoch = profile.get("compiler_epoch", "native-profile-v1")
    if epoch == "native-profile-v1":
        return COMPILER_ID
    if epoch == "full-controls-v2":
        return FULL_COMPILER_ID
    raise ValueError("wangp_profile_compiler_epoch_unsupported")


_PROFILE_TEXT = {}


def get_profile(profile_id):
    """Return a detached metadata copy; unknown IDs never select a fallback."""
    if not isinstance(profile_id, str) or profile_id not in PROFILE_IDS:
        raise ValueError("wangp_unknown_deployment_profile")
    path = PROFILE_DIRECTORY / (profile_id + ".json")
    meta = path.stat()
    key = (meta.st_ino, meta.st_size, meta.st_mtime_ns)
    cached = _PROFILE_TEXT.get(path)
    if cached is not None and cached[0] == key:
        text = cached[1]
    else:
        # Polls read every profile per request; reopen only a changed file.
        text = path.read_text(encoding="utf-8")
        _PROFILE_TEXT[path] = (key, text)
    value = json.loads(text)
    if value.get("schema_version") != 1 or value.get("id") != profile_id:
        raise ValueError("wangp_invalid_deployment_profile")
    return value


def public_catalog():
    """Public projection excludes runtime implementation configuration."""
    profiles = []
    for identity in PROFILE_IDS:
        value = get_profile(identity)
        value.pop("runtime")
        value.pop("schema_version")
        profiles.append(value)
    return {"schema_version": 1, "profiles": profiles}


def profile_output_delivery(profile_id):
    """An appended profile binds delivery; retained profiles stay unchanged."""
    from .inference.native_outputs import NATIVE_DELIVERY, GPU_R2_DELIVERY

    value = get_profile(profile_id).get("output_delivery", NATIVE_DELIVERY)
    if value not in {NATIVE_DELIVERY, GPU_R2_DELIVERY}:
        raise ValueError("wangp_profile_delivery_invalid")
    return value


def timing_hint(profile_id, mode, width, height, frames, fps, steps, roles):
    """Exact joint-envelope evidence only. No estimate or interpolation."""
    profile = get_profile(profile_id)
    if (
        not isinstance(mode, str)
        or mode not in {"fl", "ref"}
        or any(type(v) is not int for v in (width, height, frames, fps, steps))
        or not isinstance(roles, (list, tuple, set, frozenset))
        or any(not isinstance(role, str) for role in roles)
        or len(roles) != len(set(roles))
    ):
        return None
    wanted = (mode, width, height, frames, fps, steps, sorted(roles))
    cases = [
        case
        for case in profile["verified_cases"]
        if (
            case["mode"],
            case["width"],
            case["height"],
            case["frames"],
            case["fps"],
            case["steps"],
            sorted(case["input_roles"]),
        )
        == wanted
    ]
    if not cases:
        return None
    return {
        "deployment_profile_id": profile_id,
        "scope": "historical_exact_case",
        "estimated_seconds": None,
        "cases": cases,
        "warning": "Single-run historical observations with uncontrolled cache state; not a completion estimate or SLA.",
    }


def supported_cases(profile_id):
    """Explicit request envelopes; qualification candidates have no timing proof."""
    profile = get_profile(profile_id)
    return profile["verified_cases"] + profile.get("qualification_cases", [])


def model_for(profile_id, mode):
    for model in get_profile(profile_id)["models"]:
        if model["mode"] == mode:
            return model
    raise ValueError("wangp_profile_mode_unsupported")


def runtime_config(profile_id, model_root):
    """Exact tested startup values; writing/starting remains caller-owned."""
    root = Path(model_root)
    if not root.is_absolute():
        raise ValueError("wangp_absolute_model_root_required")
    return {
        **get_profile(profile_id)["runtime"]["config"],
        "checkpoints_paths": [str(root.resolve(strict=True))],
    }


def weights_manifest(profile_id, mode):
    """Exact component selection for one mode, consumable by the downloader."""
    model_for(profile_id, mode)
    components = get_profile(profile_id)["components"]
    return {
        "transformer": components["transformer_" + mode],
        **{
            key: value
            for key, value in components.items()
            if not key.startswith("transformer_")
        },
    }


def engine_manifest(profile_id, mode):
    """Unqualified manifest; an immutable profile never grants runtime readiness."""
    from .inference.wangp_contract import (
        EngineManifest,
        PROTOCOL_VERSION,
        canonical_json,
    )

    profile = get_profile(profile_id)
    model = model_for(profile_id, mode)
    runtime = profile["runtime"]
    runtime_digest = hashlib.sha256(canonical_json(runtime).encode()).hexdigest()
    document = {
        "protocol_version": PROTOCOL_VERSION,
        "engine": "wangp",
        "source_repository": "https://github.com/deepbeepmeep/Wan2GP",
        "source_revision": profile["source_revision"],
        "compiler_id": compiler_id(profile),
        "profile_id": profile_id,
        "deployment_profile_id": profile_id,
        "mode": mode,
        "model_id": profile["model_id"],
        "generation_recipe_id": model["generation_recipe_id"],
        "runtime_digest": runtime_digest,
        "runtime_digest_kind": "sixnine-native-profile-v1",
        "runtime_profile": copy.deepcopy(runtime),
        "memory_profile": "mmgp-profile-" + str(runtime["memory_profile"]),
        "kernel_profile": "sdpa-strict-no-compile",
        "topology": {"slots": 1, "gpus": 1},
        "components": weights_manifest(profile_id, mode),
        "synthetic": False,
        "inference_qualified": False,
        "production_adapter_verified": False,
    }
    # Existing profile manifests remain byte-for-byte stable. New hardware is
    # an explicit immutable binding, never inferred from the model's precision.
    if "hardware_admission" in profile:
        document["hardware_admission"] = copy.deepcopy(profile["hardware_admission"])
    if "output_delivery" in profile:
        document["output_delivery"] = profile_output_delivery(profile_id)
    return EngineManifest.from_dict(document)


def validate_manifest(manifest):
    """Reject altered mode/assets/config bindings before upload or model import."""
    document = manifest.document
    expected = engine_manifest(
        document.get("deployment_profile_id"), document.get("mode")
    )
    if document != expected.document:
        raise ValueError("wangp_profile_manifest_mismatch")
    return get_profile(document["deployment_profile_id"])
