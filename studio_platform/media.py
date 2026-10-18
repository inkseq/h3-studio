"""Bounded media inspection and explicit reference derivation on CPU."""

from __future__ import annotations

import json
import io
import math
from fractions import Fraction
from collections import deque
from contextlib import contextmanager
from pathlib import Path
import subprocess
import threading
import time

from PIL import Image, ImageOps, UnidentifiedImageError
from .media_process import run_media_process, DEFAULT_ADDRESS_SPACE_BYTES

LIGHT_MEDIA_ADDRESS_SPACE_BYTES = 512 * 1024**2

EXTENSIONS = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".mp4": "video",
    ".mov": "video",
    ".wav": "audio",
    ".mp3": "audio",
    ".flac": "audio",
}
MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
}
DEMUXERS = "mov,wav,mp3,flac"
INPUT_OPTIONS = [
    "-protocol_whitelist",
    "file,pipe",
    "-format_whitelist",
    DEMUXERS,
    "-max_streams",
    "16",
]
DEMUXER_BY_SUFFIX = {
    ".mp4": "mov",
    ".mov": "mov",
    ".wav": "wav",
    ".mp3": "mp3",
    ".flac": "flac",
}
PROBE_FIELDS = (
    "format=duration,format_name:stream=index,codec_type,codec_name,width,height,duration,sample_rate,"
    "channels,nb_frames,r_frame_rate,avg_frame_rate:stream_disposition=attached_pic:stream_side_data=rotation"
)


def input_options(path):
    demuxer = DEMUXER_BY_SUFFIX.get(Path(path).suffix.lower())
    if demuxer is None:
        raise MediaError("不支持此媒体文件扩展名")
    options = [*INPUT_OPTIONS, "-f", demuxer]
    if demuxer == "mov":
        options.extend(["-enable_drefs", "0", "-use_absolute_path", "0"])
    return options


class MediaError(ValueError):
    pass


class MediaBusy(MediaError):
    """A saved upload can retry its existing receipt when CPU capacity frees."""


class ProcessingAdmission:
    """Fair process-local memory admission for the single API worker.

    A video decoder/encoder uses the entire budget; at most two image/audio
    preparations can run together. Durable upload quotas remain independent.
    """

    def __init__(self, *, wait_seconds=30):
        if (
            type(wait_seconds) not in (int, float)
            or not math.isfinite(wait_seconds)
            or wait_seconds < 0
        ):
            raise ValueError("invalid media admission wait")
        self.wait_seconds = wait_seconds
        self._condition = threading.Condition()
        self._waiting = deque()
        self._used = 0

    @contextmanager
    def acquire(self, kind):
        if kind not in {"image", "audio", "video"}:
            raise MediaError("未知素材类型")
        weight = 2 if kind == "video" else 1
        ticket = object()
        deadline = time.monotonic() + self.wait_seconds
        acquired = False
        with self._condition:
            self._waiting.append(ticket)
            try:
                while self._waiting[0] is not ticket or self._used + weight > 2:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MediaBusy(
                            "素材处理繁忙；原件已保留，请在素材列表恢复同一素材"
                        )
                    self._condition.wait(remaining)
                self._waiting.popleft()
                self._used += weight
                acquired = True
                self._condition.notify_all()
            finally:
                if not acquired:
                    self._waiting.remove(ticket)
                    self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._used -= weight
                self._condition.notify_all()


# All AssetService instances in this process share the same memory budget.
# Deployment pins one Uvicorn process per container; scaling needs another
# separately limited container, not extra unaccounted API worker processes.
PROCESSING_ADMISSION = ProcessingAdmission()

# Default x264 lookahead/reference buffers exceeded the 3 GiB API container
# on an accepted 5760x3240 upload. These explicit bounds keep dimensions,
# CRF18 and frame timing; compression/quality is not claimed identical.
VIDEO_ENCODING = [
    "-c:v",
    "libx264",
    "-threads",
    "2",
    "-preset",
    "veryfast",
    "-crf",
    "18",
    "-x264-params",
    "rc-lookahead=0:sync-lookahead=0:bframes=0:ref=1",
    "-pix_fmt",
    "yuv420p",
]


def probe(path: Path):
    try:
        result = run_media_process(
            [
                "ffprobe",
                "-v",
                "error",
                *input_options(path),
                "-show_entries",
                PROBE_FIELDS,
                "-of",
                "json",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=True,
            memory_bytes=LIGHT_MEDIA_ADDRESS_SPACE_BYTES,
        )
        value = json.loads(result.stdout)
        formats = set(value.get("format", {}).get("format_name", "").split(","))
        if not formats.intersection(DEMUXERS.split(",")):
            raise MediaError("不支持此媒体容器；引用清单不能用作媒体文件")
        return value
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        raise MediaError(
            "文件检查失败、工具不可用或达到资源限制；原素材保持不变"
        ) from None


def ffmpeg(args, timeout=180, *, memory_bytes=DEFAULT_ADDRESS_SPACE_BYTES):
    # Scope these input-only restrictions to EVERY input. Restricting protocols
    # alone still permits concat/HLS manifests to read other local files.
    safe_args = []
    for index, value in enumerate(args):
        if str(value) == "-i":
            if index + 1 >= len(args):
                raise MediaError("媒体输入参数不完整")
            safe_args.extend(input_options(args[index + 1]))
        safe_args.append(str(value))
    try:
        # Error text is deliberately not consumed: corrupt media may produce
        # arbitrarily many decoder diagnostics. Keep it out of RAM and logs;
        # callers receive the same bounded, static error below.
        run_media_process(
            ["ffmpeg", "-v", "error", "-nostdin", "-y", "-threads", "2", *safe_args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
            memory_bytes=memory_bytes,
        )
    except (OSError, subprocess.SubprocessError):
        raise MediaError("媒体处理失败、超时或达到资源限制；原素材保持不变") from None


def _video_rotation(stream):
    # Only the whitelisted rotation number is requested from ffprobe, never
    # arbitrary tags, display-matrix strings or user-controlled metadata text.
    rotations = [
        item["rotation"]
        for item in stream.get("side_data_list", [])
        if "rotation" in item
    ]
    if len(rotations) > 1:
        raise MediaError("视频旋转信息不明确")
    rotation = rotations[0] if rotations else 0
    if (
        type(rotation) not in (int, float)
        or not math.isfinite(rotation)
        or abs(rotation) > 360
    ):
        raise MediaError("视频旋转信息无效")
    quarter_turn = round(rotation / 90)
    if not math.isclose(rotation, quarter_turn * 90, abs_tol=0.001):
        raise MediaError("目前仅支持0、90、180、270度的视频显示旋转")
    return (quarter_turn * 90) % 360


def _native_video_timing(video, audio, container_duration):
    """Recognize only a proven native grid, allowing <=1 ms container rounding.

    Audio stream duration must not be longer than the proven video timeline.
    This is deliberately stricter than the owned qualification fixture's AAC
    padding allowance: arbitrary user inputs never receive that exception.
    """
    try:
        frames = int(video["nb_frames"])
        if (
            not 56 <= frames <= 362
            or frames % 17 != 5
            or Fraction(video["avg_frame_rate"]) != 24
            or Fraction(video["r_frame_rate"]) != 24
        ):
            return None
        exact, stream_duration = frames / 24, float(video["duration"])
        if (
            not 2 <= exact <= 15
            or not math.isfinite(stream_duration)
            or abs(stream_duration - exact) > 1e-6
            or abs(container_duration - exact) > 0.001 + 1e-6
        ):
            return None
        audio_duration = float(audio["duration"]) if audio is not None else None
        if audio is not None and (
            not math.isfinite(audio_duration) or not 0 < audio_duration <= exact + 1e-6
        ):
            return None
        return {
            "duration_basis": "verified_native_video_frames_v1",
            "source_frame_count": frames,
            "source_fps": 24,
            "source_video_stream_duration": stream_duration,
            "source_audio_stream_duration": audio_duration,
            "duration_rounding_correction_seconds": exact - container_duration,
        }
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def inspect(path: Path, kind: str):
    if kind == "image":
        try:
            with Image.open(path) as source:
                expected = {
                    ".png": "PNG",
                    ".jpg": "JPEG",
                    ".jpeg": "JPEG",
                    ".webp": "WEBP",
                }.get(path.suffix.lower())
                if expected is None or source.format != expected:
                    raise MediaError("图片实际格式与扩展名不一致")
                width, height = source.size
                # EXIF rotation swaps sides; these symmetric bounds apply before
                # decoding and prevent allocating oversized pixel buffers.
                if (
                    min(width, height) < 256
                    or max(width, height) > 5760
                    or not 0.4 <= width / height <= 2.5
                ):
                    raise MediaError("参考图片边长须256–5760，宽高比0.4–2.5")
                if getattr(source, "n_frames", 1) != 1:
                    raise MediaError("仅支持静态参考图片")
                source.load()
                oriented = ImageOps.exif_transpose(source)
                width, height = oriented.size
                if (
                    min(width, height) < 256
                    or max(width, height) > 5760
                    or not 0.4 <= width / height <= 2.5
                ):
                    raise MediaError("参考图片边长须256–5760，宽高比0.4–2.5")
            return {
                "kind": kind,
                "width": width,
                "height": height,
                "duration": None,
                "source_duration": None,
                "has_audio": False,
                "model_ready": True,
                "notes": [],
            }
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
            raise MediaError("图片解码失败或像素过大") from None
    data = probe(path)
    expected = {
        ".mp4": "mov",
        ".mov": "mov",
        ".wav": "wav",
        ".mp3": "mp3",
        ".flac": "flac",
    }.get(path.suffix.lower())
    if expected is None or expected not in data["format"]["format_name"].split(","):
        raise MediaError("媒体实际容器与扩展名不一致")
    videos = [
        s
        for s in data.get("streams", [])
        if s.get("codec_type") == "video"
        and not s.get("disposition", {}).get("attached_pic")
    ]
    audios = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        raise MediaError("无法确定素材时长") from None
    if not math.isfinite(duration) or not 0.1 <= duration <= 3600:
        raise MediaError("素材时长须为0.1秒至1小时；长素材请先本地裁剪")
    if kind == "video" and not videos:
        raise MediaError("视频区需要包含真正的视频轨")
    if kind == "audio" and (videos or not audios):
        raise MediaError("音频区需要纯音频文件")

    def selected_index(streams):
        if not streams:
            return None
        index = streams[0].get("index")
        if type(index) is not int or not 0 <= index < 16:
            raise MediaError("无法确定受检媒体轨道")
        return index

    result = {
        "kind": kind,
        "duration": duration,
        "source_duration": duration,
        "has_audio": bool(audios),
        "model_ready": 2 <= duration <= 15,
        "notes": [],
        "video_stream_index": selected_index(videos),
        "audio_stream_index": selected_index(audios),
    }
    if videos:
        result["container_duration"] = duration
        timing = _native_video_timing(
            videos[0], audios[0] if audios else None, duration
        )
        if timing:
            duration = timing["source_frame_count"] / timing["source_fps"]
            result.update(
                timing,
                duration=duration,
                source_duration=duration,
                model_ready=2 <= duration <= 15,
            )
            if abs(timing["duration_rounding_correction_seconds"]) > 1e-6:
                result["notes"].append(
                    "已核验24fps原生帧数，仅校正不超过1毫秒的容器时长舍入；原文件保留"
                )
    if len(videos) > 1 or len(audios) > 1:
        result["notes"].append(
            "模型副本仅选第一个实际视频轨和第一个音轨；原文件所有轨道保持不变"
        )
    if not result["model_ready"]:
        result["notes"].append("已保留原始素材；生成参考需要先选择2–15秒的片段")
    if videos:
        width, height = videos[0].get("width", 0), videos[0].get("height", 0)
        if (
            min(width, height) < 256
            or max(width, height) > 5760
            or not 0.4 <= width / height <= 2.5
        ):
            raise MediaError("参考视频边长须256–5760，宽高比0.4–2.5")
        rotation = _video_rotation(videos[0])
        result.update(
            source_rotation_degrees=rotation,
            source_coded_width=width,
            source_coded_height=height,
        )
        if rotation in {90, 270}:
            width, height = height, width
        result.update(width=width, height=height)
    return result


def _source_selection(path, metadata):
    """Bind decoding to the original streams inspected by this module.

    Old persisted assets lack indices. Re-inspect their held original rather
    than assuming that video/audio ordinals or provider defaults are stable.
    Derived containers get fresh indices when AssetService inspects the new
    file; these values describe the original, not the normalized model copy.
    """
    if (
        "video_stream_index" not in metadata
        or "audio_stream_index" not in metadata
        or (
            metadata["kind"] == "video"
            and (
                "source_rotation_degrees" not in metadata
                or "container_duration" not in metadata
            )
        )
    ):
        return inspect(path, metadata["kind"])
    video, audio = metadata["video_stream_index"], metadata["audio_stream_index"]
    for index in (video, audio):
        if index is not None and (type(index) is not int or not 0 <= index < 16):
            raise MediaError("受检媒体轨道记录无效")
    if (
        (metadata["kind"] == "video" and video is None)
        or (metadata["kind"] == "audio" and (video is not None or audio is None))
        or bool(metadata.get("has_audio")) != (audio is not None)
    ):
        raise MediaError("受检媒体轨道记录不一致")
    if metadata["kind"] == "video" and (
        type(metadata["source_rotation_degrees"]) is not int
        or metadata["source_rotation_degrees"] not in {0, 90, 180, 270}
    ):
        raise MediaError("受检媒体旋转记录无效")
    return metadata


def _checked_video(data, metadata):
    videos = [
        stream
        for stream in data.get("streams", [])
        if stream.get("codec_type") == "video"
        and not stream.get("disposition", {}).get("attached_pic")
    ]
    if (
        len(videos) != 1
        or videos[0].get("width") != metadata["width"]
        or videos[0].get("height") != metadata["height"]
        or _video_rotation(videos[0]) != 0
    ):
        raise MediaError("处理后视频尺寸与受检轨道不一致")
    return videos[0]


class _LimitedImageOutput:
    def __init__(self, target, maximum):
        self.target, self.maximum = target, maximum

    def write(self, value):
        if self.target.tell() + len(value) > self.maximum:
            raise MediaError("规范化图片超过容量上限，原件已保留")
        return self.target.write(value)

    def tell(self):
        return self.target.tell()

    def seek(self, position, whence=0):
        return self.target.seek(position, whence)

    def flush(self):
        return self.target.flush()

    def fileno(self):
        # Force Pillow's Python write path; a direct C file-descriptor write
        # would bypass the byte bound above.
        raise io.UnsupportedOperation("bounded image output")


def check_output_size(path, maximum):
    if type(maximum) is not int or maximum < 1 or path.stat().st_size > maximum:
        raise MediaError("处理后素材超过单文件容量上限，原件已保留")


def normalize(
    path: Path, metadata: dict, target_dir: Path, *, max_output_bytes=512 * 1024 * 1024
):
    """Returns model file and model metadata; original bytes are never replaced."""
    if metadata["kind"] in {"video", "audio"}:
        metadata = _source_selection(path, metadata)
    result = dict(metadata)
    result["notes"] = list(metadata.get("notes", []))
    if not metadata["model_ready"]:
        return None, result
    kind = metadata["kind"]
    if kind == "image":
        target = target_dir / "normalized.png"
        with Image.open(path) as im, target.open("wb") as stream:
            ImageOps.exif_transpose(im).convert("RGB").save(
                _LimitedImageOutput(stream, max_output_bytes), format="PNG"
            )
        check_output_size(target, max_output_bytes)
        return target, result
    duration = metadata["source_duration"]
    if kind == "audio":
        target = target_dir / "normalized.wav"
        ffmpeg(
            [
                "-i",
                path,
                "-map",
                f'0:{metadata["audio_stream_index"]}',
                "-vn",
                "-ar",
                "32000",
                "-ac",
                "2",
                "-fs",
                max_output_bytes + 1,
                target,
            ],
            memory_bytes=LIGHT_MEDIA_ADDRESS_SPACE_BYTES,
        )
        check_output_size(target, max_output_bytes)
        checked = probe(target)
        stream = next(
            (s for s in checked.get("streams", []) if s.get("codec_type") == "audio"),
            {},
        )
        if (
            stream.get("sample_rate") != "32000"
            or stream.get("channels") != 2
            or abs(float(checked["format"].get("duration", 0)) - duration) > 0.1
        ):
            raise MediaError("音频归一化验证失败")
        result.update(sample_rate=32000, channels=2)
        result["notes"].append("模型副本为32kHz双声道，原音频保留")
        return target, result
    if metadata.get("duration_basis") == "verified_native_video_frames_v1":
        frames = metadata["source_frame_count"]
        if (
            type(frames) is not int
            or not 56 <= frames <= 362
            or frames % 17 != 5
            or metadata.get("source_fps") != 24
            or abs(duration - frames / 24) > 1e-6
        ):
            raise MediaError("受检视频帧时长记录不一致")
    else:
        frames = max(56, 17 * math.ceil((math.ceil(duration * 24) - 5) / 17) + 5)
    target = target_dir / "normalized.mp4"
    args = [
        "-i",
        path,
        "-map",
        f'0:{metadata["video_stream_index"]}',
        "-vf",
        "fps=24,tpad=stop_mode=clone:stop_duration=1",
        "-frames:v",
        frames,
        *VIDEO_ENCODING,
    ]
    if metadata["has_audio"]:
        args.extend(
            [
                "-map",
                f'0:{metadata["audio_stream_index"]}',
                "-af",
                "apad",
                "-ar",
                "32000",
                "-ac",
                "2",
                "-c:a",
                "aac",
                "-t",
                frames / 24,
            ]
        )
    else:
        args.append("-an")
    ffmpeg([*args, "-movflags", "+faststart", "-fs", max_output_bytes + 1, target])
    check_output_size(target, max_output_bytes)
    data = probe(target)
    video = _checked_video(data, metadata)
    if int(video.get("nb_frames", 0)) != frames:
        raise MediaError("视频归一化帧数验证失败")
    result.update(duration=frames / 24, fps=24, frame_count=frames)
    timing_note = (
        "保留已核验原生帧网格"
        if metadata.get("duration_basis") == "verified_native_video_frames_v1"
        else "末帧延长到原生帧网格"
    )
    result["notes"].append(f"模型副本为24fps/{frames}帧，{timing_note}；原视频保留")
    return target, result


def derive(
    path: Path,
    metadata: dict,
    target_dir: Path,
    start,
    end,
    *,
    max_output_bytes=512 * 1024 * 1024,
):
    if metadata["kind"] not in {"video", "audio"}:
        raise MediaError("当前仅视频/音频支持时间选段")
    metadata = _source_selection(path, metadata)
    if (
        type(start) not in (int, float)
        or type(end) not in (int, float)
        or not math.isfinite(start)
        or not math.isfinite(end)
        or start < 0
        or end > metadata["source_duration"] + 0.01
        or not 2 <= end - start <= 15
    ):
        raise MediaError("选段须在原素材范围内，且长度为2–15秒")
    if metadata["kind"] == "video":
        target = target_dir / "trimmed.mp4"
        args = [
            "-i",
            path,
            "-ss",
            start,
            "-t",
            end - start,
            "-map",
            f'0:{metadata["video_stream_index"]}',
        ]
        if metadata["audio_stream_index"] is not None:
            args.extend(["-map", f'0:{metadata["audio_stream_index"]}', "-c:a", "aac"])
        else:
            args.append("-an")
        args.extend([*VIDEO_ENCODING, "-movflags", "+faststart", target])
    else:
        target = target_dir / "trimmed.wav"
        args = [
            "-i",
            path,
            "-ss",
            start,
            "-t",
            end - start,
            "-map",
            f'0:{metadata["audio_stream_index"]}',
            "-vn",
            "-ar",
            "32000",
            "-ac",
            "2",
            target,
        ]
    ffmpeg(
        [*args[:-1], "-fs", max_output_bytes + 1, args[-1]],
        memory_bytes=LIGHT_MEDIA_ADDRESS_SPACE_BYTES
        if metadata["kind"] == "audio"
        else DEFAULT_ADDRESS_SPACE_BYTES,
    )
    check_output_size(target, max_output_bytes)
    checked = inspect(target, metadata["kind"])
    if metadata["kind"] == "video" and (checked["width"], checked["height"]) != (
        metadata["width"],
        metadata["height"],
    ):
        raise MediaError("处理后视频尺寸与受检轨道不一致")
    if abs(checked["source_duration"] - (end - start)) > 0.1:
        raise MediaError("选段时长验证失败")
    return target
