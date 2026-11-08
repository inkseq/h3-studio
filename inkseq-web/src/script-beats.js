// Scenes as beats (inky-help "script" stage; novel-script's flow of action and line beats).
// A scene keeps `data.intent` (what the people want in this scene, shot-take-check 0b) and
// `data.beats[]`; a shot claims the beats it films in `data.plan.beats` (beat ids). Every beat
// should be claimed by exactly one shot of its scene. All of it is optional: a scene without
// beats keeps its free text in `data.script`.

const list = (value) => (Array.isArray(value) ? value : []);
const plain = (value) => !!value && typeof value === 'object' && !Array.isArray(value);
const str = (value, max) => typeof value === 'string' && value.length <= max;

export const MAX_BEATS = 200;
export const BEAT_TEXT_MAX = 2000;
// novel-script's defaults: about 4.5 characters a second for speech, 2.5 s per action beat.
export const CHARS_PER_SECOND = 4.5;
export const ACTION_SECONDS = 2.5;

const ID = /^beat-[A-Za-z0-9_-]{1,64}$/;
export const newBeatId = () => 'beat-' + crypto.randomUUID().slice(0, 12);

/** A scene's beats, cleaned: unknown kinds and broken rows are dropped. */
export function sceneBeats(scene) {
  return list(scene?.data?.beats)
    .filter((b) => plain(b) && ID.test(b.id) && ['action', 'line'].includes(b.kind))
    .map((b) => ({
      id: b.id,
      kind: b.kind,
      text: typeof b.text === 'string' ? b.text : '',
      speaker: b.kind === 'line' && typeof b.speaker === 'string' ? b.speaker : '',
      delivery: b.kind === 'line' && typeof b.delivery === 'string' ? b.delivery : '',
    }));
}

export function validBeats(project, beats) {
  if (!Array.isArray(beats) || beats.length > MAX_BEATS) return false;
  const ids = new Set();
  for (const b of beats) {
    if (!plain(b) || !ID.test(b.id) || ids.has(b.id) || !['action', 'line'].includes(b.kind))
      return false;
    ids.add(b.id);
    if (!str(b.text ?? '', BEAT_TEXT_MAX) || !str(b.delivery ?? '', 600)) return false;
    if (b.kind === 'action' && (b.speaker || b.delivery)) return false;
    if (
      b.kind === 'line' &&
      b.speaker &&
      !project.entities.some((e) => e.id === b.speaker && e.type === 'character')
    )
      return false;
    if (!Object.keys(b).every((k) => ['id', 'kind', 'text', 'speaker', 'delivery'].includes(k)))
      return false;
  }
  return true;
}

/** Estimated seconds: speech by characters (punctuation counts as pause), action fixed. */
export function beatSeconds(beat) {
  if (beat.kind === 'action') return ACTION_SECONDS;
  const chars = (beat.text || '').replace(/\s/g, '').length;
  return Math.round((chars / CHARS_PER_SECOND) * 10) / 10;
}
export const beatsSeconds = (beats) =>
  Math.round(beats.reduce((sum, b) => sum + beatSeconds(b), 0) * 10) / 10;

/** Which shots claim each beat of the scene: Map beatId -> [shotId]. */
export function beatClaims(shots, beats) {
  const claims = new Map(beats.map((b) => [b.id, []]));
  for (const shot of shots)
    for (const id of list(shot.entity?.data?.plan?.beats ?? shot.data?.plan?.beats))
      claims.get(id)?.push(shot.id);
  return claims;
}

/**
 * Turn a scene's free text into beats. Rules first (as LumenX does): "名字：台词（语气）",
 * "Name: line", "(S1) <d>…</d>" become line beats when the name or speaker number belongs to
 * a character; every other non-empty line is an action beat. Nothing is guessed beyond that.
 */
export function parseScript(text, characters) {
  const byName = new Map(),
    bySpeaker = new Map();
  for (const c of characters) {
    if (c.name) byName.set(c.name.trim(), c.id);
    if (c.bible?.speaker) bySpeaker.set(c.bible.speaker, c.id);
  }
  const beats = [];
  for (const raw of String(text || '').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const tagged = line.match(/^\((S\d{1,2})\)\s*<d>(?:\[[^\]]*\]\s*)?([\s\S]*?)<\/d>\s*$/);
    if (tagged && bySpeaker.has(tagged[1])) {
      beats.push({
        id: newBeatId(),
        kind: 'line',
        text: tagged[2].trim(),
        speaker: bySpeaker.get(tagged[1]),
        delivery: '',
      });
      continue;
    }
    const named = line.match(/^([^：:（(]{1,20})[：:]\s*(.+)$/);
    const speaker = named && byName.get(named[1].trim());
    if (speaker) {
      let said = named[2].trim(),
        delivery = '';
      const how =
        said.match(/^[（(]([^）)]{1,60})[）)]\s*(.+)$/) ||
        said.match(/^(.+?)\s*[（(]([^）)]{1,60})[）)]$/);
      if (how) {
        if (said.startsWith('（') || said.startsWith('(')) [delivery, said] = [how[1], how[2]];
        else [said, delivery] = [how[1], how[2]];
      }
      beats.push({
        id: newBeatId(),
        kind: 'line',
        text: said.replace(/^[“"「『]|[”"」』]$/g, '').slice(0, BEAT_TEXT_MAX),
        speaker,
        delivery: delivery.slice(0, 600),
      });
      continue;
    }
    beats.push({
      id: newBeatId(),
      kind: 'action',
      text: line.slice(0, BEAT_TEXT_MAX),
      speaker: '',
      delivery: '',
    });
    if (beats.length >= MAX_BEATS) break;
  }
  return beats.slice(0, MAX_BEATS);
}

/** The beats as plain script text, the reverse of parseScript (for the free-text view). */
export function beatsText(beats, characters) {
  const name = new Map(characters.map((c) => [c.id, c.name]));
  return beats
    .map((b) =>
      b.kind === 'line'
        ? `${name.get(b.speaker) || '?'}：${b.delivery ? `（${b.delivery}）` : ''}${b.text}`
        : b.text,
    )
    .join('\n');
}
