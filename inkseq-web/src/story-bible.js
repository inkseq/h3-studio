// The story bible: structured fields on characters, places and props that the best-practice
// film flow asks for (inky-help stages cast/world; shot-take-check rules 0d, 1, 7, 10 and
// "one person, one voice"). All fields are optional and live in the entity's `data`, so
// older stories keep working. Field names follow the JSON that the novel-* and
// character-refs skills write, so a design Inky makes with those skills can be imported.

const list = (value) => (Array.isArray(value) ? value : []);
const plain = (value) => !!value && typeof value === 'object' && !Array.isArray(value);
const own = (value, key) => Object.hasOwn(value || {}, key);
const str = (value, max) => typeof value === 'string' && value.length <= max;
const count = (value) => Number.isInteger(value) && value >= 0 && value <= 12;

export const EXPRESSIONS = ['neutral', 'surprised', 'pain', 'worried', 'crying', 'wry'];
export const PROP_SCALES = ['hand', 'table', 'furniture'];
export const COMPASS = ['north', 'east', 'south', 'west', 'sun'];
export const PROFILE_FIELDS = ['identity', 'age', 'personality', 'motivation', 'arc'];
const SPEAKER = /^S([1-9]|1[0-9])$/;
const MAX_LINE = 600,
  MAX_ITEMS = 12;

/** The structured fields of one profile entity, with safe defaults. */
export function bibleOf(entity) {
  const data = entity?.data || {};
  const profile = plain(data.profile) ? data.profile : {};
  const body = plain(data.body) ? data.body : {};
  const expressions = plain(data.expressions) ? data.expressions : {};
  const compass = plain(data.compass) ? data.compass : {};
  return {
    profile: Object.fromEntries(
      PROFILE_FIELDS.map((key) => [key, typeof profile[key] === 'string' ? profile[key] : '']),
    ),
    anchors: list(data.anchors).filter((line) => typeof line === 'string'),
    body: {
      heads: count(body.heads) ? body.heads : null,
      arms: count(body.arms) ? body.arms : null,
      legs: count(body.legs) ? body.legs : null,
      line: typeof body.line === 'string' ? body.line : '',
    },
    expressions: Object.fromEntries(
      EXPRESSIONS.map((key) => [key, typeof expressions[key] === 'string' ? expressions[key] : '']),
    ),
    speaker: typeof data.speaker === 'string' ? data.speaker : '',
    groupId: typeof data.groupId === 'string' ? data.groupId : '',
    compass: Object.fromEntries(
      COMPASS.map((key) => [key, typeof compass[key] === 'string' ? compass[key] : '']),
    ),
    scale: PROP_SCALES.includes(data.scale) ? data.scale : '',
    states: list(data.states)
      .filter((state) => plain(state) && typeof state.name === 'string')
      .map((state) => ({
        name: state.name,
        imageId: str(state.imageId, 160) ? state.imageId : '',
      })),
    fixedLine: typeof data.fixedLine === 'string' ? data.fixedLine : '',
    carriedBy: typeof data.carriedBy === 'string' ? data.carriedBy : '',
  };
}

const ALLOWED = {
  character: ['profile', 'anchors', 'body', 'expressions', 'speaker', 'groupId'],
  location: ['anchors', 'compass'],
  prop: ['anchors', 'scale', 'states', 'fixedLine', 'carriedBy'],
};

const lines = (value) =>
  Array.isArray(value) && value.length <= MAX_ITEMS && value.every((line) => str(line, MAX_LINE));

/**
 * Whether a bible patch is valid for this profile. Image ids must name image assets in the
 * story; a speaker tag (S1…S19) belongs to one character only; a prop is carried by a
 * character in the story.
 */
export function validBiblePatch(project, profile, patch) {
  const allowed = ALLOWED[profile?.type];
  if (!allowed || !plain(patch) || !Object.keys(patch).length) return false;
  if (!Object.keys(patch).every((key) => allowed.includes(key))) return false;
  const image = (id) =>
    id === '' || project.entities.some((e) => e.id === id && e.type === 'image');
  if (own(patch, 'anchors') && !lines(patch.anchors)) return false;
  if (own(patch, 'profile')) {
    const value = patch.profile;
    if (!plain(value) || !Object.keys(value).every((key) => PROFILE_FIELDS.includes(key)))
      return false;
    if (!Object.values(value).every((v) => str(v, MAX_LINE))) return false;
  }
  if (own(patch, 'body')) {
    const value = patch.body;
    if (!plain(value)) return false;
    for (const key of Object.keys(value)) {
      if (key === 'line' ? !str(value.line, MAX_LINE) : !['heads', 'arms', 'legs'].includes(key))
        return false;
      if (key !== 'line' && value[key] !== null && !count(value[key])) return false;
    }
  }
  if (own(patch, 'expressions')) {
    const value = patch.expressions;
    if (!plain(value)) return false;
    if (!Object.keys(value).every((key) => EXPRESSIONS.includes(key) && image(value[key])))
      return false;
  }
  if (own(patch, 'speaker')) {
    if (patch.speaker !== '' && !SPEAKER.test(patch.speaker)) return false;
    const taken = project.entities.some(
      (e) =>
        e.type === 'character' &&
        e.id !== profile.id &&
        patch.speaker &&
        e.data?.speaker === patch.speaker,
    );
    if (taken) return false;
  }
  if (own(patch, 'groupId') && !str(patch.groupId, 80)) return false;
  if (own(patch, 'compass')) {
    const value = patch.compass;
    if (
      !plain(value) ||
      !Object.entries(value).every(([k, v]) => COMPASS.includes(k) && str(v, 120))
    )
      return false;
  }
  if (own(patch, 'scale') && patch.scale !== '' && !PROP_SCALES.includes(patch.scale)) return false;
  if (own(patch, 'states')) {
    const value = patch.states;
    if (!Array.isArray(value) || value.length > MAX_ITEMS) return false;
    if (!value.every((s) => plain(s) && str(s.name, 80) && image(s.imageId ?? ''))) return false;
  }
  if (own(patch, 'fixedLine') && !str(patch.fixedLine, MAX_LINE)) return false;
  if (
    own(patch, 'carriedBy') &&
    patch.carriedBy !== '' &&
    !project.entities.some((e) => e.id === patch.carriedBy && e.type === 'character')
  )
    return false;
  return true;
}

/** Merge a valid patch into the entity's data. Nested objects merge one level deep. */
export function applyBiblePatch(profile, patch) {
  for (const [key, value] of Object.entries(patch)) {
    if (['profile', 'body', 'expressions', 'compass'].includes(key))
      profile.data[key] = { ...(plain(profile.data[key]) ? profile.data[key] : {}), ...value };
    else
      profile.data[key] = Array.isArray(value)
        ? value.map((v) => (plain(v) ? { ...v } : v))
        : value;
  }
}

/**
 * What the best-practice flow expects of one profile (a storyboard profile view with
 * `bible`), in display order. Each item is { key, done, part? }. Characters: anchor
 * picture, body structure, expression sheet, look, voice, appearances. Places: picture,
 * anchors, light states, directions. Props: picture, scale, states, fixed line.
 */
export function profileChecklist(p, { used = false } = {}) {
  const b = p.bible || bibleOf(null);
  const pictures = p.variants.some((v) => v.images.length > 0);
  if (p.type === 'character') {
    const part = EXPRESSIONS.filter((key) => b.expressions[key]).length;
    return [
      { key: 'anchor', done: pictures },
      { key: 'body', done: !!(b.body.line.trim() || (b.body.heads && b.body.arms && b.body.legs)) },
      { key: 'expressions', done: part === EXPRESSIONS.length, part },
      { key: 'look', done: p.variants.some((v) => v.desc.trim()) },
      { key: 'voice', done: !!(p.voiceId || p.voice || p.voices?.length) },
      { key: 'used', done: used },
    ];
  }
  if (p.type === 'location')
    return [
      { key: 'picture', done: pictures },
      { key: 'anchors', done: b.anchors.filter((x) => x.trim()).length >= 3 },
      { key: 'lights', done: p.variants.length > 0 && p.variants.every((v) => v.desc.trim()) },
      { key: 'compass', done: COMPASS.filter((key) => b.compass[key].trim()).length >= 2 },
    ];
  return [
    { key: 'picture', done: pictures },
    { key: 'scale', done: !!b.scale },
    { key: 'states', done: b.states.length > 0 },
    { key: 'fixedLine', done: !!b.fixedLine.trim() },
  ];
}

/** Characters grouped for display: one entry per group (in first-member order) or single. */
export function characterGroups(profiles) {
  const out = [],
    seen = new Map();
  for (const p of profiles) {
    const group = p.bible?.groupId || '';
    if (!group) {
      out.push({ group: '', members: [p] });
      continue;
    }
    if (!seen.has(group)) {
      const entry = { group, members: [] };
      seen.set(group, entry);
      out.push(entry);
    }
    seen.get(group).members.push(p);
  }
  return out;
}

/** The next free speaker tag (S1…S19) among these profile views. */
export function nextSpeaker(profiles) {
  const used = new Set(profiles.map((p) => p.bible?.speaker).filter(Boolean));
  for (let n = 1; n < 20; n++) if (!used.has('S' + n)) return 'S' + n;
  return '';
}

const SCALE_FROM_ZH = { 手持级: 'hand', 桌面级: 'table', 家具级: 'furniture' };
const clip = (value, max) => (typeof value === 'string' ? value.slice(0, max) : '');

/**
 * Turn the JSON written by the novel-characters (cast.json) and novel-art (art.json) skills
 * into a plan: which profiles to create or fill, by name. Nothing is written here; the caller
 * shows the plan as candidates and applies the ones the person keeps.
 */
export function importPlan(profiles, json) {
  const byName = (type, name) => profiles.find((p) => p.type === type && p.name === name) || null;
  const items = [];
  for (const c of list(json?.characters)) {
    if (!c || typeof c.name !== 'string' || !c.name.trim()) continue;
    const persona = plain(c.persona) ? c.persona : {};
    const voice = plain(c.voice) ? c.voice : {};
    items.push({
      type: 'character',
      name: c.name.trim().slice(0, 160),
      existing: byName('character', c.name.trim())?.id || null,
      desc: clip(persona.appearance || c.oneLiner, 24000),
      patch: {
        profile: {
          identity: clip(persona.identity, MAX_LINE),
          age: clip(persona.ageRange, MAX_LINE),
          personality: clip(list(persona.personality).join('、'), MAX_LINE),
          motivation: clip(persona.motivation, MAX_LINE),
          arc: clip(persona.arc, MAX_LINE),
        },
      },
      voiceNote: [voice.timbre, voice.pitch, voice.pace, voice.accent].filter(Boolean).join('，'),
    });
  }
  for (const s of list(json?.scenes)) {
    if (!s || typeof s.name !== 'string' || !s.name.trim()) continue;
    items.push({
      type: 'location',
      name: s.name.trim().slice(0, 160),
      existing: byName('location', s.name.trim())?.id || null,
      desc: clip(s.summary, 24000),
      patch: {
        anchors: list(s.anchors)
          .map((a) => (plain(a) ? [a.name, a.desc].filter(Boolean).join('：') : clip(a, MAX_LINE)))
          .filter(Boolean)
          .map((line) => line.slice(0, MAX_LINE))
          .slice(0, MAX_ITEMS),
      },
      lights: list(s.lighting)
        .map((l) => (plain(l) ? clip(l.state || l.name, 160) : ''))
        .filter(Boolean),
    });
  }
  for (const p of list(json?.props)) {
    if (!p || typeof p.name !== 'string' || !p.name.trim()) continue;
    items.push({
      type: 'prop',
      name: p.name.trim().slice(0, 160),
      existing: byName('prop', p.name.trim())?.id || null,
      desc: clip(p.summary, 24000),
      patch: {
        anchors: list(p.anchors)
          .map((a) => (plain(a) ? [a.name, a.desc].filter(Boolean).join('：') : clip(a, MAX_LINE)))
          .filter(Boolean)
          .map((line) => line.slice(0, MAX_LINE))
          .slice(0, MAX_ITEMS),
        ...(SCALE_FROM_ZH[p.scale] || PROP_SCALES.includes(p.scale)
          ? { scale: SCALE_FROM_ZH[p.scale] || p.scale }
          : {}),
        states: list(p.states)
          .map((st) => (plain(st) ? clip(st.state || st.name, 80) : ''))
          .filter(Boolean)
          .slice(0, MAX_ITEMS)
          .map((name) => ({ name, imageId: '' })),
      },
    });
  }
  return items;
}

/** The bible as cast/art-shaped JSON (from storyboard profile views), for Inky's skills. */
export function exportBible(profiles) {
  const of = (type) => profiles.filter((p) => p.type === type);
  const nameOf = (id) => profiles.find((p) => p.id === id)?.name || '';
  return {
    format: 'inkseq.story-bible/1',
    characters: of('character').map((p) => ({
      name: p.name,
      speaker: p.bible.speaker,
      group: p.bible.groupId,
      persona: {
        identity: p.bible.profile.identity,
        ageRange: p.bible.profile.age,
        personality: p.bible.profile.personality
          ? p.bible.profile.personality
              .split(/[、,，]/)
              .map((x) => x.trim())
              .filter(Boolean)
          : [],
        motivation: p.bible.profile.motivation,
        arc: p.bible.profile.arc,
        appearance: p.desc,
      },
      anchors: p.bible.anchors,
      body: p.bible.body,
      looks: p.variants.map((v) => ({ name: v.name, desc: v.desc })),
    })),
    scenes: of('location').map((p) => ({
      name: p.name,
      summary: p.desc,
      anchors: p.bible.anchors,
      compass: p.bible.compass,
      lighting: p.variants.map((v) => ({ state: v.name, desc: v.desc })),
    })),
    props: of('prop').map((p) => ({
      name: p.name,
      summary: p.desc,
      scale: p.bible.scale,
      anchors: p.bible.anchors,
      states: p.bible.states.map((st) => ({ state: st.name })),
      fixedLine: p.bible.fixedLine,
      carriedBy: nameOf(p.bible.carriedBy),
    })),
  };
}
