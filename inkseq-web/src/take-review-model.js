// Take review model (h3-studio#626): a whole-take verdict (keep | redo | null) and range
// marks on one take. Pure helpers so the panel and agents agree on the same rules.
export const TAGS = ['performance', 'camera', 'consistency', 'subtitles', 'quality', 'other'];
export const MAX_MARKS = 50;
export const MAX_NOTE = 500;
export const MIN_RANGE = 0.2;

const round = (value) => Math.round(value * 100) / 100;

// Clicking the active verdict again clears it; it is never set by picks, stars or marks.
export function nextVerdict(current, clicked) {
  return current === clicked ? null : clicked;
}

// A dragged range, ordered, clamped to the take and at least MIN_RANGE long.
export function rangeFrom(a, b, duration) {
  const total = Number.isFinite(duration) && duration > 0 ? duration : null;
  if (!total) return null;
  let start = Math.max(0, Math.min(a, b)),
    end = Math.min(total, Math.max(a, b));
  if (end - start < MIN_RANGE) {
    end = Math.min(total, start + MIN_RANGE);
    start = Math.max(0, end - MIN_RANGE);
  }
  return { start_s: round(start), end_s: round(end) };
}

// What to do with a marked stretch (inky-help review step): keep it, cut it in the edit,
// fix it in post, or reshoot. Stored as verdict + optional disposition (h3-studio TakeMark).
export const CHOICES = ['keep', 'cut', 'fix', 'reshoot'];
const DISPOSITIONS = ['cut', 'fix', 'reshoot'];

/** The choice a mark shows; an older change mark without a disposition reads 'change'. */
export function choiceOf(mark) {
  if (mark?.verdict === 'keep') return 'keep';
  return DISPOSITIONS.includes(mark?.disposition) ? mark.disposition : 'change';
}

export function withChoice(mark, choice) {
  const { disposition: _old, ...rest } = mark;
  if (choice === 'keep') return { ...rest, verdict: 'keep' };
  return DISPOSITIONS.includes(choice)
    ? { ...rest, verdict: 'change', disposition: choice }
    : { ...rest, verdict: 'change' };
}

/** The marks the film editor offers to trim: stretches the reviewer said to cut. */
export function cutRanges(marks) {
  return (marks || [])
    .filter((mark) => choiceOf(mark) === 'cut')
    .map((mark) => ({ start: mark.start_s, end: mark.end_s, id: mark.id }))
    .sort((a, b) => a.start - b.start);
}

/**
 * The parts of a take left after removing the stretches marked "cut", in time order; what
 * the shot plays once the reviewer's cuts are applied. Parts shorter than `min` are dropped.
 */
export function keptAfterCuts(duration, marks, min = 0.25) {
  if (!Number.isFinite(duration) || duration <= 0) return [];
  const kept = [];
  let at = 0;
  for (const cut of cutRanges(marks)) {
    if (cut.start - at >= min) kept.push({ start: at, end: Math.min(cut.start, duration) });
    at = Math.max(at, cut.end);
  }
  if (duration - at >= min) kept.push({ start: at, end: duration });
  return kept;
}

export function newMark(range, ids = new Set()) {
  let n = ids.size + 1;
  while (ids.has('mark-' + n)) n += 1;
  return {
    id: 'mark-' + n,
    ...range,
    tags: [],
    verdict: 'change',
    disposition: 'reshoot',
    note: '',
  };
}

// The exact body the server validates: known fields only, unique tags, bounded note.
export function cleanMark(mark, duration) {
  const range = rangeFrom(mark.start_s, mark.end_s, duration);
  if (!range) throw Error('review.durationUnknown');
  return {
    id: mark.id,
    ...range,
    tags: [...new Set(mark.tags)].filter((tag) => TAGS.includes(tag)),
    verdict: mark.verdict === 'keep' ? 'keep' : 'change',
    ...(mark.verdict !== 'keep' && DISPOSITIONS.includes(mark.disposition)
      ? { disposition: mark.disposition }
      : {}),
    note: String(mark.note || '').slice(0, MAX_NOTE),
  };
}

export function marksBody(marks, duration) {
  if (marks.length > MAX_MARKS) throw Error('review.tooMany');
  return marks
    .map((mark) => cleanMark(mark, duration))
    .sort((a, b) => a.start_s - b.start_s || a.id.localeCompare(b.id));
}

export const fmtSeconds = (value) => {
  const s = Math.max(0, value);
  return (
    Math.floor(s / 60) +
    ':' +
    String(Math.floor(s % 60)).padStart(2, '0') +
    '.' +
    Math.floor((s % 1) * 10)
  );
};
