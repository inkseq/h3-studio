"""Rewrap a collected output that is already our own exact encode (#1252).

The worker used to encode every hosted shot a second time: the EveryFrame
adapter normalizes the vendor file to the compiled size, 24 fps and duration
(one libx264 encode), then collection encoded that result again before its
checks. With six shots finishing together on a 2-vCPU host, the second encode
took 10-24 s per shot (staging 2026-11-18). An output that already passes
the same checks collection applies afterwards is copied instead; anything
else is encoded exactly as before.
"""

from fractions import Fraction

from .inference.outputs import _request, _shape
from .inference.protocol import BackendError
from .media import pixel_format, probe
from .phase_timing import lap
from .providers.registry import CPU_RENDER, EVERYFRAME_RENDER


def _exact(video, width, height, duration, audio, data=None):
    """The checks _validate_video makes after the encode, made before it."""
    try:
        data = data or probe(video)
        streams = data.get("streams", [])
        picture = next(s for s in streams if s.get("codec_type") == "video")
        sounds = [s for s in streams if s.get("codec_type") == "audio"]
        if (
            picture.get("codec_name") != "h264"
            or (picture["width"], picture["height"]) != (width, height)
            or abs(float(Fraction(picture["avg_frame_rate"])) - 24) > 0.001
            or int(picture.get("nb_frames", 0)) != round(duration * 24)
            or abs(float(data["format"]["duration"]) - duration) > 0.1
            or bool(sounds) != bool(audio)
            or pixel_format(video) != "yuv420p"
        ):
            return False
        if audio:
            sound = sounds[0]
            length = float(sound.get("duration", data["format"]["duration"]))
            if (
                sound.get("codec_name") != "aac"
                or int(sound["sample_rate"]) != 32000
                or sound["channels"] != 2
                or abs(length - duration) > 0.1
            ):
                return False
        return True
    except Exception:
        return False


def rewrap_args(backend, video, job, audio, data=None):
    """ffmpeg arguments that copy the streams, or None to encode as before.

    Our CPU renderer's output is always rewrapped, as it was; a wrong pixel
    format there is an error, not a reason to encode. A hosted output is
    copied only at the default export quality: a requested export_crf is
    still applied by the encode. `data` is the caller's probe of `video`.
    """
    if backend == CPU_RENDER.id:
        if pixel_format(video) != "yuv420p":
            raise BackendError("output_video_verification_failed")
    elif (
        backend != EVERYFRAME_RENDER.id
        or _request(job).get("export_crf", 18) != 18
        or not _exact(video, *_shape(job), data)
    ):
        lap("rewrap_declined")  # slot_turn_timing shows which path ran.
        return None
    lap("rewrap_accepted")
    args = ["-i", video, "-map", "0:v:0", "-c", "copy"]
    return args + (["-map", "0:a:0"] if audio else ["-an"])
