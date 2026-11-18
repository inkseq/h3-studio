"""Join shots that are already the film's own encoding (#1252).

A first film is six whole hosted shots at the film's size. The renderer
decoded and encoded each of them again before joining: 37-52 s of a 74-97 s
export on a 2-vCPU host (staging 2026-11-18), and one more lossy generation.
When every shot is its whole source and every source already has the exact
stream the renderer would write, with one identical parameter header, the
sources are joined as they are. Anything else is encoded exactly as before.

The collected result is still fully decoded and frame-counted by the worker
before it is stored, as every render is.
"""

from fractions import Fraction
import json
import subprocess

from .media import LIGHT_MEDIA_ADDRESS_SPACE_BYTES, input_options
from .media_process import run_media_process

FIELDS = (
    "stream=codec_name,profile,level,pix_fmt,width,height,sample_aspect_ratio,"
    "field_order,r_frame_rate,avg_frame_rate,start_time,nb_frames,"
    "nb_read_packets,extradata_hash"
)


def _facts(path):
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


def _exact(facts, width, height, frames):
    """Exactly what the renderer's own clip encode produces for this shot."""
    try:
        return (
            facts["codec_name"] == "h264"
            and facts["pix_fmt"] == "yuv420p"
            and (facts["width"], facts["height"]) == (width, height)
            # Unknown is not square: the encode path forces setsar=1.
            and facts["sample_aspect_ratio"] == "1:1"
            and facts["field_order"] == "progressive"
            and Fraction(facts["r_frame_rate"]) == 24
            and Fraction(facts["avg_frame_rate"]) == 24
            and float(facts["start_time"]) == 0
            # Header and packets both: a header claim alone is not a frame.
            and int(facts["nb_frames"]) == frames
            and int(facts["nb_read_packets"]) == frames
            and bool(facts["extradata_hash"])
        )
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        return False


def copyable(render, shape, path_of):
    """True when every shot can be joined as its source file.

    All or nothing: one shot that must be encoded gets the encoder's
    parameter header, and a join of two different headers is not a stream
    every player accepts.
    """
    if shape["simulation"]:
        return False  # The simulation mark is drawn by the encode.
    seen, header = {}, None
    for shot in render["shots"]:
        frames = shot["frames"]
        if (
            shot.get("source_start_frame", 0) != 0
            or shot.get("source_frames", frames) != frames
            or shot.get("playback_rate", 1) != 1
        ):
            return False
        path = path_of(shot)
        if path not in seen:
            seen[path] = _facts(path)
        facts = seen[path]
        if facts is None or not _exact(facts, shape["width"], shape["height"], frames):
            return False
        mark = (facts.get("profile"), facts.get("level"), facts["extradata_hash"])
        if header is None:
            header = mark
        elif mark != header:
            return False
    return True
