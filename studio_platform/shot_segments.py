"""Pure, source-bound shot edit ranges; no media, database or provider access."""

import math
import re

FPS = 24
MAX_SEGMENTS = 50
SOURCE_FIELDS = ("fileId", "cloudAssetId", "cloudArtifactId")
_ID = re.compile(r"[A-Za-z0-9_-]{1,160}$")


def has_segments(shot):
    data = shot.get("data") if isinstance(shot, dict) else None
    return isinstance(data, dict) and bool(data.get("videoSegments"))


def source_binding(entity):
    return {
        "assetId": entity["id"],
        **{key: entity.get("data", {}).get(key) or None for key in SOURCE_FIELDS},
    }


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_segments_data(entity):
    """Validate editable structure; authoritative duration is checked at admission."""
    data = entity.get("data", {})
    if "videoSegments" not in data and "videoSegmentsSource" not in data:
        return
    segments, binding = data.get("videoSegments", []), data.get("videoSegmentsSource")
    if entity.get("type") != "shot" or not isinstance(segments, list):
        raise ValueError("video_segments_invalid")
    if len(segments) > MAX_SEGMENTS:
        raise ValueError("video_segments_count_limit")
    if not segments:
        if binding is not None:
            raise ValueError("video_segments_source_without_ranges")
        return
    if (
        not isinstance(binding, dict)
        or set(binding) != {"assetId", *SOURCE_FIELDS}
        or not isinstance(binding.get("assetId"), str)
        or not _ID.fullmatch(binding["assetId"])
        or any(
            binding.get(key) is not None
            and (not isinstance(binding[key], str) or not 1 <= len(binding[key]) <= 200)
            for key in SOURCE_FIELDS
        )
    ):
        raise ValueError("video_segments_source_invalid")
    for segment in segments:
        if (
            not isinstance(segment, dict)
            or set(segment) != {"start", "end"}
            or not all(_number(segment.get(key)) for key in ("start", "end"))
            or not 0 <= segment["start"] < segment["end"] <= 360000
        ):
            raise ValueError("video_segment_range_invalid")
        first, last = _frames(segment)
        if last <= first:
            raise ValueError("video_segment_less_than_one_frame")


def _frames(segment):
    return (
        math.ceil(segment["start"] * FPS - 1e-7),
        math.floor(segment["end"] * FPS + 1e-7),
    )


def segment_frames(shot, entity=None, source_duration=None):
    """Ordered inward-grid ranges; optionally prove current identity and duration."""
    validate_segments_data(shot)
    if not has_segments(shot):
        return []
    data = shot["data"]
    binding = data["videoSegmentsSource"]
    if binding["assetId"] != data.get("selectedAssetId"):
        raise ValueError("video_segments_source_changed")
    if entity is not None and (
        entity.get("type") != "video" or source_binding(entity) != binding
    ):
        raise ValueError("video_segments_source_changed")
    selected = data.get("selectedVideoRange")
    if isinstance(selected, dict) and "playback_rate" in selected:
        if {key: selected.get(key) for key in binding} != binding:
            raise ValueError("video_segments_speed_source_changed")
    if source_duration is not None and (
        not _number(source_duration) or not 0 < source_duration <= 360000
    ):
        raise ValueError("video_segments_source_duration_unknown")
    result = []
    for segment in data["videoSegments"]:
        if source_duration is not None and segment["end"] > source_duration:
            raise ValueError("video_segment_exceeds_source")
        first, last = _frames(segment)
        result.append(
            {
                "start_frame": first,
                "end_frame": last,
                "frames": last - first,
                "start": first / FPS,
                "end": last / FPS,
            }
        )
    return result


def validate_segments_transition(before, after):
    """A take change cannot quietly discard or rebind an existing segment edit.

    Call per action, and on a full-document save. Explicit [] is the clear
    intent; an omitted field in an older client save is not that confirmation.
    Deleting the shot itself remains an ordinary explicit project edit.
    """
    previous = {item["id"]: item for item in before.get("entities", [])}
    current = {item["id"]: item for item in after.get("entities", [])}
    for ident, old in previous.items():
        if old.get("type") != "shot" or not has_segments(old) or ident not in current:
            continue
        new = current[ident]
        if new.get("data", {}).get("videoSegments") == []:
            continue
        if not has_segments(new):
            raise ValueError("video_segments_clear_required")
        old_source = previous.get(old["data"].get("selectedAssetId"))
        new_source = current.get(new.get("data", {}).get("selectedAssetId"))
        if (
            old["data"].get("videoSegmentsSource")
            != new.get("data", {}).get("videoSegmentsSource")
            or old_source is None
            or new_source is None
            or source_binding(old_source) != source_binding(new_source)
        ):
            raise ValueError("video_segments_clear_required")
