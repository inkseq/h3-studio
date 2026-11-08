// A shot's plan, acting and continuity (inky-help storyboard/shot/generate stages; the
// shot-take-check rules named below). Stored on the shot's `data.plan` beside the beats it
// claims (script-beats.js). Every field is optional.

const list = (value) => (Array.isArray(value) ? value : []);
const plain = (value) => !!value && typeof value === 'object' && !Array.isArray(value);
const str = (value, max) => typeof value === 'string' && value.length <= max;

// novel-storyboard's shot sizes.
export const SIZES = ['extreme-wide', 'wide', 'medium', 'close', 'extreme-close'];
export const CONTINUITY = ['fresh', 'continue', 'master'];
const TEXT = [
  'purpose',
  'lens',
  'angle',
  'eyeline',
  'recipe',
  'entry',
  'exit',
  'emotionFrom',
  'emotionTo',
];
const MAX_ROWS = 12;

/** The plan of one shot entity with defaults. */
export function planOf(shot) {
  const plan = plain(shot?.data?.plan) ? shot.data.plan : {};
  return {
    beats: list(plan.beats).filter((id) => typeof id === 'string'),
    ...Object.fromEntries(TEXT.map((key) => [key, typeof plan[key] === 'string' ? plan[key] : ''])),
    size: SIZES.includes(plan.size) ? plan.size : '',
    continuity: CONTINUITY.includes(plan.continuity) ? plan.continuity : '',
    timeline: list(plan.timeline)
      .filter((row) => plain(row) && typeof row.text === 'string')
      .map((row) => ({
        from: Number.isFinite(row.from) ? row.from : null,
        to: Number.isFinite(row.to) ? row.to : null,
        text: row.text,
      })),
    // Characters in frame who must keep their mouths shut (0f).
    silent: list(plan.silent).filter((id) => typeof id === 'string'),
  };
}

export function validPlanPatch(project, shot, patch) {
  if (shot?.type !== 'shot' || !plain(patch) || !Object.keys(patch).length) return false;
  for (const [key, value] of Object.entries(patch)) {
    if (TEXT.includes(key)) {
      if (!str(value, key === 'entry' || key === 'exit' || key === 'purpose' ? 600 : 160))
        return false;
    } else if (key === 'size') {
      if (value !== '' && !SIZES.includes(value)) return false;
    } else if (key === 'continuity') {
      if (value !== '' && !CONTINUITY.includes(value)) return false;
    } else if (key === 'timeline') {
      if (!Array.isArray(value) || value.length > MAX_ROWS) return false;
      for (const row of value) {
        if (!plain(row) || !str(row.text, 600)) return false;
        for (const edge of ['from', 'to'])
          if (
            row[edge] !== null &&
            (!Number.isFinite(row[edge]) || row[edge] < 0 || row[edge] > 600)
          )
            return false;
        if (Object.keys(row).some((k) => !['from', 'to', 'text'].includes(k))) return false;
      }
    } else if (key === 'silent') {
      if (!Array.isArray(value) || value.length > 20) return false;
      const characters = new Set(
        project.entities.filter((e) => e.type === 'character').map((e) => e.id),
      );
      if (!value.every((id) => characters.has(id))) return false;
    } else return false;
  }
  return true;
}

export function applyPlanPatch(shot, patch) {
  shot.data.plan = { ...(plain(shot.data.plan) ? shot.data.plan : {}) };
  for (const [key, value] of Object.entries(patch))
    shot.data.plan[key] = Array.isArray(value)
      ? value.map((v) => (plain(v) ? { ...v } : v))
      : value;
}

/** The previous shot's exit, which this shot's entry should copy (14c). */
export function previousExit(shots, id) {
  const index = shots.findIndex((shot) => shot.id === id);
  return index > 0 ? planOf(shots[index - 1].entity ?? shots[index - 1]).exit : '';
}

/**
 * What the flow asks of a shot before it is generated, in order. Each item is
 * { key, rule, done, applies }. `speakers` are the character ids that speak in the beats
 * this shot claims; `characters` the story's character profile views.
 */
export function planChecklist(shotView, { speakers = [], characters = [] } = {}) {
  const plan = planOf(shotView.entity ?? shotView);
  const byId = new Map(characters.map((c) => [c.id, c]));
  const items = [
    { key: 'purpose', rule: '0a', done: !!plan.purpose.trim() },
    { key: 'beats', rule: 'script', done: plan.beats.length > 0 },
    { key: 'size', rule: '3', done: !!plan.size },
    { key: 'emotion', rule: '2', done: !!(plan.emotionFrom.trim() && plan.emotionTo.trim()) },
    { key: 'timeline', rule: '4', done: plan.timeline.some((row) => row.text.trim()) },
    { key: 'continuity', rule: '14c', done: !!(plan.entry.trim() && plan.exit.trim()) },
    {
      key: 'expressions',
      rule: '1',
      applies: speakers.length > 0,
      done: speakers.every((id) => {
        const e = byId.get(id)?.bible?.expressions;
        return !!e && Object.values(e).filter(Boolean).length >= 6;
      }),
    },
    { key: 'firstFrame', rule: '13', done: !!shotView.start },
  ];
  return items.filter((item) => item.applies !== false);
}
