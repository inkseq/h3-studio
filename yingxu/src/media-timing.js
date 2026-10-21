import { translate } from './i18n/locale.js';
export function validateRange(start, end, duration, locale = 'zh') {
  if (
    start === '' ||
    end === '' ||
    !Number.isFinite(Number(start)) ||
    !Number.isFinite(Number(end))
  )
    return translate(locale, 'sound.range0');
  if (!Number.isFinite(duration) || duration <= 0) return translate(locale, 'sound.range1');
  if (Number(start) < 0) return translate(locale, 'sound.range2');
  if (Number(end) <= Number(start)) return translate(locale, 'sound.range3');
  if (Number(end) > duration + 0.001)
    return translate(locale, 'sound.maxRange', { seconds: duration.toFixed(2) });
  return '';
}
// Match CPU rendering: round each shot once, then accumulate integer 24fps frames.
export function chapterTiming(shots) {
  let cursor = 0;
  return shots.map((shot) => {
    const start = cursor / 24;
    cursor += Math.max(0, Math.floor((Number(shot.data.seconds) || 0) * 24 + 0.5));
    const rate = shot.data.selectedVideoRange?.playback_rate ?? 1;
    return { id: shot.id, title: shot.title, start, end: cursor / 24, shot, rate };
  });
}
export function trackPlacement(track, timeline) {
  const origin = track.shotId ? timeline.find((s) => s.id === track.shotId) : null;
  if (track.shotId && !origin) return null;
  const offset = Number(track.offset ?? 0),
    sourceStart = Number(track.start),
    sourceEnd = Number(track.end),
    start = (origin?.start || 0) + offset,
    // A clip's own sound plays at the clip's speed; detached and other tracks at normal speed.
    rate = origin && track.generatedFrom && !track.detached ? origin.rate || 1 : 1,
    length = (sourceEnd - sourceStart) / rate;
  if (
    !Number.isFinite(offset) ||
    offset < 0 ||
    !Number.isFinite(sourceStart) ||
    sourceStart < 0 ||
    !Number.isFinite(start) ||
    start < 0 ||
    !Number.isFinite(length) ||
    length <= 0
  )
    return null;
  return { start, end: start + length };
}
export function trackAtTime(track, timeline, time) {
  const placement = trackPlacement(track, timeline);
  if (!placement || track.muted || time < placement.start || time >= placement.end) return null;
  return Number(track.start) + time - placement.start;
}
export const secondsLabel = (value) =>
  `${Math.floor(Math.max(0, value) / 60)
    .toString()
    .padStart(2, '0')}:${(Math.max(0, value) % 60).toFixed(1).padStart(4, '0')}`;
