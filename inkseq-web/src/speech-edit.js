// Editing a shot's text without its speech tags (h3-studio#1113). The video model's spoken line
// `(S1) <d>[Chinese] 今天吃牛。</d>` is edited as 「今天吃牛。」 (“Hi.” for other languages): the
// box shows only the words, and saving puts the same tag back around them.
//
// As in react-mentions, the box's text is never rebuilt while someone types: each change is
// read as one replaced range of the shown text, and the lines' places move with it. So the
// caret never jumps, a quote typed in the story text never turns into a tag, and the tags a
// person cannot see are never lost except by deleting a line's own quote marks, which turns
// that line into plain text.

// An optional speaker number, then the line with an optional language tag. The words are the
// middle group; everything around them is kept exactly as stored.
const LINE = /(?:\(S[1-9]\d?\)\s*)?<d>\s*(?:\[[^\]\r\n]{0,40}\]\s*)?([\s\S]*?)\s*<\/d>/dgi;
const wide = (text) => [...text].some((ch) => ch.codePointAt(0) >= 0x3000);
const CJK = /[぀-ヿ㐀-鿿豈-﫿가-힯]/;

/** The stored text as parts: [{ text }] for story text, [{ line, head, tail, open, close }]. */
export function speechParts(stored) {
  const source = String(stored ?? ''),
    parts = [];
  let at = 0;
  for (const match of source.matchAll(LINE)) {
    const [start, end] = match.indices[1];
    if (match.index > at) parts.push({ text: source.slice(at, match.index) });
    const quote = wide(match[1]) ? ['「', '」'] : ['“', '”'];
    parts.push({
      line: match[1],
      head: source.slice(match.index, start),
      tail: source.slice(end, match.index + match[0].length),
      open: quote[0],
      close: quote[1],
    });
    at = match.index + match[0].length;
  }
  if (at < source.length) parts.push({ text: source.slice(at) });
  return parts;
}

/** What the box shows. */
export const speechShown = (parts) =>
  parts.map((p) => ('line' in p ? p.open + p.line + p.close : p.text)).join('');

// A line's language tag follows its words when they change script: Chinese words in an
// [English] line, or the other way round, would be read in the wrong voice.
function retag(head, line) {
  if (CJK.test(line)) return head.replace(/\[English\]/i, '[Chinese]');
  if (/[A-Za-z]/.test(line)) return head.replace(/\[Chinese\]/i, '[English]');
  return head;
}

/** What is saved. */
export const speechStored = (parts) =>
  parts.map((p) => ('line' in p ? retag(p.head, p.line) + p.line + p.tail : p.text)).join('');

// The one range that changed, as [from, oldEnd, newEnd]. The caret (where typing ends) settles
// which of two equal neighbours was typed; without it the common start and end decide.
function changed(before, after, caret) {
  let start = 0;
  const shortest = Math.min(before.length, after.length);
  while (start < shortest && before[start] === after[start]) start++;
  let tail = 0;
  while (
    tail < shortest - start &&
    before[before.length - 1 - tail] === after[after.length - 1 - tail]
  )
    tail++;
  if (Number.isInteger(caret) && caret >= 0 && caret <= after.length) {
    const kept = after.length - caret;
    if (kept <= tail && before.length - kept >= 0) {
      const from = Math.min(start, caret, before.length - kept);
      if (before.slice(0, from) === after.slice(0, from))
        return [from, before.length - kept, caret];
    }
  }
  return [start, before.length - tail, after.length - tail];
}

const shownOf = (part) => ('line' in part ? part.open + part.line + part.close : part.text);

/**
 * The parts after the box's text went from `speechShown(parts)` to `after`. Typing inside a
 * line's quotes edits that line; any change that touches a quote mark turns the lines it
 * touches into plain text; everything else edits the story text.
 */
export function speechEdit(parts, after, caret) {
  const before = speechShown(parts);
  if (before === after) return parts;
  const [from, oldEnd, newEnd] = changed(before, after, caret);
  const typed = after.slice(from, newEnd);
  // Where each part sits in the shown text.
  let at = 0;
  const placed = parts.map((part) => {
    const start = at;
    at += shownOf(part).length;
    return { part, start, end: at };
  });
  // Inside one line's quotes: the words change, the tag stays.
  const inside = placed.find(
    ({ part, start, end }) => 'line' in part && from > start && oldEnd < end,
  );
  if (inside)
    return parts.map((part) =>
      part === inside.part
        ? {
            ...part,
            line:
              part.line.slice(0, from - inside.start - 1) +
              typed +
              part.line.slice(oldEnd - inside.start - 1),
          }
        : part,
    );
  // Lines the change touches become plain text; then the change lies in the text between
  // the remaining lines (an empty gap stands between two lines and at both ends).
  const touches = (start, end) =>
    from === oldEnd ? from > start && from < end : from < end && oldEnd > start;
  const gaps = [{ text: '' }];
  for (const { part, start, end } of placed) {
    if ('line' in part && !touches(start, end)) gaps.push(part, { text: '' });
    else gaps.at(-1).text += shownOf(part);
  }
  at = 0;
  let done = false;
  const out = gaps.map((part) => {
    const start = at;
    at += shownOf(part).length;
    if (done || 'line' in part || from < start || oldEnd > at) return part;
    done = true;
    return { text: part.text.slice(0, from - start) + typed + part.text.slice(oldEnd - start) };
  });
  return out.filter((part) => 'line' in part || part.text);
}
