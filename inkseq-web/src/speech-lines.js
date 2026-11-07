// The video model's speech tags, e.g. `(S1) <d>[Chinese] 今天吃牛。</d>`, shown as plain quoted
// lines wherever a shot's text is only read (h3-studio#1113). Editors keep the stored form.
const TAGGED = /\(S[1-9]\d*\)\s*<d>\s*(?:\[[^\]\r\n]*\]\s*)?([^<>]*?)\s*<\/d>/gi;

export function readableLines(text) {
  return String(text ?? '').replace(TAGGED, (_, line) =>
    [...line].some((ch) => ch.codePointAt(0) >= 0x3000) ? `「${line}」` : `“${line}”`,
  );
}
