"""Pure validation of accepted CPU render requests; no media or provider work."""

from __future__ import annotations

from fractions import Fraction
import math
import re

from .caption_server import validate_normalized_subtitles
from .inference.protocol import BackendError
from .render_limits import MAX_AUDIO_TRACKS
from .storage import key_belongs_to, validate_key

MIB = 1024 * 1024
SOURCE_ID = re.compile(r"[A-Za-z0-9_:\-]{1,160}$")
RECIPE = "chapter-roughcut-v1"
MIMES = {
    "video/mp4": ("video", ".mp4"),
    "video/quicktime": ("video", ".mov"),
    "audio/wav": ("audio", ".wav"),
    "audio/flac": ("audio", ".flac"),
    "audio/mpeg": ("audio", ".mp3"),
}


def _require(condition, code):
    if not condition:
        raise BackendError(code)


def _number(value, minimum, maximum):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and minimum <= value <= maximum
    )


def _film_concat(render):
    return render["version"] == 4 or (
        render["version"] == 5 and render.get("stage") == "film_concat"
    )


def _audio_tracks(render):
    """Keep accepted legacy payloads intact; v5 freezes integer sample bounds."""
    if render["version"] != 5:
        return render["audio_tracks"]
    return [
        {
            **track,
            "timeline_start": track["timeline_start_sample"] / 32000,
            "source_start": track["source_start_sample"] / 32000,
            "source_end": track["source_end_sample"] / 32000,
        }
        for track in render["audio_tracks"]
    ]


def validate_subtitles(value, frames):
    """The renderer does not trust a previously compiled subtitle payload."""
    try:
        validate_normalized_subtitles(value, frames)
    except ValueError:
        raise BackendError("invalid_render_subtitles") from None


def validate_render_request(
    payload,
    *,
    owner_id=None,
    max_source_bytes=512 * MIB,
    max_total_input_bytes=2048 * MIB,
):
    """Pure validation; source ownership/project authorization also belongs to API."""
    _require(
        isinstance(payload, dict) and payload.get("recipe_id") == RECIPE,
        "invalid_render_recipe",
    )
    request, shape, sources = (
        payload.get("request"),
        payload.get("output_spec"),
        payload.get("sources"),
    )
    _require(
        isinstance(request, dict)
        and isinstance(shape, dict)
        and isinstance(sources, dict),
        "invalid_render_request",
    )
    render = request.get("render")
    _require(
        isinstance(render, dict)
        and type(render.get("version")) is int
        and render["version"] in (1, 2, 3, 4, 5),
        "invalid_render_version",
    )
    _require(
        set(render)
        == {"version", "shots", "audio_tracks"}
        | ({"subtitles"} if render["version"] >= 3 else set())
        | ({"stage"} if render["version"] == 5 else set()),
        "invalid_render_version",
    )
    shots, tracks = render["shots"], render["audio_tracks"]
    _require(
        render["version"] != 5
        or (
            isinstance(render["stage"], str)
            and render["stage"] in {"chapter", "film_concat"}
        ),
        "invalid_render_stage",
    )
    _require(
        not _film_concat(render) or render.get("subtitles") is None,
        "film_export_captions_must_be_in_chapters",
    )
    _require(
        isinstance(shots, list)
        and 1 <= len(shots) <= 50
        and isinstance(tracks, list)
        and len(tracks) <= MAX_AUDIO_TRACKS,
        "render_clip_count_limit",
    )
    width, height = shape.get("width"), shape.get("height")
    _require(
        type(width) is int
        and type(height) is int
        and 256 <= width <= 1280
        and 256 <= height <= 1280
        and not width % 2
        and not height % 2
        and width * height <= 1280 * 720,
        "render_dimensions_limit",
    )
    used, frames = {}, 0
    for shot in shots:
        fields = (
            {"shot_id", "source_id", "frames"}
            | ({"source_start_frame"} if render["version"] >= 2 else set())
            | ({"source_frames", "playback_rate"} if render["version"] == 5 else set())
        )
        _require(isinstance(shot, dict) and set(shot) == fields, "invalid_render_shot")
        _require(
            isinstance(shot["shot_id"], str) and SOURCE_ID.fullmatch(shot["shot_id"]),
            "invalid_render_shot_id",
        )
        _require(
            isinstance(shot["source_id"], str)
            and SOURCE_ID.fullmatch(shot["source_id"]),
            "invalid_render_source_id",
        )
        _require(
            type(shot["frames"]) is int and 1 <= shot["frames"] <= 14400,
            "invalid_render_shot_frames",
        )
        first = shot.get("source_start_frame", 0)
        source_frames = shot.get("source_frames", shot["frames"])
        _require(
            type(first) is int
            and 0 <= first < 86400
            and type(source_frames) is int
            and 1 <= source_frames <= 86400
            and first + source_frames <= 86400,
            "invalid_render_source_start_frame",
        )
        _require(
            not _film_concat(render) or first == 0,
            "film_export_chapter_range_invalid",
        )
        if render["version"] == 5:
            rate = shot["playback_rate"]
            _require(
                type(rate) in (int, float) and rate in (0.5, 0.75, 1, 1.25, 1.5, 2),
                "invalid_render_playback_rate",
            )
            ratio = Fraction(str(rate))
            _require(
                shot["frames"]
                == (2 * source_frames * ratio.denominator + ratio.numerator)
                // (2 * ratio.numerator)
                and (not _film_concat(render) or rate == 1),
                "render_playback_frames_mismatch",
            )
        frames += shot["frames"]
        used[shot["source_id"]] = "video"
    _require(
        frames <= 14400
        and type(shape.get("fps")) is int
        and shape["fps"] == 24
        and type(shape.get("frame_count")) is int
        and shape["frame_count"] == frames,
        "render_duration_limit",
    )
    duration = frames / 24
    validate_subtitles(render.get("subtitles"), frames)
    _require(
        _number(request.get("duration"), 1 / 24, 600)
        and abs(request["duration"] - duration) < 1e-6,
        "render_duration_mismatch",
    )
    _require(
        type(request.get("generate_audio")) is bool
        and request["generate_audio"] == bool(tracks)
        and request.get("export_crf") == 18,
        "render_output_controls_mismatch",
    )
    for track in tracks:
        if render["version"] == 5:
            _require(
                isinstance(track, dict)
                and set(track)
                == {
                    "source_id",
                    "timeline_start_sample",
                    "source_start_sample",
                    "source_end_sample",
                    "output_samples",
                    "playback_rate",
                    "gain",
                    "fade_in_samples",
                    "fade_out_samples",
                },
                "invalid_render_audio_track",
            )
            _require(
                all(
                    type(track[name]) is int and 0 <= track[name] <= 3600 * 32000
                    for name in (
                        "timeline_start_sample",
                        "source_start_sample",
                        "source_end_sample",
                        "output_samples",
                        "fade_in_samples",
                        "fade_out_samples",
                    )
                )
                and type(track["playback_rate"]) in (int, float)
                and track["playback_rate"] in (0.5, 0.75, 1, 1.25, 1.5, 2)
                and track["source_end_sample"] > track["source_start_sample"]
                and track["output_samples"] > 0
                and track["timeline_start_sample"] + track["output_samples"]
                <= (frames * 32000 + 12) // 24
                and max(track["fade_in_samples"], track["fade_out_samples"])
                <= 5 * 32000
                and track["fade_in_samples"] + track["fade_out_samples"]
                <= track["output_samples"]
                and abs(
                    track["output_samples"]
                    - (track["source_end_sample"] - track["source_start_sample"])
                    / track["playback_rate"]
                )
                <= math.ceil(32000 / 24)
                and (
                    not _film_concat(render)
                    or track["playback_rate"] == 1
                    and track["fade_in_samples"] == track["fade_out_samples"] == 0
                ),
                "render_audio_range_limit",
            )
            continue
        _require(
            isinstance(track, dict)
            and set(track)
            == {"source_id", "timeline_start", "source_start", "source_end", "gain"},
            "invalid_render_audio_track",
        )
    for track in _audio_tracks(render):
        key = track["source_id"]
        _require(
            isinstance(key, str)
            and SOURCE_ID.fullmatch(key)
            and used.get(key, "audio") == "audio",
            "invalid_render_audio_source",
        )
        _require(
            _number(track["source_start"], 0, 3600)
            and _number(track["source_end"], 0, 3600)
            and track["source_end"] > track["source_start"]
            and _number(track["timeline_start"], 0, duration)
            and track["timeline_start"]
            + (
                track["output_samples"] / 32000
                if render["version"] == 5
                else track["source_end"] - track["source_start"]
            )
            <= duration + 1 / 32000
            and _number(track["gain"], 0, 1),
            "render_audio_range_limit",
        )
        used[key] = "audio"
    _require(set(sources) == set(used), "render_sources_must_match_timeline")
    total = 0
    for key, kind in used.items():
        source = sources[key]
        _require(
            isinstance(source, dict)
            and source.get("kind") == kind
            and type(source.get("simulation")) is bool,
            "invalid_render_source_snapshot",
        )
        obj = source.get("object")
        _require(
            isinstance(obj, dict)
            and type(obj.get("size_bytes")) is int
            and 0 < obj["size_bytes"] <= max_source_bytes,
            "render_source_size_limit",
        )
        _require(
            isinstance(obj.get("sha256"), str)
            and re.fullmatch("[0-9a-f]{64}", obj["sha256"])
            and isinstance(obj.get("content_type"), str)
            and obj["content_type"] in MIMES
            and MIMES[obj["content_type"]][0] == kind,
            "invalid_render_source_metadata",
        )
        try:
            validate_key(obj.get("key"))
            _require(
                owner_id is None or key_belongs_to(obj["key"], owner_id),
                "render_source_owner_mismatch",
            )
        except ValueError:
            raise BackendError("invalid_render_source_key") from None
        total += obj["size_bytes"]
    _require(total <= max_total_input_bytes, "render_total_input_size_limit")
    return {
        "width": width,
        "height": height,
        "duration": duration,
        "frames": frames,
        "audio": bool(tracks),
        "simulation": any(s["simulation"] for s in sources.values()),
    }
