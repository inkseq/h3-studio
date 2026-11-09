// Film editor keyboard (story audit 2026-11-09): the transport keys most editors share, and the
// rules for when the page keeps a key for itself. Ideas from Kdenlive and Shotcut (J/K/L, I/O,
// Up/Down to the previous/next cut, Shift+arrow one second) and from openreel-video's focus
// guard (a field, slider, menu or dialog keeps its own keys); no code was copied.
import { readableLines } from './speech-lines.js';

// Where a key belongs to the focused control, not to the editor.
const OWN_KEYS =
  'input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="slider"], [role="textbox"], [role="combobox"], [role="listbox"], [role="menu"], [role="menuitem"], [role="dialog"], dialog, .ui-popover';
// Timeline items (clips, sound blocks, caption cues) take the transport arrows; any other button
// keeps Space, Enter and arrows for itself.
const TIMELINE_ITEM = '[data-timeline-item]';

// Command per key: [plain, with Shift]. Letters are matched in either case.
const COMMANDS = {
  ' ': ['toggle', 'toggle'],
  j: ['reverse', 'reverse'],
  k: ['pause', 'pause'],
  l: ['forward', 'forward'],
  i: ['markIn', 'markIn'],
  o: ['markOut', 'markOut'],
  ArrowLeft: ['backFrame', 'back1s'],
  ArrowRight: ['forwardFrame', 'forward1s'],
  ArrowUp: ['previousCut', 'previousCut'],
  ArrowDown: ['nextCut', 'nextCut'],
  Home: ['start', 'start'],
  End: ['end', 'end'],
};
// Keys a focused button or link keeps for itself (activation, its own arrow handling).
const CONTROL_KEYS = new Set([
  ' ',
  'Enter',
  'ArrowLeft',
  'ArrowRight',
  'ArrowUp',
  'ArrowDown',
  'Home',
  'End',
]);

// Whether the focused element keeps this key: a field, slider, menu or dialog keeps every key;
// a real button (a caption cue too) keeps Space and Enter; any other control that is not a
// timeline item keeps the navigation keys.
function keptByTarget(target, key) {
  if (!target) return false;
  if (target.closest(OWN_KEYS)) return true;
  if ((key === ' ' || key === 'Enter') && target.closest('button, a[href]')) return true;
  return (
    CONTROL_KEYS.has(key) &&
    !target.closest(TIMELINE_ITEM) &&
    !!target.closest('button, a[href], [role="button"], [role="tab"]')
  );
}

/** The transport command for a key press in the editor, or null to leave it alone. */
export function transportCommand(event, { inEditor = true } = {}) {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || !inEditor) return null;
  const target = event.target?.closest ? event.target : null,
    key = event.code === 'Space' ? ' ' : event.key;
  if (keptByTarget(target, key)) return null;
  if (key === '?') return 'help';
  if (event.altKey) return null;
  const pair = COMMANDS[key.length === 1 ? key.toLowerCase() : key];
  return pair ? pair[event.shiftKey ? 1 : 0] : null;
}

/** J/L shuttle: each press doubles the speed in its direction (to 4×); the other key turns. */
export function shuttle(rate, playing, direction) {
  if (!playing || Math.sign(rate) !== direction) return direction;
  return direction * Math.min(4, Math.abs(rate) * 2);
}

/** Frames where one included clip ends and the next starts, plus the film's start and end. */
export function cutPoints(included) {
  const points = new Set([0]);
  for (const clip of included) {
    points.add(clip.from);
    points.add(clip.from + clip.frames);
  }
  return [...points].sort((a, b) => a - b);
}

/** The previous (-1) or next (+1) cut from `frame`; stays put at either end. */
export function stepToCut(frame, points, direction) {
  if (direction < 0) return [...points].reverse().find((p) => p < frame) ?? points[0] ?? 0;
  return points.find((p) => p > frame) ?? points.at(-1) ?? 0;
}

/**
 * The source time (seconds) under cut frame `frame` inside `clip`, or null when the playhead is
 * not on that clip. Slowed or sped-up clips map through their rate.
 */
export function sourceTimeAt(clip, frame, fps) {
  if (!clip?.included || !Number.isInteger(clip.from)) return null;
  if (frame < clip.from || frame >= clip.from + clip.frames) return null;
  return (clip.startFrame + (frame - clip.from) * (clip.rate || 1)) / fps;
}

/**
 * The new [start, end) source range when I or O marks the playhead on the selected clip, or a
 * reason it cannot: 'editor.playheadOff' (not on the clip), 'editor.keptParts' (several parts).
 */
export function markRange(clip, frame, fps, side) {
  if (clip?.parts) return { error: 'editor.keptPartsKey' };
  const at = sourceTimeAt(clip, frame, fps);
  if (at === null) return { error: 'editor.playheadOff' };
  const start = clip.startFrame / fps,
    end = (clip.startFrame + clip.sourceFrames) / fps,
    one = 1 / fps;
  if (side === 'in') return { start: Math.min(at, end - one), end };
  return { start, end: Math.max(start + one, at + one * (clip.rate || 1)) };
}

/** A clip's name: its title, else the first line of its action text without speech tags. */
export function clipName(shot) {
  const title = String(shot?.title ?? '').trim();
  if (title) return title;
  const line = readableLines(shot?.description ?? '')
    .split('\n')
    .map((part) => part.trim())
    .find(Boolean);
  return line ? Array.from(line).slice(0, 40).join('') : '';
}
