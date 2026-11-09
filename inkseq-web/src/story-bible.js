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
    voiceNote: typeof data.voiceNote === 'string' ? data.voiceNote : '',
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
  character: ['profile', 'anchors', 'body', 'expressions', 'speaker', 'groupId', 'voiceNote'],
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
  if (own(patch, 'voiceNote') && !str(patch.voiceNote, MAX_LINE)) return false;
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

// ---------- One person, one voice (inky-help L20261108-02) ----------

export const MAX_SPLIT = 6;
const COUNT_WORDS = { 两: 2, 二: 2, 双: 2, 俩: 2, 三: 3, 仨: 3, 四: 4, 五: 5, 六: 6 };
const EN_WORDS = { two: 2, three: 3, four: 4, five: 5, six: 6 };
const countWord = (word) => COUNT_WORDS[word] || EN_WORDS[word.toLowerCase()] || Number(word) || 0;
// Several people in a name or group ("三兄弟", "two brothers", "twins").
const NAME_ZH = /([两二双俩三仨四五六2-6])\s*(?:个|位|名)?\s*(?:兄弟|姐妹|兄妹|姐弟|胞胎|人|头|身)/;
// Several heads or bodies in a description ("一个身体三个头", "three bodies").
const BODY_ZH = /([两二双三四五六2-6])\s*(?:个|颗)\s*(?:头|脑袋|身体)|([双三四五六])胞胎/;
const NAME_EN =
  /\b(two|three|four|five|six|[2-6])[\s-]+(?:heads?|headed|bod(?:y|ies)|bodied|people|persons|brothers|sisters|siblings)\b/i;
const BODY_EN = /\b(two|three|four|five|six|[2-6])[\s-]+(?:heads|headed|bodies|bodied)\b/i;
const PAIRS = [
  [/\btwins\b|\bconjoined\b/i, 2],
  [/\btriplets\b/i, 3],
];

function countIn(text, zh, en) {
  if (typeof text !== 'string' || !text) return 0;
  const hit = text.match(zh);
  if (hit) return countWord(hit[1] || hit[2]);
  const word = text.match(en);
  if (word) return countWord(word[1]);
  return PAIRS.find(([re]) => re.test(text))?.[1] || 0;
}

/**
 * How many people one character profile seems to hold: 0 for one person, else 2…6. Reads the
 * body counts, then the name or a group that has no other card yet, then the descriptions.
 * A member of a group that already has several cards is one person.
 */
export function peopleInProfile(p, profiles = []) {
  if (p?.type !== 'character') return 0;
  const b = p.bible || bibleOf(null);
  if (b.body.heads > 1) return Math.min(b.body.heads, MAX_SPLIT);
  const grouped =
    b.groupId &&
    profiles.some((o) => o.id !== p.id && o.type === 'character' && o.bible?.groupId === b.groupId);
  if (grouped) return 0;
  const named = [p.name, b.groupId].map((x) => countIn(x, NAME_ZH, NAME_EN)).find(Boolean);
  const described = [p.desc, ...list(p.variants).map((v) => v.desc), b.body.line]
    .map((x) => countIn(x, BODY_ZH, BODY_EN))
    .find(Boolean);
  const n = named || described || 0;
  return n > 1 ? Math.min(n, MAX_SPLIT) : 0;
}

const LINE_TAG = /\((S(?:1[0-9]|[1-9]))\)/g;

/**
 * Where this character may speak: line beats in the script that name them, shot text with
 * their speaker number, and lines in shots they are cast in whose number belongs to nobody
 * else in that shot. A count of places, not of words.
 */
export function speakingPlaces(p, { scenes = [], shots = [], profiles = [] } = {}) {
  if (p?.type !== 'character') return 0;
  let n = 0;
  for (const scene of scenes)
    for (const beat of list(scene?.data?.beats))
      if (plain(beat) && beat.kind === 'line' && beat.speaker === p.id) n++;
  const own = p.bible?.speaker || '';
  const speakerOf = new Map(profiles.map((o) => [o.id, o.bible?.speaker || '']));
  for (const shot of shots) {
    const text = [shot?.prompt, shot?.script].filter((x) => typeof x === 'string').join('\n');
    const tags = [...text.matchAll(LINE_TAG)].map((m) => m[1]);
    if (!tags.length) continue;
    if (own) {
      if (tags.includes(own)) n++;
      continue;
    }
    const cast = list(shot.cast).map((c) => c.id);
    if (!cast.includes(p.id)) continue;
    const claimed = new Set(cast.map((id) => speakerOf.get(id)).filter(Boolean));
    if (tags.some((tag) => !claimed.has(tag))) n++;
  }
  return n;
}

/** What a speaking character still lacks: 'speaker' (an Sx number) and/or 'voice'. */
export function voiceGaps(p, speaking) {
  if (p?.type !== 'character' || !speaking) return [];
  return [
    ...(p.bible?.speaker ? [] : ['speaker']),
    ...(p.voiceId || p.voice || p.voices?.length ? [] : ['voice']),
  ];
}

/** The name without a trailing note in brackets: "儿子（三兄弟）" -> "儿子". */
export const splitBase = (name) => name.replace(/\s*[（(][^（）()]*[）)]\s*$/, '').trim() || name;

/**
 * Storyboard actions (for one `batch`) that turn one profile holding several people into one
 * character per person in a group: each gets the shared description, look and body text and
 * the next free speaker number. The old profile is left alone; the caller moves it to the
 * trash afterwards. Returns null unless the names are 2…6 distinct non-empty names.
 */
export function splitActions(p, profiles, { names, lookName = '' } = {}) {
  if (p?.type !== 'character' || !Array.isArray(names)) return null;
  const clean = names.map((n) => (typeof n === 'string' ? n.trim().slice(0, 160) : ''));
  if (clean.length < 2 || clean.length > MAX_SPLIT || clean.some((n) => !n)) return null;
  if (new Set(clean).size !== clean.length) return null;
  const b = p.bible || bibleOf(null);
  const group = (b.groupId || splitBase(p.name)).slice(0, 80);
  const used = new Set(
    profiles
      .filter((o) => o.type === 'character')
      .map((o) => o.bible?.speaker)
      .filter(Boolean),
  );
  const look = list(p.variants).find((v) => v.id === p.def) || list(p.variants)[0];
  const each = (total) =>
    Number.isInteger(total) && total % clean.length === 0 ? total / clean.length : null;
  const anchors = b.anchors.filter((x) => x.trim());
  const actions = [];
  for (const name of clean) {
    actions.push({ type: 'newProfile', ptype: 'character', name, ...(lookName && { lookName }) });
    const ref = { $ref: actions.length - 1 };
    if (p.desc) actions.push({ type: 'profile', id: ref, patch: { desc: p.desc } });
    if (look?.desc)
      actions.push({ type: 'variant', pid: ref, vid: 'main', patch: { desc: look.desc } });
    let speaker = '';
    for (let n = 1; n < 20 && !speaker; n++) if (!used.has('S' + n)) speaker = 'S' + n;
    if (speaker) used.add(speaker);
    actions.push({
      type: 'bible',
      pid: ref,
      patch: {
        groupId: group,
        ...(speaker && { speaker }),
        ...(anchors.length && { anchors }),
        body: { heads: 1, arms: each(b.body.arms), legs: each(b.body.legs), line: b.body.line },
      },
    });
  }
  return actions;
}

// ---------- Import and export ----------

const SCALE_FROM_ZH = { 手持级: 'hand', 桌面级: 'table', 家具级: 'furniture' };
const clip = (value, max) => (typeof value === 'string' ? value.slice(0, max) : '');
const anchorLines = (value) =>
  list(value)
    .map((a) =>
      plain(a)
        ? [a.name, a.desc].filter((x) => typeof x === 'string' && x).join('：')
        : clip(a, MAX_LINE),
    )
    .filter(Boolean)
    .map((line) => line.slice(0, MAX_LINE))
    .slice(0, MAX_ITEMS);
const filled = (value) =>
  Array.isArray(value)
    ? value.length > 0
    : plain(value)
      ? Object.values(value).some((v) => v !== null && v !== '')
      : value !== null && value !== '' && value !== undefined;
// Drop empty fields, and empty keys inside the one-level objects.
function compact(patch) {
  const out = {};
  for (const [key, value] of Object.entries(patch)) {
    const v = plain(value)
      ? Object.fromEntries(Object.entries(value).filter(([, x]) => x !== null && x !== ''))
      : value;
    if (filled(v)) out[key] = v;
  }
  return out;
}
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// Which patch fields would change this existing profile view.
function changedKeys(current, patch) {
  const b = current.bible || bibleOf(null);
  return Object.keys(patch).filter((key) =>
    plain(patch[key])
      ? Object.entries(patch[key]).some(([k, v]) => !same(b[key]?.[k], v))
      : key === 'carriedBy' || !same(b[key], patch[key]),
  );
}

/**
 * Turn the JSON written by the novel-characters (cast.json) and novel-art (art.json) skills, or
 * by exportBible, into a plan: which profiles to create or fill, by name, and which fields each
 * one changes (`changes`). Nothing is written here. Picture references only count when the
 * picture is in this story (`images`, a Set of image ids); others are counted in `missing`.
 * A speaker number another card already has is left out (`skipped`).
 */
export function importPlan(profiles, json, { images = new Set() } = {}) {
  const byName = (type, name) => profiles.find((p) => p.type === type && p.name === name) || null;
  const picture = (value) => {
    const id = plain(value) ? value.id : value;
    return typeof id === 'string' && images.has(id) ? id : '';
  };
  const items = [];
  for (const c of list(json?.characters)) {
    if (!c || typeof c.name !== 'string' || !c.name.trim()) continue;
    const persona = plain(c.persona) ? c.persona : {};
    const voice = plain(c.voice) ? c.voice : {};
    const body = plain(c.body) ? c.body : {};
    const faces = plain(c.expressions) ? c.expressions : {};
    let missing = 0;
    const expressions = {};
    for (const key of EXPRESSIONS) {
      if (!faces[key]) continue;
      const id = picture(faces[key]);
      if (id) expressions[key] = id;
      else missing++;
    }
    const name = c.name.trim().slice(0, 160);
    items.push({
      type: 'character',
      name,
      existing: byName('character', name)?.id || null,
      desc: clip(persona.appearance || c.oneLiner, 24000),
      looks: list(c.looks)
        .filter((l) => plain(l) && typeof l.name === 'string' && l.name.trim())
        .slice(0, 20)
        .map((l) => ({ name: l.name.trim().slice(0, 160), desc: clip(l.desc, 24000) })),
      voiceId: str(voice.id, 40) ? voice.id : '',
      missing,
      patch: {
        profile: {
          identity: clip(persona.identity, MAX_LINE),
          age: clip(persona.ageRange, MAX_LINE),
          personality: clip(list(persona.personality).join('、'), MAX_LINE),
          motivation: clip(persona.motivation, MAX_LINE),
          arc: clip(persona.arc, MAX_LINE),
        },
        anchors: anchorLines(c.anchors),
        body: {
          heads: count(body.heads) ? body.heads : null,
          arms: count(body.arms) ? body.arms : null,
          legs: count(body.legs) ? body.legs : null,
          line: clip(body.line, MAX_LINE),
        },
        expressions,
        speaker: typeof c.speaker === 'string' && SPEAKER.test(c.speaker) ? c.speaker : '',
        groupId: clip(c.group, 80),
        voiceNote: clip(
          typeof voice.note === 'string' && voice.note
            ? voice.note
            : [voice.timbre, voice.pitch, voice.pace, voice.accent]
                .filter((x) => typeof x === 'string' && x)
                .join('，'),
          MAX_LINE,
        ),
      },
    });
  }
  for (const s of list(json?.scenes)) {
    if (!s || typeof s.name !== 'string' || !s.name.trim()) continue;
    const compass = plain(s.compass) ? s.compass : {};
    const name = s.name.trim().slice(0, 160);
    items.push({
      type: 'location',
      name,
      existing: byName('location', name)?.id || null,
      desc: clip(s.summary, 24000),
      lights: list(s.lighting)
        .map((l) =>
          plain(l) ? { name: clip(l.state || l.name, 160), desc: clip(l.desc, 24000) } : null,
        )
        .filter((l) => l?.name),
      missing: 0,
      patch: {
        anchors: anchorLines(s.anchors),
        compass: Object.fromEntries(COMPASS.map((key) => [key, clip(compass[key], 120)])),
      },
    });
  }
  for (const p of list(json?.props)) {
    if (!p || typeof p.name !== 'string' || !p.name.trim()) continue;
    let missing = 0;
    const states = list(p.states)
      .filter((st) => plain(st) && clip(st.state || st.name, 80))
      .slice(0, MAX_ITEMS)
      .map((st) => {
        const ref = st.image ?? st.imageId;
        const imageId = ref ? picture(ref) : '';
        if (ref && !imageId) missing++;
        return { name: clip(st.state || st.name, 80), imageId };
      });
    const name = p.name.trim().slice(0, 160);
    items.push({
      type: 'prop',
      name,
      existing: byName('prop', name)?.id || null,
      desc: clip(p.summary, 24000),
      missing,
      patch: {
        anchors: anchorLines(p.anchors),
        scale: SCALE_FROM_ZH[p.scale] || (PROP_SCALES.includes(p.scale) ? p.scale : ''),
        states,
        fixedLine: clip(p.fixedLine, MAX_LINE),
        carriedBy: clip(p.carriedBy, 160),
      },
    });
  }
  // A speaker number goes to one card only: one another card holds, or a repeat, is left out.
  const holders = new Map(
    profiles
      .filter((p) => p.type === 'character' && p.bible?.speaker)
      .map((p) => [p.bible.speaker, p.id]),
  );
  const seen = new Set();
  for (const item of items) {
    item.patch = compact(item.patch);
    item.skipped = [];
    const speaker = item.patch.speaker;
    if (speaker) {
      const holder = holders.get(speaker);
      if ((holder && holder !== item.existing) || seen.has(speaker)) {
        delete item.patch.speaker;
        item.skipped.push('speaker');
      } else seen.add(speaker);
    }
    const current = item.existing && profiles.find((p) => p.id === item.existing);
    if (current) {
      // An existing card is filled in: only fields that differ, never its looks or description.
      item.patch = Object.fromEntries(
        changedKeys(current, item.patch).map((key) => [key, item.patch[key]]),
      );
      if (item.voiceId === current.voiceId) item.voiceId = '';
      item.changes = Object.keys(item.patch);
    } else
      item.changes = [
        ...(item.desc ? ['desc'] : []),
        ...(item.looks?.length ? ['looks'] : []),
        ...(item.lights?.length ? ['lights'] : []),
        ...Object.keys(item.patch),
      ];
    if (item.voiceId) item.changes.push('voice');
    item.changes = [...new Set(item.changes.map((key) => (key === 'voiceNote' ? 'voice' : key)))];
  }
  return items;
}

/**
 * The storyboard actions (for one `batch`, so all or nothing is saved) that apply the kept
 * items of an import plan. New profiles are referred to by `{ $ref: index }` of the action
 * that makes them. A prop's carrier is matched by name to a character in the story or in
 * the import; an unknown carrier is left out.
 */
export function importActions(plan, keep, { profiles = [], lookName = '', placeName = '' } = {}) {
  const actions = [],
    made = new Map();
  const add = (action) => {
    actions.push(action);
    return { $ref: actions.length - 1 };
  };
  plan.forEach((item, index) => {
    if (!keep.has(index)) return;
    let pid = item.existing;
    if (!pid) {
      const first =
        item.type === 'character' ? lookName : item.type === 'location' ? placeName : '';
      pid = add({
        type: 'newProfile',
        ptype: item.type,
        name: item.name,
        ...(first && { lookName: first }),
      });
      if (item.type === 'character') made.set(item.name, pid);
      if (item.desc) add({ type: 'profile', id: pid, patch: { desc: item.desc } });
      const versions = (item.type === 'character' ? item.looks : item.lights) || [];
      versions.slice(0, 20).forEach((v, i) => {
        const vid = i === 0 ? 'main' : add({ type: 'addVariant', pid, name: v.name });
        const patch = { ...(i === 0 && { name: v.name }), ...(v.desc && { desc: v.desc }) };
        if (Object.keys(patch).length) add({ type: 'variant', pid, vid, patch });
      });
    }
    if (item.voiceId) add({ type: 'profile', id: pid, patch: { voiceId: item.voiceId } });
    const patch = { ...item.patch };
    if (patch.carriedBy) {
      const id =
        profiles.find((c) => c.type === 'character' && c.name === patch.carriedBy)?.id ||
        made.get(patch.carriedBy);
      if (id) patch.carriedBy = id;
      else delete patch.carriedBy;
    }
    if (Object.keys(patch).length) add({ type: 'bible', pid, patch });
  });
  return actions;
}

/**
 * The bible as cast/art-shaped JSON (from storyboard profile views), for Inky's skills and for
 * importing back. Pictures are references ({ id, name }) to files in the story.
 */
export function exportBible(profiles, { assetName = () => '' } = {}) {
  const of = (type) => profiles.filter((p) => p.type === type);
  const nameOf = (id) => profiles.find((p) => p.id === id)?.name || '';
  const picture = (id) => ({ id, name: assetName(id) || '' });
  return {
    format: 'inkseq.story-bible/1',
    characters: of('character').map((p) => ({
      name: p.name,
      speaker: p.bible.speaker,
      group: p.bible.groupId,
      voice: { id: p.voiceId || '', note: p.bible.voiceNote },
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
      expressions: Object.fromEntries(
        EXPRESSIONS.filter((key) => p.bible.expressions[key]).map((key) => [
          key,
          picture(p.bible.expressions[key]),
        ]),
      ),
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
      states: p.bible.states.map((st) => ({
        state: st.name,
        ...(st.imageId && { image: picture(st.imageId) }),
      })),
      fixedLine: p.bible.fixedLine,
      carriedBy: nameOf(p.bible.carriedBy),
    })),
  };
}
