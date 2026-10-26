"""Pure integer timing for the opt-in v5 editing fields."""

from fractions import Fraction
import math

from .render_limits import MAX_AUDIO_TRACKS
from .shot_segments import has_segments, segment_frames

RATES = (0.5, 0.75, 1, 1.25, 1.5, 2)
CROSSFADES = (0, 0.25, 0.5, 1)
TRACK_FIELDS = {"detached", "slow_rate_audio_enabled", "fade_in_s", "fade_out_s"}


def number(value, minimum, maximum):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and minimum <= value <= maximum
    )


def frame_sample(frame):
    return (frame * 32000 + 12) // 24


def sample(value):
    return math.floor(value * 32000 + 0.5)


def has_speed(shot):
    selected = shot.get("data", {}).get("selectedVideoRange")
    return isinstance(selected, dict) and "playback_rate" in selected


def enabled(project, chapter_id, shots):
    journey = project.get("journey", {})
    cut = journey.get("filmCuts", {}).get(chapter_id, {})
    # The same selector also omits audio anchored to excluded shots.
    from .render_plans import chapter_tracks

    tracks = chapter_tracks(project, chapter_id)
    return (
        any(has_speed(shot) or has_segments(shot) for shot in shots)
        or isinstance(cut, dict)
        and "clip_sound_crossfade_s" in cut
        or isinstance(tracks, list)
        and any(
            isinstance(track, dict) and TRACK_FIELDS.intersection(track)
            for track in tracks
        )
    )


def shot_frames(shot):
    """Source/output frames; legacy duration is untouched without explicit rate."""
    data = shot.get("data", {})
    if has_segments(shot):
        clips = segment_timing(shot)
        return (
            sum(clip["frames"] for clip in clips),
            sum(clip["output_frames"] for clip in clips),
            clips[0]["playback_rate"],
        )
    if not has_speed(shot):
        seconds = data.get("seconds")
        if not number(seconds, 1 / 24, 600):
            raise ValueError("镜头时长无效")
        frames = max(1, math.floor(seconds * 24 + 0.5))
        return frames, frames, 1
    selected = data["selectedVideoRange"]
    rate, start, end = (
        selected.get("playback_rate"),
        selected.get("start"),
        selected.get("end"),
    )
    if (
        type(rate) not in (int, float)
        or rate not in RATES
        or not number(start, 0, 3600)
        or not number(end, 0, 3600)
        or end <= start
    ):
        raise ValueError("播放速度或视频选段无效")
    first, last = math.ceil(start * 24 - 1e-7), math.floor(end * 24 + 1e-7)
    source_frames = last - first
    ratio = Fraction(str(rate))
    frames = (2 * source_frames * ratio.denominator + ratio.numerator) // (
        2 * ratio.numerator
    )
    if not 1 <= source_frames <= 86400 or not 1 <= frames <= 14400:
        raise ValueError("播放速度后的镜头帧数超限")
    return source_frames, frames, rate


def segment_timing(shot, entity=None, source_duration=None):
    """Round each edit independently, as the worker renders each clip separately."""
    clips = segment_frames(shot, entity, source_duration)
    rate = 1
    if has_speed(shot):
        selected = shot["data"]["selectedVideoRange"]
        rate = selected["playback_rate"]
        if type(rate) not in (int, float) or rate not in RATES:
            raise ValueError("播放速度无效")
        if any(
            selected.get(key) != value
            for key, value in shot["data"]["videoSegmentsSource"].items()
        ):
            raise ValueError("video_segments_speed_source_changed")
    ratio = Fraction(str(rate))
    for clip in clips:
        frames = (2 * clip["frames"] * ratio.denominator + ratio.numerator) // (
            2 * ratio.numerator
        )
        if not 1 <= clip["frames"] <= 86400 or not 1 <= frames <= 14400:
            raise ValueError("播放速度后的镜头帧数超限")
        clip.update(output_frames=frames, playback_rate=rate)
    return clips


def expanded_audio_count(project, chapter_id, shots):
    """Admission bound for segment edits, before Film can quote missing shots.

    Followed original sound expands with its shot; absolute or manually anchored
    sound remains a single track. The compiler still proves ownership and samples.
    """
    from .render_plans import chapter_tracks

    if project.get("journey", {}).get("sound", {}).get("mode") == "silent":
        return 0
    tracks = chapter_tracks(project, chapter_id)
    if not isinstance(tracks, list) or len(tracks) > MAX_AUDIO_TRACKS:
        raise ValueError("film_audio_track_count_limit")
    lookup, count = {shot["id"]: shot for shot in shots}, 0
    for track in tracks:
        if not isinstance(track, dict):
            raise ValueError("film_audio_track_invalid")
        if track.get("muted") is True:
            continue
        shot_id = track.get("shotId")
        shot = lookup.get(shot_id) if isinstance(shot_id, str) else None
        if (
            track.get("generatedFrom") is not None
            and not track.get("detached", False)
            and shot is not None
        ):
            if shot_frames(shot)[2] < 0.75 and not track.get(
                "slow_rate_audio_enabled", False
            ):
                continue
            count += len(segment_timing(shot)) if has_segments(shot) else 1
        else:
            count += 1
    return count


def crossfade(project, chapter_id):
    value = (
        project.get("journey", {})
        .get("filmCuts", {})
        .get(chapter_id, {})
        .get("clip_sound_crossfade_s", 0.5)
    )
    if type(value) not in (int, float) or value not in CROSSFADES:
        raise ValueError("原声边界淡化时长无效")
    return value


def validate_track_fields(track):
    for key in ("detached", "slow_rate_audio_enabled"):
        if key in track and type(track[key]) is not bool:
            raise ValueError("声音分离或慢速原声开关无效")
    for key in ("fade_in_s", "fade_out_s"):
        if key in track and not number(track[key], 0, 5):
            raise ValueError("声音淡入淡出须为0至5秒")
    if track.get("detached") is True and track.get("generatedFrom") is None:
        raise ValueError("只有已验证的生成原声可以分离")
