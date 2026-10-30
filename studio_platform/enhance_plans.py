"""Enhance one finished video take to 1080P or 2K (#795, docs/ENHANCE.md).

A plan like chapter render: the source is the caller's own validated video
artifact, resolved by the render source resolver. Execution is the
`runpod-enhance` backend on pool `enhance`; this module only compiles and
prices. It never contacts a provider.
"""

import math

RECIPE = "h3-enhance-v1"
POOL = "enhance"
BACKEND = "runpod-enhance"
MODEL = "open-h3-regenerate-2k"
CONFIGURATION = "runpod-enhance-v1"
FPS = 24
MAX_SOURCE_BYTES = 512 * 1024**2
MAX_DURATION_S = 30
# tier -> (output size, worker settings) from the #795 measurements.
TIERS = {
    "1080p": ((1920, 1080), {"res": "1080", "steps": 2, "sigma": 0.0}),
    "2k": ((2560, 1440), {"res": "2k", "steps": 3, "sigma": 0.8}),
}
# Owner 2026-10-30 (#795): credits per standard clip, then per started extra
# second. The base covers a standard 124-frame take (5.17 s), up to 5.25 s.
PRICES = {"1080p": (100, 10), "2k": (150, 30)}
BASE_SECONDS = 5.25


def credits(tier, duration_s):
    base, per_second = PRICES[tier]
    extra = max(0, math.ceil(duration_s - BASE_SECONDS - 1e-9))
    return base + per_second * extra


def compile_enhance(body, project, source_resolver):
    """Return (compiled, display). Raises ValueError with a user-facing message."""
    if not isinstance(body, dict) or set(body) != {"client_ref", "tier"}:
        raise ValueError("提升清晰度只接受成片与清晰度档位")
    ref, tier = body["client_ref"], body["tier"]
    if (
        not isinstance(ref, dict)
        or set(ref) != {"project_id", "entity_id"}
        or ref["project_id"] != project["id"]
        or not isinstance(ref["entity_id"], str)
    ):
        raise ValueError("需要明确的项目与成片")
    if tier not in TIERS:
        raise ValueError("清晰度只支持 1080P 或 2K")
    entity = next(
        (e for e in project.get("entities", []) if e.get("id") == ref["entity_id"]),
        None,
    )
    if (
        entity is None
        or entity.get("type") != "video"
        or not entity.get("data", {}).get("cloudArtifactId")
    ):
        raise ValueError("只能为已生成的视频成片提升清晰度")
    source = source_resolver(entity, "video")
    duration = source.get("duration")
    if (
        source.get("kind") != "video"
        or not source.get("artifact_id")
        or type(duration) not in (int, float)
        or not 0 < duration <= MAX_DURATION_S
        or not 0 < source["object"]["size_bytes"] <= MAX_SOURCE_BYTES
    ):
        raise ValueError("成片格式、时长或大小不符合提升清晰度要求")
    (width, height), settings = TIERS[tier]
    frames = round(duration * FPS)
    compiled = {
        "recipe_id": RECIPE,
        "client_ref": ref,
        "request": {
            "model": MODEL,
            "tier": tier,
            "duration": duration,
            "enhance": {
                "version": 1,
                **settings,
                "audio": "draft",
                "unet": "int8",
                "source_artifact_id": source["artifact_id"],
                "source_job_id": source["source_job_id"],
            },
        },
        "sources": {
            ref["entity_id"]: {f: source[f] for f in ("kind", "object", "simulation")}
        },
        "output_spec": {
            "width": width,
            "height": height,
            "fps": FPS,
            "frame_count": frames,
            "actual_duration": duration,
        },
        "simulation": bool(source.get("simulation")),
    }
    return compiled, {
        "tier": tier,
        "duration": duration,
        "width": width,
        "height": height,
        "credits": credits(tier, duration),
    }
