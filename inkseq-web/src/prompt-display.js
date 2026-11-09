// The video model's way of writing a spoken line, e.g. `(S1) <d>[Chinese] 今天吃牛。</d>`, shown
// as "名字：台词" wherever a shot's text is only read (audit 2026-11-09, Script P0). The stored
// text keeps the model's form: it is what the person edits and what generation sends.

// An optional speaker number, then the line with an optional language tag.
const LINE = /(?:\((S[1-9]\d?)\)\s*)?<d>\s*(?:\[[^\]\r\n]{0,40}\]\s*)?([\s\S]*?)\s*<\/d>/gi;
const CJK = /[\u3000-\u303f\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uff00-\uffef\uac00-\ud7af]/;
// Where a sentence ends: a line after one of these stands on its own.
const SENTENCE_END = /(^|[\n。！？!?；;])\s*$/;
const ENDS_IN_COLON = /[：:]\s*$/;

/**
 * The text cut into plain text and spoken lines, in order:
 * [{ kind: 'text', text }, { kind: 'line', code: 'S1' | '', text }].
 */
export function promptParts(text) {
  const parts = [],
    source = String(text ?? '');
  let at = 0;
  for (const match of source.matchAll(LINE)) {
    if (match.index > at) parts.push({ kind: 'text', text: source.slice(at, match.index) });
    parts.push({ kind: 'line', code: (match[1] || '').toUpperCase(), text: match[2] });
    at = match.index + match[0].length;
  }
  if (at < source.length) parts.push({ kind: 'text', text: source.slice(at) });
  return parts;
}

export const hasModelLines = (text) => /<d>/i.test(String(text ?? ''));

/**
 * Speaker number → character name, from each character's `bible.speaker` only: a name shown
 * beside a line must be one the person set, never a guess.
 */
export function speakerNames(profiles = []) {
  const names = new Map();
  for (const p of profiles)
    if (p.type === 'character' && p.bible?.speaker && p.name)
      names.set(p.bible.speaker.toUpperCase(), p.name);
  return names;
}

// The last sentence before a line: where its speaker is usually named.
export function sentenceBefore(text) {
  const parts = String(text).split(/[\n。！？!?；;]/);
  return parts[parts.length - 1] || '';
}

// The names a character goes by: "儿子（三兄弟）" is also "儿子" and "三兄弟".
export function characterAliases(name) {
  const full = String(name || '').trim();
  const out = new Set([full]);
  const inner = full.match(/^(.*?)\s*[（(]([^）)]+)[）)]\s*$/);
  if (inner) {
    out.add(inner[1].trim());
    for (const piece of inner[2].split(/[、,，/]/)) out.add(piece.trim());
  }
  return [...out].filter((alias) => [...alias].length >= 2);
}

/**
 * Speaker numbers that the story's own words tie to one character: every sentence that leads
 * into an `(Sx)` line names the same single character (by name or alias). Numbers with no
 * named sentence, or with sentences naming different characters, stay unknown.
 */
export function inferSpeakers(texts, characters) {
  const aliases = characters.map((c) => ({ id: c.id, names: characterAliases(c.name) }));
  const seen = new Map();
  for (const text of texts) {
    let before = '';
    for (const part of promptParts(text)) {
      if (part.kind === 'text') {
        before += part.text;
        continue;
      }
      const sentence = sentenceBefore(before);
      before = '';
      if (!part.code) continue;
      const named = aliases.filter((c) => c.names.some((n) => sentence.includes(n)));
      if (named.length !== 1) continue;
      const ids = seen.get(part.code) || new Set();
      ids.add(named[0].id);
      seen.set(part.code, ids);
    }
  }
  const out = new Map();
  for (const [code, ids] of seen) if (ids.size === 1) out.set(code, [...ids][0]);
  return out;
}

/**
 * The text as a person reads it: a line that stands on its own becomes "名字：台词"; a line
 * inside a sentence becomes `说道：“台词”`, with the speaker's name added only when the
 * sentence does not already name them. Unknown speakers never show their number.
 */
export function readablePrompt(text, names = new Map()) {
  let out = '';
  for (const part of promptParts(text)) {
    if (part.kind === 'text') {
      out += part.text;
      continue;
    }
    const said = part.text.trim();
    const wide = CJK.test(said) || CJK.test(out.slice(-12));
    const colon = wide ? '：' : ': ';
    const quoted = `“${said}”`;
    const name = names.get(part.code) || '';
    if (SENTENCE_END.test(out)) {
      out += name ? name + colon + said : quoted;
      continue;
    }
    if (ENDS_IN_COLON.test(out)) {
      out += quoted;
      continue;
    }
    const sentence = sentenceBefore(out);
    const known = name && characterAliases(name).some((n) => sentence.includes(n));
    out = out.replace(/\s+$/, '');
    if (name && !known) out += wide ? `（${name}）` : ` (${name})`;
    out += colon + quoted;
  }
  return out;
}
