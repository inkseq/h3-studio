"""Opt-in CPU chapter rough cuts from authorized immutable object snapshots.

No provider/URL access or process starts at import/construction. A submission is
one synchronous, journalled CPU attempt, never blindly replayed after a crash.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

from . import media
from .caption_server import validate_normalized_subtitles
from .storage import (
    _check_ancestors,
    _copy,
    _no_links,
    _sync_directory,
    key_belongs_to,
    validate_key,
)
from .worker import (
    BackendError,
    NotReady,
    Outcome,
    RenderCacheCapacityExceeded,
    SubmissionRejected,
    SubmissionUncertain,
    TAG,
    _slot_lock,
)

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
        and len(tracks) <= 32,
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


def _hash(payload):
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(MIB), b""):
            value.update(chunk)
    return value.hexdigest()


class _Cancelled(Exception):
    pass


class CPURenderBackend:
    """Local rough-cut processing; cost 0 means no separate user charge here."""

    kind = "cpu-render"

    def __init__(
        self,
        state_dir,
        *,
        enabled=False,
        max_source_bytes=512 * MIB,
        max_total_input_bytes=2048 * MIB,
        max_output_bytes=512 * MIB,
        max_attempt_bytes=4096 * MIB,
        max_state_bytes=8192 * MIB,
        timeout_seconds=1800,
        subtitle_font_profile="noto-cjk",
    ):
        self.enabled = bool(enabled)
        self.state_dir = Path(state_dir)
        _require(
            self.state_dir.is_absolute(), "render_state_directory_must_be_absolute"
        )
        for limit in (
            max_source_bytes,
            max_total_input_bytes,
            max_output_bytes,
            max_attempt_bytes,
            max_state_bytes,
        ):
            _require(type(limit) is int and limit > 0, "invalid_render_storage_limits")
        _require(_number(timeout_seconds, 1, 1800), "invalid_render_timeout")
        _require(
            isinstance(subtitle_font_profile, str)
            and subtitle_font_profile in {"noto-cjk", "windows-yahei"},
            "invalid_subtitle_font_profile",
        )
        self.subtitle_font_profile = subtitle_font_profile
        self.max_source_bytes, self.max_total_input_bytes = (
            max_source_bytes,
            max_total_input_bytes,
        )
        self.max_output_bytes, self.max_attempt_bytes = (
            max_output_bytes,
            max_attempt_bytes,
        )
        self.max_state_bytes = max_state_bytes
        self.timeout_seconds = timeout_seconds
        self.slot_key = (
            "cpu-render-" + hashlib.sha256(str(self.state_dir).encode()).hexdigest()
        )
        self.cost_resolver = self.actual_cost_resolver
        self._active, self._preparing, self._mutex = {}, {}, threading.Lock()

    @staticmethod
    def actual_cost_resolver(job, task_id):
        # CPU hosting/operations still have costs. This is the site's current
        # explicit zero additional charge, not an invoice for free hardware.
        return 0

    @staticmethod
    def subtitle_environment(profile="noto-cjk"):
        """Fixed operator-owned fonts only; never accept a font path from a job."""
        from PIL import ImageFont

        if profile not in {"noto-cjk", "windows-yahei"} or (
            profile == "windows-yahei" and os.name != "nt"
        ):
            raise NotReady("subtitle_font_profile_unavailable")
        windows = profile == "windows-yahei"
        family = "Microsoft YaHei" if windows else "Noto Sans CJK SC"
        path = Path(
            "C:/Windows/Fonts/msyh.ttc"
            if windows
            else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
        )
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            if not path.is_file():
                raise ValueError("font_missing")
            face = next(
                (
                    index
                    for index in range(10)
                    if ImageFont.truetype(str(path), 20, index=index).getname()[0]
                    == family
                ),
                None,
            )
            if face is None:
                raise ValueError("font_family_missing")
            if not windows:
                match = (
                    subprocess.run(
                        ["fc-match", "--format", "%{family}\n%{file}\n", family],
                        capture_output=True,
                        check=True,
                        timeout=10,
                        creationflags=flags,
                    )
                    .stdout.decode("utf-8")
                    .splitlines()
                )
                if match != [family, str(path)]:
                    raise ValueError("font_fallback_not_allowed")
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-h", "filter=ass"],
                capture_output=True,
                check=True,
                timeout=10,
                creationflags=flags,
            )
            if b"Render ASS subtitles" not in result.stdout + result.stderr:
                raise ValueError("libass_unavailable")
            return {"family": family, "path": str(path), "face": face}
        except (OSError, ValueError, subprocess.SubprocessError):
            raise NotReady("subtitle_font_or_libass_unavailable") from None

    def assert_subtitle_ready(self):
        self.subtitle_environment(self.subtitle_font_profile)

    def _subtitle_style(self, shape, subtitles):
        if subtitles is None:
            return None
        from PIL import ImageFont

        environment = self.subtitle_environment(self.subtitle_font_profile)
        font_size = max(10, math.floor(min(shape["width"], shape["height"]) * 0.045))
        margin_x, margin_y = (
            math.ceil(shape["width"] * 0.08),
            math.ceil(shape["height"] * 0.12),
        )
        outline = max(1, round(font_size * 0.08))
        font = ImageFont.truetype(
            environment["path"], font_size, index=environment["face"]
        )
        missing = bytes(font.getmask("\U0010ffff"))
        for cue in subtitles["cues"]:
            for line in cue["text"].split("\n"):
                _require(
                    font.getlength(line) + 2 * outline <= shape["width"] - 2 * margin_x,
                    "render_subtitle_exceeds_safe_area",
                )
                for char in line:
                    if not char.isspace():
                        mask = bytes(font.getmask(char))
                        _require(
                            mask and mask != missing,
                            "render_subtitle_glyph_unavailable",
                        )
            ascent, descent = font.getmetrics()
            _require(
                len(cue["text"].split("\n")) * (ascent + descent) + 2 * outline
                <= shape["height"] * 0.72,
                "render_subtitle_exceeds_safe_area",
            )
        return {
            **environment,
            "font_size": font_size,
            "margin_x": margin_x,
            "margin_y": margin_y,
            "outline": outline,
        }

    def _write_subtitles(self, directory, shape, subtitles, style):
        # User text never appears inside an ASS override, filename or filter.
        # Unsupported syntax was rejected, not silently changed or stripped.
        def timestamp(frame):
            centiseconds = frame * 100 // 24
            return f"{centiseconds // 360000}:{centiseconds // 6000 % 60:02d}:{centiseconds // 100 % 60:02d}.{centiseconds % 100:02d}"

        value = (
            "[Script Info]\nScriptType: v4.00+\nScaledBorderAndShadow: yes\nYCbCr Matrix: None\n"
            f"PlayResX: {shape['width']}\nPlayResY: {shape['height']}\nLayoutResX: {shape['width']}\nLayoutResY: {shape['height']}\nWrapStyle: 2\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,{style['family']},{style['font_size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{style['outline']},0,2,{style['margin_x']},{style['margin_x']},{style['margin_y']},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )
        for cue in subtitles["cues"]:
            text = cue["text"].replace("\n", r"\N")
            value += f"Dialogue: 0,{timestamp(cue['start_frame'])},{timestamp(cue['end_frame'])},Default,,0,0,0,,{text}\n"
        encoded = value.encode("utf-8-sig")
        _require(len(encoded) <= 256 * 1024, "render_subtitle_file_limit")
        path = self._path(directory, "captions.ass")
        with path.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return path

    def _directory(self, tag, *, create=False):
        _require(
            isinstance(tag, str) and TAG.fullmatch(tag), "invalid_render_attempt_tag"
        )
        directory = self.state_dir / tag
        _check_ancestors(directory)
        if create:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory

    def _path(self, directory, filename):
        _require(
            isinstance(filename, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", filename)
            and filename not in {".", ".."},
            "invalid_render_file",
        )
        path = directory / filename
        if path.exists() or path.is_symlink():
            stat = _no_links(path)
            _require(path.is_file() and stat.st_nlink == 1, "invalid_render_file")
        return path

    def _save(self, directory, state):
        target = self._path(directory, "state.json")
        temp = self._path(directory, "state.new")
        encoded = json.dumps(state, sort_keys=True, allow_nan=False).encode()
        _require(len(encoded) <= MIB, "render_state_limit")
        with temp.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(target)
        _sync_directory(directory)

    def _load(self, directory):
        path = self._path(directory, "state.json")
        _require(
            path.exists() and path.stat().st_size <= MIB, "render_state_unavailable"
        )
        try:
            state = json.loads(path.read_bytes())
        except (ValueError, OSError):
            raise BackendError("render_state_invalid") from None
        _require(
            isinstance(state, dict) and state.get("version") == 1,
            "render_state_invalid",
        )
        return state

    def prepare(self, job, tag, store, heartbeat):
        if not self.enabled:
            raise NotReady("backend_disabled")
        self._directory(tag)  # Validate before using an untrusted map key.
        with self._mutex:
            _require(tag not in self._preparing, "render_attempt_busy")
            cancelled = self._preparing[tag] = threading.Event()
        deadline = time.monotonic() + self.timeout_seconds

        def progress():
            _require(time.monotonic() <= deadline, "render_prepare_timeout")
            heartbeat()
            _require(not cancelled.is_set(), "render_preparation_cancelled")

        try:
            prepared = self._prepare(job, tag, store, progress, deadline)
            prepared["heartbeat"] = heartbeat  # Encoding starts its own bounded phase.
            return prepared
        except Exception:
            # Only a proven failed PREPARATION releases the conservative future
            # work reservation. Crashes/BaseException keep the reservation.
            try:
                directory = self._directory(tag)
                if (directory / "state.json").exists():
                    with _slot_lock(directory, "attempt") as acquired:
                        if acquired:
                            state = self._load(directory)
                            identity = _hash(
                                {
                                    "owner": job["owner_id"],
                                    "job": job["id"],
                                    "payload": job["request"],
                                }
                            )
                            if (
                                state["phase"] == "preparing"
                                and state["identity"] == identity
                            ):
                                state["phase"] = "preparation_failed"
                                self._save(directory, state)
            except Exception:
                pass  # Preserve the conservative reservation if state is unclear.
            raise
        finally:
            with self._mutex:
                self._preparing.pop(tag, None)

    def _prepare(self, job, tag, store, heartbeat, deadline):
        if not self.enabled:
            raise NotReady("backend_disabled")
        payload = job["request"]
        shape = validate_render_request(
            payload,
            owner_id=job["owner_id"],
            max_source_bytes=self.max_source_bytes,
            max_total_input_bytes=self.max_total_input_bytes,
        )
        self._subtitle_style(shape, payload["request"]["render"].get("subtitles"))
        identity = _hash(
            {"owner": job["owner_id"], "job": job["id"], "payload": payload}
        )
        directory = self._directory(tag, create=True)
        with _slot_lock(directory, "attempt") as acquired:
            _require(acquired, "render_attempt_busy")
            if (directory / "state.json").exists():
                state = self._load(directory)
                _require(
                    state.get("identity") == identity,
                    "render_attempt_identity_mismatch",
                )
                _require(
                    state.get("phase")
                    in {"preparing", "prepared", "preparation_failed"},
                    "render_submission_already_recorded",
                )
            else:
                state = {
                    "version": 1,
                    "backend": self.kind,
                    "phase": "preparing",
                    "tag": tag,
                    "task_id": "cpu-render-" + tag,
                    "identity": identity,
                    "payload": payload,
                    "shape": shape,
                    "inputs": {},
                }
            # Cross-process reservation is stored in the attempt itself under a
            # root quota lock. Never count an active attempt as only its bytes-so-far.
            with _slot_lock(self.state_dir, "state-capacity") as capacity_acquired:
                if not capacity_acquired:
                    raise NotReady("render_state_capacity_busy")
                current = self._state_usage()
                already = (directory / "state.json").exists()
                old = self._load(directory) if already else None
                old_charge = self._attempt_charge(directory, old) if old else 0
                if current - old_charge + self.max_attempt_bytes > self.max_state_bytes:
                    raise RenderCacheCapacityExceeded("render_state_capacity_exhausted")
                state.update(phase="preparing", reserved_bytes=self.max_attempt_bytes)
                self._save(directory, state)
            for index, (source_id, snapshot) in enumerate(payload["sources"].items()):
                heartbeat()
                obj = snapshot["object"]
                extension = MIMES[obj["content_type"]][1]
                name = f"input-{index:03d}" + extension
                path = self._path(directory, name)
                if not path.exists():
                    partial = self._path(directory, "receiving.part")
                    with store.open(obj["key"]) as source, partial.open("wb") as target:

                        class ProgressReader:
                            # A single blocked cloud read is bounded separately
                            # by the store's configured transport read timeout.
                            last = time.monotonic()

                            def read(inner, size=-1):
                                _require(
                                    time.monotonic() <= deadline,
                                    "render_prepare_timeout",
                                )
                                value = source.read(size)
                                now = time.monotonic()
                                _require(now <= deadline, "render_prepare_timeout")
                                if now - inner.last >= 5 or not value:
                                    heartbeat()
                                    inner.last = now
                                return value

                        count, _ = _copy(
                            ProgressReader(),
                            target,
                            self.max_source_bytes,
                            obj["sha256"],
                        )
                        target.flush()
                        os.fsync(target.fileno())
                    _require(count == obj["size_bytes"], "render_source_size_mismatch")
                    partial.replace(path)
                    _sync_directory(directory)
                _require(
                    path.stat().st_size == obj["size_bytes"]
                    and _digest(path) == obj["sha256"],
                    "render_source_changed",
                )
                metadata = media.probe(path)
                streams = [
                    s
                    for s in metadata.get("streams", [])
                    if s.get("codec_type") == snapshot["kind"]
                    and not s.get("disposition", {}).get("attached_pic")
                ]
                _require(bool(streams), "render_source_stream_missing")
                try:
                    duration = float(
                        streams[0].get("duration", metadata["format"]["duration"])
                    )
                except (ValueError, KeyError, TypeError):
                    raise BackendError("render_source_duration_unknown") from None
                _require(
                    _number(duration, 1 / 24, 3600), "render_source_duration_limit"
                )
                if snapshot["kind"] == "video":
                    _require(
                        all(
                            type(streams[0].get(axis)) is int
                            and 16 <= streams[0][axis] <= 5760
                            for axis in ("width", "height")
                        ),
                        "render_source_dimensions_limit",
                    )
                    if _film_concat(payload["request"]["render"]):
                        _require(
                            streams[0].get("codec_name") == "h264"
                            and streams[0].get("width") == shape["width"]
                            and streams[0].get("height") == shape["height"]
                            and Fraction(streams[0].get("avg_frame_rate", "0")) == 24,
                            "film_export_chapter_codec_mismatch",
                        )
                else:
                    _require(
                        not any(
                            s.get("codec_type") == "video"
                            for s in metadata.get("streams", [])
                        ),
                        "render_audio_source_must_be_audio",
                    )
                state["inputs"][source_id] = {"filename": name, "duration": duration}
                self._save(directory, state)
                self._check_work_size(directory)
            for shot in payload["request"]["render"]["shots"]:
                _require(
                    state["inputs"][shot["source_id"]]["duration"] + 0.001
                    >= (
                        shot.get("source_start_frame", 0)
                        + shot.get("source_frames", shot["frames"])
                    )
                    / 24,
                    "render_source_video_too_short",
                )
                if _film_concat(payload["request"]["render"]):
                    metadata = media.probe(
                        self._path(
                            directory, state["inputs"][shot["source_id"]]["filename"]
                        )
                    )
                    stream = next(
                        item
                        for item in metadata["streams"]
                        if item.get("codec_type") == "video"
                    )
                    _require(
                        int(stream.get("nb_frames", 0)) == shot["frames"],
                        "film_export_chapter_frames_mismatch",
                    )
            for track in _audio_tracks(payload["request"]["render"]):
                actual_duration = state["inputs"][track["source_id"]]["duration"]
                enough = (
                    round(actual_duration * 32000) >= round(track["source_end"] * 32000)
                    if payload["request"]["render"]["version"] >= 2
                    else actual_duration + 0.001 >= track["source_end"]
                )
                _require(enough, "render_source_audio_too_short")
            heartbeat()
            state["phase"] = "prepared"
            self._save(directory, state)
        # Heartbeats are process-local callbacks, never serialized into receipts.
        return {"tag": tag, "identity": identity, "heartbeat": heartbeat}

    def _check_work_size(self, directory):
        total = 0
        for item in directory.iterdir():
            checked = self._path(directory, item.name)
            total += checked.stat().st_size
        _require(total <= self.max_attempt_bytes, "render_work_directory_limit")
        if self._state_usage() > self.max_state_bytes:
            raise RenderCacheCapacityExceeded("render_state_capacity_exhausted")

    def _attempt_charge(self, directory, state):
        actual = 0
        for path in directory.iterdir():
            # One known attempt level only; never recurse or follow a link.
            actual += self._path(directory, path.name).stat().st_size
        if state["phase"] in {"preparing", "prepared", "submitting"}:
            return max(actual, state.get("reserved_bytes", self.max_attempt_bytes))
        return actual

    def _state_usage(self):
        if not self.state_dir.exists():
            return 0
        _check_ancestors(self.state_dir)
        total = 0
        for directory in self.state_dir.iterdir():
            item = _no_links(directory)
            if directory.is_file():
                _require(
                    bool(re.fullmatch(r"[0-9a-f]{64}\.slot", directory.name))
                    and item.st_nlink == 1,
                    "unexpected_render_root_file",
                )
                continue  # Tiny root quota locks do not hold user/media content.
            _require(
                directory.is_dir() and TAG.fullmatch(directory.name),
                "unexpected_render_root_entry",
            )
            if not (directory / "state.json").exists():
                # A just-created attempt may only contain its OS lock. Anything
                # else is unowned data requiring review, not a directory to scan.
                _require(
                    all(
                        re.fullmatch(r"[0-9a-f]{64}\.slot", p.name)
                        and self._path(directory, p.name).is_file()
                        for p in directory.iterdir()
                    ),
                    "unrecognized_render_attempt",
                )
                continue
            state = self._load(directory)
            _require(
                state.get("tag") == directory.name
                and state.get("payload", {}).get("recipe_id") == RECIPE,
                "unrecognized_render_attempt",
            )
            total += self._attempt_charge(directory, state)
        return total

    def _run_process(self, args, directory, tag, heartbeat, deadline, output=None):
        state = self._load(directory) if output is not None else {}
        checkpoint_key = output.name if output is not None else None
        argument_hash = _hash([str(item) for item in args])
        checkpoint = state.get("checkpoints", {}).get(checkpoint_key)
        if checkpoint is not None:
            _require(
                output is not None
                and checkpoint["arguments"] == argument_hash
                and output.is_file()
                and output.stat().st_size == checkpoint["size_bytes"]
                and _digest(output) == checkpoint["sha256"],
                "render_checkpoint_changed",
            )
            heartbeat()
            return
        with self._mutex:
            if self._active[tag]["cancelled"].is_set():
                raise _Cancelled()
        heartbeat()
        with self._mutex:
            if self._active[tag]["cancelled"].is_set():
                raise _Cancelled()
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-threads",
                "2",
                *map(str, args),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            cwd=directory,
            **(
                {
                    "pass_fds": tuple(
                        fd
                        for fd in (
                            self._active[tag]["process_fence"],
                            getattr(self, "_operation_fence_fd", None),
                        )
                        if fd is not None
                    )
                }
                if os.name == "posix"
                and self._active[tag].get("process_fence") is not None
                else {}
            ),
        )
        with self._mutex:
            entry = self._active[tag]
            entry["process"] = process
        last_heartbeat = 0
        try:
            while process.poll() is None:
                if entry["cancelled"].is_set():
                    raise _Cancelled()
                _require(time.monotonic() < deadline, "render_processing_timeout")
                if output is not None and output.exists():
                    _require(
                        output.stat().st_size <= self.max_output_bytes + MIB,
                        "render_output_limit",
                    )
                self._check_work_size(directory)
                now = time.monotonic()
                if now - last_heartbeat >= 5:
                    heartbeat()
                    last_heartbeat = now
                time.sleep(0.05)
            if entry["cancelled"].is_set():
                raise _Cancelled()
            _require(process.returncode == 0, "render_processing_failed")
            if output is not None:
                _require(
                    output.is_file()
                    and 0 < output.stat().st_size <= self.max_output_bytes,
                    "render_output_limit",
                )
            self._check_work_size(directory)
            if output is not None:
                with output.open("r+b") as stream:
                    os.fsync(stream.fileno())
                state.setdefault("checkpoints", {})[checkpoint_key] = {
                    "arguments": argument_hash,
                    "size_bytes": output.stat().st_size,
                    "sha256": _digest(output),
                }
                self._save(directory, state)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            with self._mutex:
                entry["process"] = None

    def _render(self, state, directory, tag, heartbeat):
        shape, render = state["shape"], state["payload"]["request"]["render"]
        deadline = time.monotonic() + self.timeout_seconds
        width, height, duration = shape["width"], shape["height"], shape["duration"]
        subtitles = render.get("subtitles")
        subtitle_style = self._subtitle_style(shape, subtitles)
        for source_id, source in state["payload"]["sources"].items():
            path = self._path(directory, state["inputs"][source_id]["filename"])
            _require(
                path.stat().st_size == source["object"]["size_bytes"]
                and _digest(path) == source["object"]["sha256"],
                "render_source_changed",
            )
        if shape["simulation"] and not _film_concat(render):
            from PIL import Image, ImageDraw, ImageFont

            marker = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(marker)
            font = ImageFont.load_default(size=max(18, width // 18))
            draw.rectangle((0, 0, width, max(42, height // 8)), fill=(0, 0, 0, 210))
            draw.text((12, 8), "SIMULATION", font=font, fill=(255, 235, 0, 255))
            marker.save(self._path(directory, "simulation.png"))
        clips, total_clip_bytes = [], 0
        for index, shot in enumerate(render["shots"]):
            source = self._path(
                directory, state["inputs"][shot["source_id"]]["filename"]
            )
            if _film_concat(render):
                # The completed chapter is already normalized and captioned.
                # Copy its bitstream at concat; never rebuild a finished chapter.
                clips.append(source)
                total_clip_bytes += source.stat().st_size
                _require(
                    total_clip_bytes <= self.max_output_bytes,
                    "render_combined_video_size_limit",
                )
                continue
            output = self._path(directory, f"clip-{index:03d}.mp4")
            first = shot.get("source_start_frame", 0)
            source_frames = shot.get("source_frames", shot["frames"])
            if render["version"] == 5:
                # Prove the unretimed decoded trim before any rounding pad.
                # A timestamp/header claim alone cannot hide missing frames.
                proof = self._path(directory, f"source-{index:03d}.framemd5")
                self._run_process(
                    [
                        *media.input_options(source),
                        "-i",
                        source,
                        "-map",
                        "0:v:0",
                        "-an",
                        "-vf",
                        f"setpts=PTS-STARTPTS,fps=24,"
                        f"trim=start_frame={first}:end_frame={first + source_frames},"
                        "setpts=PTS-STARTPTS",
                        "-f",
                        "framemd5",
                        proof,
                    ],
                    directory,
                    tag,
                    heartbeat,
                    deadline,
                    proof,
                )
                _require(
                    proof.stat().st_size <= 4096 + source_frames * 256,
                    "render_source_frame_proof_limit",
                )
                count = 0
                with proof.open("r", encoding="ascii") as stream:
                    for line in stream:
                        if line.startswith("#"):
                            continue
                        fields = [field.strip() for field in line.split(",")]
                        _require(
                            len(fields) == 6
                            and fields[0] == "0"
                            and fields[1] == fields[2] == str(count)
                            and fields[3] == "1"
                            and re.fullmatch(r"[0-9a-f]{32}", fields[5]),
                            "render_source_frame_proof_invalid",
                        )
                        count += 1
                        _require(
                            count <= source_frames, "render_source_frame_proof_invalid"
                        )
                _require(count == source_frames, "render_source_video_too_short")
            retime = (
                # Two bounded source-tail frames cover CFR half-up rounding,
                # even M=1 at2x; -frames:v still emits exactly the frozen N.
                "tpad=stop_mode=clone:stop=2,"
                f"setpts=(PTS-STARTPTS)/{shot['playback_rate']:.9f},fps=24,"
                if render["version"] == 5
                else "setpts=PTS-STARTPTS,"
            )
            # Normalize the source timeline and frame rate before choosing the
            # exact frame interval. This works independently of GOP seek points
            # and includes no user-written FFmpeg expression. Trimming precedes
            # scaling, so discarded frames are not needlessly resized.
            filters = (
                f"setpts=PTS-STARTPTS,fps=24,trim=start_frame={first}:end_frame={first + source_frames},"
                f"{retime}scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
            )
            args = [*media.input_options(source), "-i", source]
            if shape["simulation"]:
                # This PNG and fixed label are generated locally. No user text,
                # filename or filters enter the filter graph or image demuxer.
                args += [
                    "-protocol_whitelist",
                    "file,pipe",
                    "-f",
                    "image2",
                    "-pattern_type",
                    "none",
                    "-loop",
                    "1",
                    "-i",
                    self._path(directory, "simulation.png"),
                    "-filter_complex_threads",
                    "1",
                    "-filter_complex",
                    f"[0:v]{filters}[s];[s][1:v]overlay=0:0:shortest=1[v]",
                    "-map",
                    "[v]",
                ]
            else:
                args += ["-map", "0:v:0", "-vf", filters]
            args += [
                "-an",
                "-frames:v",
                shot["frames"],
                "-r",
                "24",
                "-fps_mode",
                "cfr",
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-fs",
                self.max_output_bytes + 1,
                output,
            ]
            self._run_process(args, directory, tag, heartbeat, deadline, output)
            info = media.probe(output)
            stream = next(
                (s for s in info["streams"] if s["codec_type"] == "video"), {}
            )
            _require(
                int(stream.get("nb_frames", 0)) == shot["frames"],
                "render_source_video_too_short",
            )
            total_clip_bytes += output.stat().st_size
            _require(
                total_clip_bytes <= self.max_output_bytes,
                "render_combined_video_size_limit",
            )
            clips.append(output)
        manifest = self._path(directory, "clips.txt")
        # MP4 format duration can be rounded to milliseconds (one 24fps frame
        # becomes .042s). Concat must advance by the validated frame count,
        # not accumulate those per-file header roundings across short shots.
        manifest.write_text(
            "".join(
                "file '" + path.name + "'\n" + f"duration {shot['frames'] / 24:.12f}\n"
                for path, shot in zip(clips, render["shots"])
            ),
            encoding="ascii",
        )
        joined = self._path(directory, "joined.mp4")
        self._run_process(
            [
                "-protocol_whitelist",
                "file,pipe",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                manifest,
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-an",
                "-fs",
                self.max_output_bytes + 1,
                joined,
            ],
            directory,
            tag,
            heartbeat,
            deadline,
            joined,
        )
        if subtitles is not None:
            self._write_subtitles(directory, shape, subtitles, subtitle_style)
            captioned = self._path(directory, "captioned.mp4")
            # All paths in the filter are fixed relative names under the trusted
            # cwd. No Windows drive/path escaping or user filter interpolation.
            self._run_process(
                [
                    *media.input_options(joined),
                    "-i",
                    joined,
                    "-map",
                    "0:v:0",
                    "-an",
                    "-vf",
                    "ass=filename=captions.ass",
                    "-c:v",
                    "libx264",
                    "-threads",
                    "2",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-r",
                    "24",
                    "-fps_mode",
                    "cfr",
                    "-frames:v",
                    shape["frames"],
                    "-fs",
                    self.max_output_bytes + 1,
                    captioned,
                ],
                directory,
                tag,
                heartbeat,
                deadline,
                captioned,
            )
            joined = captioned
        final = self._path(directory, "result.mp4")
        if shape["audio"]:
            args, filters, labels = [], [], []
            for index, track in enumerate(_audio_tracks(render)):
                source = self._path(
                    directory, state["inputs"][track["source_id"]]["filename"]
                )
                if render["version"] >= 2:
                    first, last = (
                        round(track["source_start"] * 32000),
                        round(track["source_end"] * 32000),
                    )
                    _require(last > first, "render_audio_empty_sample_range")
                    # Decode/resample before counting samples, then verify the
                    # unpadded segment. Header duration alone must not let a
                    # truncated input become apparently valid through apad.
                    segment = self._path(directory, f"audio-{index:03d}.flac")
                    self._run_process(
                        [
                            *media.input_options(source),
                            "-i",
                            source,
                            "-map",
                            "0:a:0",
                            "-vn",
                            "-af",
                            f"aresample=32000,aformat=channel_layouts=stereo,atrim=start_sample={first}:end_sample={last},asetpts=PTS-STARTPTS",
                            "-c:a",
                            "flac",
                            "-ar",
                            "32000",
                            "-ac",
                            "2",
                            "-sample_fmt",
                            "s16",
                            "-fs",
                            self.max_output_bytes + 1,
                            segment,
                        ],
                        directory,
                        tag,
                        heartbeat,
                        deadline,
                        segment,
                    )
                    info = media.probe(segment)
                    try:
                        samples = round(float(info["format"]["duration"]) * 32000)
                    except (KeyError, ValueError, TypeError, OverflowError):
                        raise BackendError(
                            "render_audio_segment_duration_unknown"
                        ) from None
                    _require(samples == last - first, "render_source_audio_too_short")
                    source = segment
                args += [*media.input_options(source), "-i", source]
                label = f"a{index}"
                trim = (
                    "asetpts=PTS-STARTPTS,"
                    if render["version"] >= 2
                    else f"atrim=start={track['source_start']:.9f}:end={track['source_end']:.9f},asetpts=PTS-STARTPTS,aresample=32000,aformat=channel_layouts=stereo,"
                )
                processing = ""
                if render["version"] == 5:
                    output_samples = track["output_samples"]
                    processing = (
                        f"atempo={track['playback_rate']:.9f},"
                        f"apad=whole_len={output_samples},atrim=end_sample={output_samples},"
                        "asetpts=PTS-STARTPTS,"
                    )
                    if track["fade_in_samples"]:
                        processing += f"afade=t=in:ss=0:ns={track['fade_in_samples']},"
                    if track["fade_out_samples"]:
                        processing += (
                            f"afade=t=out:ss={output_samples - track['fade_out_samples']}:"
                            f"ns={track['fade_out_samples']},"
                        )
                filters.append(
                    f"[{index}:a:0]{trim}volume={track['gain']:.9f},{processing}"
                    f"adelay={round(track['timeline_start'] * 32000)}S:all=1,apad=whole_dur={duration:.9f}[{label}]"
                )
                labels.append(f"[{label}]")
            filters.append(
                "".join(labels)
                + f"amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,"
                f"alimiter=limit=0.95:level=0:latency=1,atrim=duration={duration:.9f},asetpts=PTS-STARTPTS[mix]"
            )
            audio = self._path(directory, "result.flac")
            args += [
                "-filter_complex_threads",
                "1",
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[mix]",
                "-vn",
                "-c:a",
                "flac",
                "-ar",
                "32000",
                "-ac",
                "2",
                "-sample_fmt",
                "s16",
                "-fs",
                self.max_output_bytes + 1,
                audio,
            ]
            self._run_process(args, directory, tag, heartbeat, deadline, audio)
            self._run_process(
                [
                    *media.input_options(joined),
                    "-i",
                    joined,
                    *media.input_options(audio),
                    "-i",
                    audio,
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-ar",
                    "32000",
                    "-ac",
                    "2",
                    "-t",
                    duration,
                    "-movflags",
                    "+faststart",
                    "-fs",
                    self.max_output_bytes + 1,
                    final,
                ],
                directory,
                tag,
                heartbeat,
                deadline,
                final,
            )
        else:
            # Keep the verified concat checkpoint available after a crash.
            shutil.copyfile(joined, final)
        self._verify_outputs(directory, state)
        outputs = {
            "video": "result.mp4",
            **({"audio": "result.flac"} if shape["audio"] else {}),
        }
        state["outputs"] = {
            kind: {
                "filename": name,
                "size_bytes": (directory / name).stat().st_size,
                "sha256": _digest(directory / name),
            }
            for kind, name in outputs.items()
        }
        for entry in state["outputs"].values():
            with self._path(directory, entry["filename"]).open("r+b") as stream:
                os.fsync(stream.fileno())
        _sync_directory(directory)

    def _verify_outputs(self, directory, state):
        from fractions import Fraction

        shape = state["shape"]
        path = self._path(directory, "result.mp4")
        _require(
            path.is_file() and 0 < path.stat().st_size <= self.max_output_bytes,
            "render_output_missing",
        )
        info = media.probe(path)
        video = next(
            (s for s in info.get("streams", []) if s.get("codec_type") == "video"), {}
        )
        try:
            _require(
                video["codec_name"] == "h264"
                and video["width"] == shape["width"]
                and video["height"] == shape["height"]
                and abs(float(Fraction(video["avg_frame_rate"])) - 24) < 0.001
                and int(video["nb_frames"]) == shape["frames"]
                and abs(float(info["format"]["duration"]) - shape["duration"]) < 0.1,
                "render_video_verification_failed",
            )
        except (KeyError, ValueError, ZeroDivisionError):
            raise BackendError("render_video_verification_failed") from None
        audio_streams = [
            s for s in info.get("streams", []) if s.get("codec_type") == "audio"
        ]
        _require(bool(audio_streams) == shape["audio"], "render_unexpected_audio")
        if shape["audio"]:
            path = self._path(directory, "result.flac")
            _require(
                path.is_file() and 0 < path.stat().st_size <= self.max_output_bytes,
                "render_audio_missing",
            )
            info = media.probe(path)
            stream = next(
                (s for s in info.get("streams", []) if s.get("codec_type") == "audio"),
                {},
            )
            _require(
                stream.get("codec_name") == "flac"
                and stream.get("sample_rate") == "32000"
                and stream.get("channels") == 2
                and abs(float(info["format"].get("duration", 0)) - shape["duration"])
                < 0.1,
                "render_audio_verification_failed",
            )

    @contextmanager
    def _process_fence(self, directory):
        """An inherited OFD flock also fences an orphaned FFmpeg child."""
        if os.name != "posix":
            yield None
            return
        import fcntl

        path = self._path(directory, "cpu-process.lock")
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            _require(os.fstat(descriptor).st_nlink == 1, "render_process_fence_invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SubmissionUncertain("render_original_process_active") from None
            yield descriptor
        finally:
            # Closing our FD cannot release a lock still held by an orphan.
            os.close(descriptor)

    def _execute(self, state, directory, tag, heartbeat, *, resumed=False):
        with self._process_fence(directory) as process_fence:
            state["phase"] = "submitting"
            if not resumed:
                state["cpu_checkpoint_version"] = 1 if os.name == "posix" else 0
            self._save(directory, state)
            with self._mutex:
                self._active[tag] = {
                    "process": None,
                    "cancelled": threading.Event(),
                    "process_fence": process_fence,
                }
            try:
                self._render(state, directory, tag, heartbeat)
                # _run_process persists each checkpoint using its own fresh copy.
                state["checkpoints"] = self._load(directory).get("checkpoints", {})
                state["phase"] = "succeeded"
                self._save(directory, state)
                return state["task_id"]
            except _Cancelled:
                state["phase"] = "cancelled"
                self._save(directory, state)
                return state["task_id"]
            except Exception:
                state["phase"] = "failed"
                self._save(directory, state)
                raise SubmissionRejected("render_cpu_attempt_failed") from None
            finally:
                with self._mutex:
                    self._active.pop(tag, None)

    def resume(self, job, tag, task_id, heartbeat):
        """Recover only the same fenced CPU receipt, never a new GPU submit."""
        if not self.enabled:
            raise NotReady("backend_disabled")
        directory = self._directory(tag)
        with _slot_lock(directory, "attempt") as acquired:
            if not acquired:
                raise SubmissionUncertain("render_attempt_busy")
            state = self._load(directory)
            _require(
                os.name == "posix"
                and state.get("cpu_checkpoint_version") == 1
                and state.get("phase") == "submitting"
                and state.get("tag") == tag
                and state.get("task_id") == task_id == "cpu-render-" + tag
                and state.get("identity")
                == _hash(
                    {
                        "owner": job["owner_id"],
                        "job": job["id"],
                        "payload": job["request"],
                    }
                ),
                "render_resume_identity_unconfirmed",
            )
            heartbeat()  # Original WorkerRunner lease/fence, not SDK authority.
            return self._execute(state, directory, tag, heartbeat, resumed=True)

    def submit(self, prepared, tag):
        if not self.enabled:
            raise NotReady("backend_disabled")
        directory = self._directory(tag)
        with _slot_lock(directory, "attempt") as acquired:
            if not acquired:
                raise SubmissionUncertain("render_attempt_busy")
            state = self._load(directory)
            _require(
                prepared.get("tag") == tag
                and prepared.get("identity") == state["identity"],
                "render_prepared_identity_mismatch",
            )
            if state["phase"] != "prepared":
                raise SubmissionUncertain("render_submission_already_recorded")
            return self._execute(state, directory, tag, prepared["heartbeat"])

    def reconcile(self, tag, task_id=None):
        directory = self._directory(tag)
        if not (directory / "state.json").exists():
            return Outcome("unknown")
        expected = "cpu-render-" + tag
        _require(
            task_id is None or task_id == expected, "render_task_identity_mismatch"
        )
        with _slot_lock(directory, "attempt") as acquired:
            if not acquired:
                return Outcome(
                    "running" if tag in self._active else "unknown", expected
                )
            state = self._load(directory)
            _require(
                state.get("tag") == tag and state.get("task_id") == expected,
                "render_state_identity_mismatch",
            )
            phase = state["phase"]
            if phase in {"preparing", "prepared", "preparation_failed"}:
                return Outcome("unknown")
            if phase == "succeeded":
                self._verify_outputs(directory, state)
                for item in state["outputs"].values():
                    path = self._path(directory, item["filename"])
                    _require(
                        path.stat().st_size == item["size_bytes"]
                        and _digest(path) == item["sha256"],
                        "render_output_changed",
                    )
            # An interrupted submitting state is deliberately not re-rendered.
            return Outcome(
                phase if phase in {"succeeded", "failed", "cancelled"} else "unknown",
                expected,
                0 if phase in {"succeeded", "failed", "cancelled"} else None,
            )

    def poll(self, tag, task_id):
        return self.reconcile(tag, task_id)

    def cancel(self, tag, task_id):
        self._directory(tag)
        _require(task_id == "cpu-render-" + tag, "render_task_identity_mismatch")
        with self._mutex:
            preparing = self._preparing.get(tag)
            if preparing is not None:
                preparing.set()
                return True
            active = self._active.get(tag)
            if active:
                active["cancelled"].set()
                return True
        # A PID in an old receipt would not prove ownership after restart.
        return False

    def fetch(self, job, tag, task_id, target_dir, heartbeat):
        heartbeat()
        outcome = self.reconcile(tag, task_id)
        _require(outcome.state == "succeeded", "render_output_unavailable")
        directory = self._directory(tag)
        state = self._load(directory)
        _require(
            state["identity"]
            == _hash(
                {"owner": job["owner_id"], "job": job["id"], "payload": job["request"]}
            ),
            "render_fetch_identity_mismatch",
        )
        return {
            kind: self._path(directory, item["filename"])
            for kind, item in state["outputs"].items()
        }
