"""Bounded display copies of an already inspected image; never generation inputs.

All objects remain part of the original immutable asset and upload receipt.
This module neither reads credentials nor performs storage or database IO.
"""

from __future__ import annotations

import hashlib
import io
import os
import re

from PIL import Image, ImageOps

from .media import MediaError
from .asset_objects import ASSET_OBJECT_ROLES

RENDITIONS = {"thumb": (320, 64 * 1024), "preview": (1280, 512 * 1024)}
POSTER_PREFIX_BYTES = 8 * 1024 * 1024
POSTER_FRAME_BYTES = 8 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}")


def create_image_renditions(source, directory, *, max_bytes):
    """Strip metadata, apply EXIF orientation and preserve alpha without upscaling.

    Both dimensions and encoded bytes are bounded. Lower quality is attempted
    only in memory, and a failed limit never publishes an oversized copy.
    Caller holds the existing processing admission and asset operation lock.
    """
    results = {}
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
        image = image.convert("RGBA" if alpha else "RGB")
        for role, (edge, byte_cap) in RENDITIONS.items():
            small = image.copy()
            small.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            limit = min(byte_cap, max_bytes)
            encoded = None
            for _ in range(6):
                for quality in (80, 50, 20):
                    output = io.BytesIO()
                    small.save(
                        output,
                        format="WEBP",
                        quality=quality,
                        method=4,
                        exif=b"",
                        icc_profile=b"",
                    )
                    if 0 < output.tell() <= limit:
                        encoded = output.getvalue()
                        break
                if encoded is not None:
                    break
                # Noisy lossless alpha alone can exceed the byte cap at320px.
                # Reduce display dimensions, retaining alpha/aspect and the
                # original, instead of dropping transparency or serving it raw.
                small.thumbnail(
                    (max(1, small.width * 3 // 4), max(1, small.height * 3 // 4)),
                    Image.Resampling.LANCZOS,
                )
            if encoded is None:
                raise MediaError("图片预览超过容量上限；原件已保留")
            path = directory / f"{role}-v1.webp"
            # Only replace the same unpublished deterministic processing copy.
            # AssetService validates any existing file before calling us.
            with path.open("wb") as target:
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())
            results[role] = dict(
                filename=path.name,
                size_bytes=len(encoded),
                sha256=hashlib.sha256(encoded).hexdigest(),
                content_type="image/webp",
                width=small.width,
                height=small.height,
                phase="planned",
            )
    return results


def create_video_renditions(source, directory, *, max_bytes):
    """Decode one frame only; a faststart prefix may be supplied by the writer.

    The existing media runner restricts demuxers, protocols, memory and time.
    A truncated/late-moov prefix is unsupported, never a reason to read an
    unbounded source or decode the complete video. The original is untouched.
    """
    from . import media

    info = media.inspect(
        source, "video"
    )  # Metadata only; validates dimensions/stream/rotation bounds.
    target = directory / "poster-frame.png"
    media.ffmpeg(
        [
            "-i",
            source,
            "-map",
            f'0:{info["video_stream_index"]}',
            "-an",
            "-sn",
            "-dn",
            "-frames:v",
            "1",
            "-vf",
            "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease",
            "-c:v",
            "png",
            "-threads",
            "1",
            "-fs",
            min(POSTER_FRAME_BYTES, max_bytes),
            target,
        ],
        timeout=30,
    )
    if not target.is_file() or not 0 < target.stat().st_size <= POSTER_FRAME_BYTES:
        raise MediaError("视频封面未完整提取；原视频已保留")
    media.inspect(target, "image")
    return create_image_renditions(target, directory, max_bytes=max_bytes)


def public_renditions(record):
    """Safe metadata only; malformed/missing copies never fall back to originals."""
    if record.get("kind") not in {"image", "video"}:
        return {}
    metadata = record.get("rendition_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    original = record.get("original")
    original = original if isinstance(original, dict) else {}
    source = metadata.get("source_sha256")
    valid_source = (
        record.get("status") == "ready"
        and type(metadata.get("version")) is int
        and metadata["version"] == 1
        and isinstance(source, str)
        and _HASH.fullmatch(source)
        and source == original.get("sha256")
    )
    result = {}
    for role, (edge, cap) in RENDITIONS.items():
        item, info = record.get(role), metadata.get(role)
        item = item if isinstance(item, dict) else {}
        info = info if isinstance(info, dict) else {}
        width, height, size, sha = (
            info.get("width"),
            info.get("height"),
            item.get("size_bytes"),
            item.get("sha256"),
        )
        if (
            not valid_source
            or not item.get("key")
            or item.get("content_type") != "image/webp"
            or type(width) is not int
            or type(height) is not int
            or not 1 <= width <= edge
            or not 1 <= height <= edge
            or type(size) is not int
            or not 1 <= size <= cap
            or not isinstance(sha, str)
            or not _HASH.fullmatch(sha)
        ):
            continue
        result[role] = dict(
            content_url=f'/v1/assets/{record["id"]}/content?rendition={role}',
            width=width,
            height=height,
            size_bytes=size,
            sha256=sha,
            content_type="image/webp",
            immutable=True,
        )
    ready = len(result) == len(RENDITIONS)
    status = (
        "ready"
        if ready
        else "pending"
        if record.get("rendition_status") == "pending"
        else "missing"
    )
    public = dict(renditions=result, rendition_ready=ready, rendition_status=status)
    for role, field in (("thumb", "thumbnail_url"), ("preview", "preview_url")):
        if role in result:
            public[field] = result[role]["content_url"]
    return public


def public_artifact_renditions(record):
    """Same display validation, rooted in an original validated video artifact."""
    value = record.get("metadata", {})
    if value.get("kind") != "video":
        return {}
    display = value.get("display_renditions", {})
    display = display if isinstance(display, dict) else {}
    objects = display.get("objects", {})
    objects = objects if isinstance(objects, dict) else {}
    view = dict(
        id=record["id"],
        kind="video",
        status="ready" if value.get("validated") is True else "pending",
        original=value,
        rendition_metadata=display.get("metadata"),
        **{role: objects.get(role) for role in RENDITIONS},
    )
    result = public_renditions(view)
    for role, item in result.get("renditions", {}).items():
        item["content_url"] = f'/v1/artifacts/{record["id"]}/content?rendition={role}'
    for role, field in (("thumb", "thumbnail_url"), ("preview", "preview_url")):
        if role in result.get("renditions", {}):
            result[field] = result["renditions"][role]["content_url"]
    return result
