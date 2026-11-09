// Scenes as beats (inky-help "script" stage; novel-script's flow of action and line beats).
// A scene keeps `data.intent` (what the people want in this scene, shot-take-check 0b) and
// `data.beats[]`; a shot claims the beats it films in `data.plan.beats` (beat ids). Every beat
// should be claimed by exactly one shot of its scene. All of it is optional: a scene without
// beats keeps its free text in `data.script`.
import { inferSpeakers, promptParts } from './prompt-display.js';

const list = (value) => (Array.isArray(value) ? value : []);
const plain = (value) => !!value && typeof value === 'object' && !Array.isArray(value);
const str = (value, max) => typeof value === 'string' && value.length <= max;

export const MAX_BEATS = 200;
export const BEAT_TEXT_MAX = 2000;
// novel-script's defaults: about 4.5 characters a second for Chinese speech, 2.5 s per action
// beat. Speech in Latin letters is counted in words: about 2.7 a second (150-170 a minute).
export const CHARS_PER_SECOND = 4.5;
export const WORDS_PER_SECOND = 2.7;
export const ACTION_SECONDS = 2.5;
const WIDE = /[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uac00-\ud7af]/;
const WORD = /[A-Za-z0-9\u00c0-\u024f]+(?:['\u2019][A-Za-z]+)*/g;

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

/**
 * Estimated seconds. Action beats are fixed. Speech in Chinese or Japanese counts characters
 * (punctuation counts as a pause); words in Latin letters count as words, so an English line
 * is timed by words and a mixed line by both.
 */
export function beatSeconds(beat) {
  if (beat.kind === 'action') return ACTION_SECONDS;
  const text = beat.text || '';
  const words = text.match(WORD) || [];
  const chars = WIDE.test(text) ? text.replace(WORD, '').replace(/\s/g, '').length : 0;
  return Math.round((chars / CHARS_PER_SECOND + words.length / WORDS_PER_SECOND) * 10) / 10;
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

const blank = (kind, text, extra = {}) => ({
  id: newBeatId(),
  kind,
  text: text.slice(0, BEAT_TEXT_MAX),
  speaker: '',
  delivery: '',
  ...extra,
});

// Notes to the video model that are not part of the story ("无对白").
const NO_LINES =
  /[，,；;、]?\s*(?:无对白|无台词|没有对白|没有台词|no dialogue|no lines)(?=\s*(?:[。.！!，,；;]|$))/gi;
const LEAD = /^[\s，,、；;：:。.]+/;
const TRAIL = /[\s，,、；;：:]+$/;
// A short clause that ends in a speaking word reads as how the next line is said.
const SPEAKING =
  /(?:道|说|讲|喊|问|叫|吼|唤|呼|语|囔|喃|答|应|念|骂|嚷|咕哝|嘀咕|叹)$|\b(?:says?|said|asks?|asked|shouts?|shouted|whispers?|whispered|yells?|yelled|replies|replied|mutters?|muttered|calls?|called)$/i;
const DELIVERY_MAX = 40;
const CLAUSE_MARKS = [...'，,；;。.！!？?\n'];

const clean = (text) => text.replace(NO_LINES, '').replace(LEAD, '').replace(TRAIL, '');

/**
 * Text written the video model's way: the action between `(Sx) <d>…</d>` lines becomes action
 * beats and each line a line beat. The clause right before a line, when it ends in a speaking
 * word ("老大粗暴瞪牛喊道"), becomes that line's delivery instead of an action.
 * `speakers` maps speaker numbers to character ids.
 */
export function splitTagged(text, speakers = new Map()) {
  const beats = [];
  let pending = '';
  const flush = () => {
    const action = clean(pending);
    pending = '';
    if (/[^\s\p{P}]/u.test(action)) beats.push(blank('action', action));
  };
  for (const part of promptParts(text)) {
    if (part.kind === 'text') {
      pending += part.text;
      continue;
    }
    const lead = pending.replace(TRAIL, '');
    const cut = Math.max(...CLAUSE_MARKS.map((mark) => lead.lastIndexOf(mark)));
    const clause = lead.slice(cut + 1).trim();
    let delivery = '';
    if (clause && [...clause].length <= DELIVERY_MAX && SPEAKING.test(clause)) {
      delivery = clause;
      pending = lead.slice(0, cut + 1);
    }
    flush();
    const said = part.text.trim();
    if (said)
      beats.push(
        blank('line', said, { speaker: speakers.get(part.code) || '', delivery: delivery }),
      );
  }
  flush();
  return beats;
}

/** Speaker numbers → character ids: `bible.speaker` first, then what the texts say. */
export function speakerIds(texts, characters) {
  const ids = inferSpeakers(texts, characters);
  for (const c of characters) if (c.bible?.speaker) ids.set(c.bible.speaker.toUpperCase(), c.id);
  return ids;
}

/**
 * Turn a scene's free text into beats. Rules only (as LumenX does): "名字：台词（语气）" and
 * "Name: line" become line beats when the name belongs to a character; a line with
 * `(S1) <d>…</d>` is split by splitTagged; every other non-empty line is an action beat.
 */
export function parseScript(text, characters, speakers = speakerIds([text], characters)) {
  const byName = new Map();
  for (const c of characters) if (c.name) byName.set(c.name.trim(), c.id);
  const beats = [];
  for (const raw of String(text || '').split(/\r?\n/)) {
    if (beats.length >= MAX_BEATS) break;
    const line = raw.trim();
    if (!line) continue;
    if (/<d>/i.test(line)) {
      beats.push(...splitTagged(line, speakers));
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
      beats.push(
        blank('line', said.replace(/^[“"「『]|[”"」』]$/g, ''), {
          speaker,
          delivery: delivery.slice(0, 600),
        }),
      );
      continue;
    }
    const action = line.replace(NO_LINES, '');
    if (/[^\s\p{P}]/u.test(action)) beats.push(blank('action', action));
  }
  return beats.slice(0, MAX_BEATS);
}

/**
 * A scene whose script lives in its shots' text (stories written in chat): its beats in shot
 * order, and the shot each beat came from, so every beat can be claimed by its own shot.
 * `shots` are [{ id, prompt }] in scene order. Returns { beats, claims: [[beatId, shotId]] }.
 */
export function beatsFromShots(shots, characters) {
  const texts = shots.map((shot) => shot.prompt || '');
  const speakers = speakerIds(texts, characters);
  const beats = [],
    claims = [];
  shots.forEach((shot, index) => {
    for (const beat of parseScript(texts[index], characters, speakers)) {
      if (beats.length >= MAX_BEATS) return;
      beats.push(beat);
      claims.push([beat.id, shot.id]);
    }
  });
  return { beats, claims };
}

/** Why a scene's own text cannot be split into beats: '' (it can), 'inShots' or 'empty'. */
export function splitBlocked(group) {
  if ((group.script || '').trim()) return '';
  return (group.shots || []).some((shot) => (shot.prompt || '').trim()) ? 'inShots' : 'empty';
}

/** The beats with one moved up (-1) or down (+1); null when it cannot move. */
export function moveBeat(beats, id, dir) {
  const from = beats.findIndex((b) => b.id === id),
    to = from + dir;
  if (from < 0 || ![-1, 1].includes(dir) || to < 0 || to >= beats.length) return null;
  const next = [...beats];
  [next[from], next[to]] = [next[to], next[from]];
  return next;
}

/** A new empty beat of `kind` after `id` (at the end without one): { beats, beat } or null. */
export function insertBeat(beats, id, kind) {
  if (beats.length >= MAX_BEATS) return null;
  const beat = blank(kind === 'line' ? 'line' : 'action', '');
  const at = id ? beats.findIndex((b) => b.id === id) : -1;
  const next = [...beats];
  next.splice(at < 0 ? beats.length : at + 1, 0, beat);
  return { beats: next, beat };
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
