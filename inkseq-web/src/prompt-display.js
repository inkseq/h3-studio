// The video model's way of writing a spoken line, e.g. `(S1) <d>[Chinese] 今天吃牛。</d>`, cut
// into parts, and which character each speaker number stands for. Editors show the lines
// without tags through speech-edit.js (h3-studio#1113).

// An optional speaker number, then the line with an optional language tag.
const LINE = /(?:\((S[1-9]\d?)\)\s*)?<d>\s*(?:\[[^\]\r\n]{0,40}\]\s*)?([\s\S]*?)\s*<\/d>/gi;

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
