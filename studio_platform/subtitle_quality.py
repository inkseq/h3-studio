"""Advisory subtitle evidence; never authorizes generation, payment or retries."""

import math
import hashlib
import re
import unicodedata

DETECTOR = 'dialogue-ocr-v1'
STATUSES = {'checked', 'no_dialogue', 'unavailable', 'timeout', 'failed', 'not_checked'}


def normalized(text):
    return ''.join(
        c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum()
    )


def dialogue_lines(prompt):
    if not isinstance(prompt, str) or len(prompt) > 200000:
        return []
    lines = []
    for value in re.findall(r'<d>(.*?)</d>', prompt, re.S | re.I)[:32]:
        value = re.sub(r'^\s*\[[^\]]{1,40}\]\s*', '', value)
        line = normalized(value)
        if 4 <= len(line) <= 1000:
            lines.append(line)
    return lines


def dialogue_digests(prompt):
    # Keep the receipt journal free of dialogue/prompt text across host restarts.
    return [
        hashlib.sha256(line.encode('utf-8')).hexdigest()
        for line in dialogue_lines(prompt)
    ]


def evidence(status, *, confidence=None, times=None):
    check = {'status': status, 'detector': DETECTOR}
    flags = []
    if confidence is not None:
        flags = ['burned_subtitles']
        check.update(confidence=round(confidence, 4), times_s=sorted(set(times)))
    return {'quality_flags': flags, 'quality_checks': {'burned_subtitles': check}}


def validate_quality(value, duration=None):
    if type(value) is not dict or set(value) != {'quality_flags', 'quality_checks'}:
        raise ValueError('subtitle_quality_invalid')
    checks = value['quality_checks']
    if type(checks) is not dict or set(checks) != {'burned_subtitles'}:
        raise ValueError('subtitle_quality_invalid')
    check = checks['burned_subtitles']
    if (
        type(check) is not dict
        or type(check.get('status')) is not str
        or check['status'] not in STATUSES
        or check.get('detector') != DETECTOR
    ):
        raise ValueError('subtitle_quality_invalid')
    flagged = value['quality_flags'] == ['burned_subtitles']
    if not flagged and value['quality_flags'] != []:
        raise ValueError('subtitle_quality_invalid')
    fields = {'status', 'detector'} | ({'confidence', 'times_s'} if flagged else set())
    if set(check) != fields or flagged and check['status'] != 'checked':
        raise ValueError('subtitle_quality_invalid')
    if flagged:
        confidence, times = check['confidence'], check['times_s']
        if (
            type(confidence) not in (float, int)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
            or type(times) is not list
            or not 3 <= len(times) <= 120
            or any(
                type(t) not in (float, int)
                or not math.isfinite(t)
                or t < 0
                or t > (duration if duration is not None else 60)
                for t in times
            )
            or sorted(set(times)) != times
            or times[-1] - times[0] < 1
        ):
            raise ValueError('subtitle_quality_invalid')
    return value


def public_quality(metadata):
    if 'quality_flags' not in metadata and 'quality_checks' not in metadata:
        return evidence('not_checked')
    return validate_quality(
        {k: metadata.get(k) for k in ('quality_flags', 'quality_checks')},
        metadata.get('duration_s'),
    )


def assess(samples, lines):
    """Require dialogue match, stable centred geometry, a hold, and background motion."""
    tracks = []
    for sample in samples:
        t = sample['time']
        for box, text, score in sample['texts']:
            text = normalized(text)
            if len(text) < 4 or score < 0.8:
                continue
            if hashlib.sha256(text.encode('utf-8')).hexdigest() not in lines:
                continue
            x0, y0, x1, y1 = box
            if not (
                0.3 <= (x0 + x1) / 2 <= 0.7
                and 0.15 <= x1 - x0 <= 0.95
                and 2 / 3 <= y0 < y1 <= 1
                and y1 - y0 <= 0.15
            ):
                continue
            track = next(
                (
                    v
                    for v in tracks
                    if 0 < t - v['times'][-1] <= 0.6
                    and v['text'] == text
                    and max(abs(a - b) for a, b in zip(v['box'], box)) < 0.035
                ),
                None,
            )
            if track is None:
                track = {
                    'text': text,
                    'box': box,
                    'times': [],
                    'scores': [],
                    'motion': [],
                }
                tracks.append(track)
            track['times'].append(t)
            track['scores'].append(score)
            track['motion'].append(sample['motion'])
    hits = [
        v
        for v in tracks
        if len(v['times']) >= 3
        and v['times'][-1] - v['times'][0] >= 1
        and max(v['motion'][1:], default=0) >= 0.015
    ]
    if not hits:
        return evidence('checked')
    return evidence(
        'checked',
        confidence=min(min(v['scores']) for v in hits),
        times=sorted({t for v in hits for t in v['times']}),
    )
