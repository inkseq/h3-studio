// Which class names the app's source can render, read from its string literals. A class built
// at run time counts when a string fragment it starts with is concatenated
// ('tile-' + kind, 'c' + n, `p4-${x}`).
import { tokenize } from 'espree';

// Only string and template literals made of class-like tokens can be class names; copy such
// as 'Edit as a new proposal' does not keep .proposal alive.
const classLike = /^([a-z][a-z0-9_-]*|[A-Z])$/;

export function classUse() {
  const words = new Set(),
    prefixes = new Set();
  const addWords = (part) => {
    const tokens = part.trim().split(/\s+/).filter(Boolean);
    if (tokens.length && tokens.every((token) => classLike.test(token)))
      for (const token of tokens) words.add(token);
  };
  const addPrefix = (part) => {
    const tail = /[A-Za-z][\w-]*$/.exec(part);
    if (tail) prefixes.add(tail[0]);
  };
  return {
    // The JavaScript tokenizer finds the literals. Matching quotes with a pattern cannot tell
    // a quote in a comment or a regular expression from one that opens a string: one such
    // backtick once hid every literal after it, and a thousand rendered classes read as unused.
    script(text) {
      const tokens = tokenize(text, {
        ecmaVersion: 'latest',
        sourceType: 'module',
        ecmaFeatures: { jsx: true },
      });
      tokens.forEach(({ type, value }, at) => {
        const before = tokens[at - 1]?.value,
          after = tokens[at + 1]?.value;
        if (type === 'String' || (type === 'JSXText' && before === '=' && /^['"]/.test(value))) {
          // className="dock" arrives as JSXText with its quotes; other JSXText is copy on the page
          const part = value.slice(1, -1);
          addWords(part);
          if (after === '+') addPrefix(part);
        } else if (type === 'Template') {
          // `a-${x} b` arrives in pieces: `a-${ and } b`
          const open = value.endsWith('${');
          const part = value.slice(1, open ? -2 : -1);
          addWords(part);
          if (open || after === '+') addPrefix(part);
        }
      });
    },
    markup(html) {
      for (const [, , double, single] of html.matchAll(/\sclass\s*=\s*("([^"]*)"|'([^']*)')/g))
        addWords(double ?? single);
    },
    // A fragment builds classes only as 'prefix-' + x or a numbered class ('c' + n → c20).
    renders: (name) =>
      words.has(name) ||
      [...prefixes].some(
        (prefix) =>
          name.startsWith(prefix) &&
          (prefix.endsWith('-') || /^\d+$/.test(name.slice(prefix.length))),
      ),
  };
}

export function sheetClasses(css) {
  const sheet = css
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/url\([^)]*\)/g, '')
    // declarations hold numbers like 0.5em, not classes
    .replace(/:[^;{}]*;/g, ';');
  return new Set([...sheet.matchAll(/\.([A-Za-z_][\w-]*)/g)].map((m) => m[1]));
}
