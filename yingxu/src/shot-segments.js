// Shot segments (h3-studio#512): several kept parts of one take, played in the person's
// order at the shot's place. Times are seconds in the source video; the server snaps them
// to the 24 fps grid and binds them to the chosen take.

export const SEGMENT_FPS = 24;

const finite = (value) => typeof value === 'number' && Number.isFinite(value);

// Only well-formed ranges; order and repeats are the person's intent and are kept.
export function shotSegments(value) {
  return Array.isArray(value)
    ? value
        .filter((r) => r && finite(r.start) && finite(r.end) && r.start >= 0 && r.end > r.start)
        .map((r) => ({ start: r.start, end: r.end }))
    : [];
}

export const segmentsTotal = (list) => list.reduce((sum, r) => sum + (r.end - r.start), 0);

// Snap like the server: start rounds up, end rounds down to whole frames.
export function snapRange(start, end) {
  const s = Math.ceil(start * SEGMENT_FPS - 1e-7) / SEGMENT_FPS,
    e = Math.floor(end * SEGMENT_FPS + 1e-7) / SEGMENT_FPS;
  return e > s ? { start: s, end: e } : null;
}

// Where in the source video a moment of the shot's playback falls.
export function sourceTime(list, local) {
  if (!list?.length) return local;
  let at = 0;
  for (const r of list) {
    const length = r.end - r.start;
    if (local < at + length) return r.start + Math.max(0, local - at);
    at += length;
  }
  return list.at(-1).end;
}

// A list edit for POST /actions: the whole ordered list replaces the old one ([] clears).
export const segmentsAction = (shotId, list) => ({
  op: 'shot.segments',
  shot_id: shotId,
  segments: list.map(({ start, end }) => ({ start, end })),
});

export const move = (list, from, to) => {
  const next = list.slice();
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
};

export function clock(seconds) {
  const m = Math.floor(seconds / 60),
    s = seconds - m * 60;
  return m + ':' + (s < 10 ? '0' : '') + s.toFixed(s % 1 ? 1 : 0);
}
