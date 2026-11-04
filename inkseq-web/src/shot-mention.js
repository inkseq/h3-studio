// Pure rules for `@` in a shot's prompt (h3-studio#1040).
const MAX_OPTIONS = 6;

// The `@word` being typed at the caret, if any. An email-like `a@b` is not a mention.
export function mentionAt(text, caret) {
  const before = String(text ?? '').slice(0, caret),
    found = /(^|\s)@([^\s@]*)$/u.exec(before);
  if (!found) return null;
  const query = found[2];
  return { start: caret - query.length - 1, end: caret, query };
}

export function mentionOptions(profiles, query) {
  const wanted = String(query ?? '').toLowerCase();
  return (Array.isArray(profiles) ? profiles : [])
    .filter((profile) => profile?.name?.toLowerCase().includes(wanted))
    .slice(0, MAX_OPTIONS);
}

export function withMention(text, mention, name) {
  const value = String(text ?? ''),
    inserted = name + ' ';
  return {
    text: value.slice(0, mention.start) + inserted + value.slice(mention.end),
    caret: mention.start + inserted.length,
  };
}

export const CARD_SIZES = Object.freeze(['s', 'm', 'l']);
const SIZE_KEY = 'inkseq.shotCardSize';

export function readCardSize(storage) {
  try {
    const value = storage?.getItem(SIZE_KEY);
    return CARD_SIZES.includes(value) ? value : 'm';
  } catch {
    return 'm';
  }
}

export function rememberCardSize(storage, value) {
  try {
    storage?.setItem(SIZE_KEY, value);
  } catch {
    // Blocked storage only means the size is not remembered.
  }
}
