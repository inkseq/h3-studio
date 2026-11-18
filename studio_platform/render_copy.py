"""Join shots that are already the renderer's own encoding (#1252).

A first film is six whole hosted shots at the film's size. The renderer
decoded and encoded each of them again before joining: 37-54 s of a 74-97 s
export on a 2-vCPU host (staging 2026-11-18), and one more lossy generation.

The sources are joined as they are only when every shot is its whole source
and every source carries exactly the stream the renderer's clip encode would
write for it. "Exactly" is measured, not assumed: one frame of a source goes
through that encode (REFERENCE), and every source must have that same
parameter header. A header that is merely shared between the sources is not
enough: a later film join copies chapters bit for bit, and a chapter with
another encoder's header next to an encoded chapter is a stream some players
show as garbage. Anything else is encoded exactly as before.

The collected result is still fully decoded and frame-counted by the worker
before it is stored, as every render is.
"""

from fractions import Fraction
import json
import subprocess

from .media import LIGHT_MEDIA_ADDRESS_SPACE_BYTES, input_options
from .media_process import run_media_process

REFERENCE = "header.mp4"
# The clip encoder. The reference frame and the real clips share it, so the
# measured header is the one the encode path writes.
CLIP_ENCODER = (
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
)
FIELDS = (
    "stream=index,codec_name,profile,level,pix_fmt,width,height,"
    "sample_aspect_ratio,field_order,r_frame_rate,avg_frame_rate,start_time,"
    "nb_frames,nb_read_packets,extradata_hash:stream_side_data=rotation"
)


def facts(path):
    """The first video stream as read from its packets, or None."""
    try:
        result = run_media_process(
            [
                "ffprobe",
                "-v",
                "error",
                *input_options(path),
                "-select_streams",
                "v:0",
                "-count_packets",
                "-show_data_hash",
                "sha256",
                "-show_entries",
                FIELDS,
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
        return json.loads(result.stdout)["streams"][0]
    except (OSError, subprocess.SubprocessError, ValueError, LookupError, TypeError):
        return None


def header(found):
    """What must be identical for two streams to join as one."""
    try:
        return (found["profile"], found["level"], found["extradata_hash"])
    except (KeyError, TypeError):
        return None


def exact(found, width, height, frames):
    """Exactly what the renderer's own clip encode produces for this shot."""
    try:
        return (
            # The join takes stream 0 of every file.
            found["index"] == 0
            and found["codec_name"] == "h264"
            and found["pix_fmt"] == "yuv420p"
            and (found["width"], found["height"]) == (width, height)
            # Unknown is not square: the encode path forces setsar=1.
            and found["sample_aspect_ratio"] == "1:1"
            and found["field_order"] == "progressive"
            and Fraction(found["r_frame_rate"]) == 24
            and Fraction(found["avg_frame_rate"]) == 24
            and float(found["start_time"]) == 0
            # Header and packets both: a header claim alone is not a frame.
            and int(found["nb_frames"]) == frames
            and int(found["nb_read_packets"]) == frames
            and bool(found["extradata_hash"])
            # A rotated upload is turned upright by the encode; a copy keeps
            # the rotation and the film would play on its side.
            and not found.get("side_data_list")
        )
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        return False


def shared_header(render, shape, path_of, max_bytes, heartbeat=lambda: None):
    """The one header of every source when all shots are whole, else None.

    All or nothing: one shot that must be encoded gets the encoder's header,
    and the film is then encoded throughout, as before.
    """
    if shape["simulation"]:
        return None  # The simulation mark is drawn by the encode.
    seen, common, total = {}, None, 0
    for shot in render["shots"]:
        frames = shot["frames"]
        if (
            shot.get("source_start_frame", 0) != 0
            or shot.get("source_frames", frames) != frames
            or shot.get("playback_rate", 1) != 1
        ):
            return None
        path = path_of(shot)
        if path not in seen:
            heartbeat()
            seen[path] = facts(path)
        found = seen[path]
        if found is None or not exact(found, shape["width"], shape["height"], frames):
            return None
        # Whole files, sound included: over the limit the encode still fits.
        total += path.stat().st_size
        mark = header(found)
        if total > max_bytes or mark is None or mark != (common or mark):
            return None
        common = mark
    return common


def reference_args(source, width, height, output):
    """One frame of `source` through the renderer's clip encode."""
    return [
        *input_options(source),
        "-i",
        source,
        "-map",
        "0:v:0",
        "-vf",
        "setpts=PTS-STARTPTS,fps=24,trim=start_frame=0:end_frame=1,"
        "setpts=PTS-STARTPTS,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
        f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-an",
        "-frames:v",
        1,
        "-r",
        "24",
        "-fps_mode",
        "cfr",
        *CLIP_ENCODER,
        output,
    ]
