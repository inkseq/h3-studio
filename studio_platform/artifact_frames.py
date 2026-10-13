"""Bounded CPU frame extraction from an already owned, verified video.

No model execution, provider call or alternative media/job authority lives here.
The caller holds AssetService's existing operation and processing admission.
"""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import math
import re

from . import media
from .storage import key_belongs_to


def selection(body):
    if (not isinstance(body, dict) or not isinstance(body.get("position"), str)
            or body["position"] not in {"last", "time"}):
        raise media.MediaError("截帧需要position为last或time")
    if body["position"] == "last":
        if set(body) != {"position"}:
            raise media.MediaError("末帧截取只接受position=last")
        return {"position": "last"}
    if (set(body) != {"position", "time_seconds"}
            or type(body["time_seconds"]) not in (int, float)
            or not math.isfinite(body["time_seconds"])
            or not 0 <= body["time_seconds"] < 600):
        raise media.MediaError("截帧时间须为0至600秒内的有限数字，且小于视频时长")
    return {"position": "time", "time_seconds": float(body["time_seconds"])}


def source_spec(metadata, owner, max_bytes):
    """Only normalized generated MP4 receipts; never accept a client URL/path."""
    if (not isinstance(metadata, dict) or metadata.get("kind") != "video"
            or metadata.get("validated") is not True
            or metadata.get("content_type", metadata.get("mime")) != "video/mp4"
            or not key_belongs_to(metadata.get("object_key", ""), owner)
            or type(metadata.get("size_bytes")) is not int
            or not 1 <= metadata["size_bytes"] <= max_bytes
            or not isinstance(metadata.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])):
        raise media.MediaError("截帧来源需要已核验的视频产物和完整存储凭据")
    return {"key": metadata["object_key"], "size_bytes": metadata["size_bytes"],
            "sha256": metadata["sha256"], "content_type": "video/mp4"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def extract(path, target_dir, requested, *, max_output_bytes):
    info = media.inspect(path, "video")
    data = media.probe(path)
    videos = [stream for stream in data.get("streams", [])
              if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")]
    try:
        video = videos[0]
        fps = Fraction(video["avg_frame_rate"])
        nominal_fps = Fraction(video.get("r_frame_rate", "0"))
        frames = int(video["nb_frames"])
        duration = frames / float(fps)
    except (IndexError, KeyError, TypeError, ValueError, ZeroDivisionError):
        raise media.MediaError("截帧来源缺少可核对的帧数或帧率") from None
    if (len(videos) != 1 or not 0 < fps <= 120 or not 1 <= frames <= 72000
            or not 0 < duration <= 600 or abs(info["source_duration"] - duration) > .1
            or nominal_fps != fps
            or info.get("source_rotation_degrees") != 0):
        raise media.MediaError("截帧仅支持时长不超过600秒、帧网格明确的已核验视频")
    if requested["position"] == "time" and requested["time_seconds"] >= duration:
        raise media.MediaError("截帧时间超出视频范围；请选择小于视频时长的位置")
    frame_index = frames - 1 if requested["position"] == "last" else min(
        frames - 1, math.floor(requested["time_seconds"] * float(fps)))
    target = target_dir / "extracted.png"
    # Decode from the original stream and select the exact decoded index. The
    # final index preserves the true last frame, with no seek/sentinel shortcut.
    media.ffmpeg(["-xerror", "-i", path, "-map", f'0:{info["video_stream_index"]}',
                  "-an", "-sn", "-dn", "-vf", f"select=eq(n\\,{frame_index})",
                  "-fps_mode", "passthrough", "-frames:v", "1", "-c:v", "png",
                  "-threads", "1", "-fs", str(max_output_bytes), target], timeout=180)
    if not target.is_file() or not target.stat().st_size:
        raise media.MediaError("所选帧未完整解码；原视频保持不变")
    media.check_output_size(target, max_output_bytes)
    media.inspect(target, "image")
    return target, {"frame_index": frame_index, "time_seconds": frame_index / float(fps),
                    "source_frame_count": frames, "source_fps": float(fps)}
