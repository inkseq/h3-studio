"""Pure projection of pinned WanGP 0e58385 H3 default PW scheduling.

No new scheduler: the worker still executes WanGP's default window plan. Tests
compare this projection with that image offline. Slash commands are excluded.
"""

import math

from .inference.wangp_full_compiler import window_frames


def _grid(value, *, ceil=False):
    value = max(107, value)
    low = max(107, (value - 5) // 17 * 17 + 5)
    high = math.ceil((value - 5) / 17) * 17 + 5
    return high if ceil or high - value < value - low else low


def schedule(total_frames, seconds, first_frame=False):
    if type(seconds) is not int or not 5 <= seconds <= 10:
        raise ValueError("script_windows_unsupported_length")
    if type(total_frames) is not int or not 192 <= total_frames <= 362:
        raise ValueError("script_windows_unsupported_duration")
    size = window_frames(seconds)
    target = total_frames - int(first_frame)
    requested = actual = native_actual = 0
    windows = []
    while requested < target:
        preferred = int(first_frame) if not windows else 18
        available = int(first_frame) if not windows else native_actual
        contribution = min(target - requested, size - preferred)
        limit = min(max(preferred, 120), available)
        limit = 0 if limit < 1 else (limit - 1) // 17 * 17 + 1
        overlaps = range(preferred, limit + 1, 17) if preferred else [0]
        candidates = []
        for overlap in overlaps:
            frames = _grid(contribution + overlap)
            if frames <= overlap:
                frames = _grid(overlap + 1, ceil=True)
            output = frames - overlap
            candidates.append(
                (
                    (abs(output - contribution), abs(overlap - preferred), frames),
                    frames,
                    overlap,
                    output,
                )
            )
        _, frames, overlap, native_output = min(candidates)
        # wgp.py keeps the complete first sample for a single image prefix;
        # later samples lose their overlap. Scheduler contributions exclude
        # that one shared frame, but the exported MP4 includes it.
        output = native_output + (int(first_frame) if not windows else 0)
        windows.append(
            {
                "index": len(windows),
                "start_frame": actual,
                "end_frame": actual + output,
                "output_frames": output,
                "native_contribution_frames": native_output,
                "generation_frames": frames,
                "overlap_frames": overlap,
                "start_seconds": actual / 24,
                "end_seconds": (actual + output) / 24,
            }
        )
        requested += contribution
        actual += output
        native_actual += native_output
    if len(windows) < 2:
        raise ValueError("script_windows_single_window")
    return {
        "engine_revision": "0e58385",
        "fps": 24,
        "requested_frames": total_frames,
        "output_frames": actual,
        "window_frames": size,
        "initial_shared_frames": int(first_frame),
        "windows": windows,
    }
