// Editing a chapter's caption track in the film editor. Cues live on
// project.journey.captionTracks[chapterId].cues as { id, start, end, text } in seconds on the
// cut timeline (caption-model.js); any edit leaves an earlier burn-in confirmation stale.
import { captionProblems, captionSignature, captionTrack, orderedCues } from './caption-model.js';
import { planOf } from './shot-plan.js';
import { sceneBeats } from './script-beats.js';

export const MAX_CUE_TEXT = 200;
// The burn-in layout (caption-model.js burnCaptionStatus): at most 2 lines of 1–18 characters.
export const CAPTION_LINE = 18,
  CAPTION_LINES = 2;
const MIN_CUE = 0.3;
const round = (value) => Math.round(value * 100) / 100;
const chars = (text) => Array.from(text).length;

/** Each line of a caption as typed, with its length and whether burn-in accepts it. */
export function captionLines(text) {
  return String(text ?? '')
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .map((line, index) => {
      const length = chars(line);
      return { line, length, ok: index < CAPTION_LINES && length >= 1 && length <= CAPTION_LINE };
    });
}

// A line may break after punctuation or a space; the words themselves are never changed.
const BREAK_AFTER = /[\s，。！？、；：,.!?;:…—）」』”)]/u;

// Cut `text` (no break points) into pieces of at most `width`, as even as possible.
function evenPieces(text, width) {
  const all = Array.from(text),
    size = Math.ceil(all.length / Math.ceil(all.length / width)),
    out = [];
  for (let at = 0; at < all.length; at += size) out.push(all.slice(at, at + size).join(''));
  return out;
}

/**
 * One script line as caption texts of at most 2 lines × 18 characters each (lines joined by
 * "\n"), breaking after punctuation or spaces where it can. A line that already fits stays whole.
 */
export function splitCaptionText(text, width = CAPTION_LINE, lines = CAPTION_LINES) {
  const clean = String(text ?? '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!clean) return [];
  if (chars(clean) <= width) return [clean];
  const phrases = [];
  let phrase = '';
  for (const ch of Array.from(clean)) {
    phrase += ch;
    if (BREAK_AFTER.test(ch)) {
      phrases.push(phrase);
      phrase = '';
    }
  }
  if (phrase) phrases.push(phrase);
  const rows = [];
  let row = '';
  const push = () => {
    if (row.trim()) rows.push(row.trim());
    row = '';
  };
  for (const part of phrases) {
    if (chars(part.trim()) > width) {
      push();
      const pieces = evenPieces(part.trim(), width);
      rows.push(...pieces.slice(0, -1));
      row = pieces.at(-1);
      continue;
    }
    if (chars((row + part).trim()) > width) push();
    row += part;
  }
  push();
  const out = [];
  for (let at = 0; at < rows.length; at += lines) out.push(rows.slice(at, at + lines).join('\n'));
  return out;
}

// The video model's speech tags in a shot's action text, e.g. `(S1) <d>[Chinese] 今天吃牛。</d>`:
// a story written by chat keeps its lines there instead of in script beats.
const SPOKEN = /\(S[1-9]\d*\)\s*<d>\s*(?:\[[^\]\r\n]*\]\s*)?([^<>]*?)\s*<\/d>/gi;
const spokenLines = (text) =>
  [...String(text ?? '').matchAll(SPOKEN)].map((match) => match[1].trim()).filter(Boolean);

function track(project, chapterId) {
  project.journey ||= {};
  project.journey.captionTracks ||= {};
  const current = captionTrack(project, chapterId);
  const next = { ...current, cues: orderedCues(current.cues).map((cue) => ({ ...cue })) };
  project.journey.captionTracks[chapterId] = next;
  return next;
}

const newCueId = () => 'cue-' + crypto.randomUUID().slice(0, 12);

/** Change one cue's text or times; times stay inside [0, total] and at least MIN_CUE long. */
export function setCue(project, chapterId, id, patch, total) {
  const cues = track(project, chapterId).cues,
    cue = cues.find((item) => item.id === id);
  if (!cue) throw Error('editor.cueMissing');
  if ('text' in patch) cue.text = String(patch.text).slice(0, MAX_CUE_TEXT);
  let start = 'start' in patch ? Number(patch.start) : Number(cue.start),
    end = 'end' in patch ? Number(patch.end) : Number(cue.end);
  if (!Number.isFinite(start) || !Number.isFinite(end)) throw Error('editor.cueTime');
  start = Math.max(0, Math.min(start, total - MIN_CUE));
  end = Math.min(total, Math.max(end, start + MIN_CUE));
  cue.start = round(start);
  cue.end = round(end);
}

/** A new empty cue at `at` seconds, 2 s long or up to the next cue. Returns its id. */
export function addCue(project, chapterId, at, total) {
  const current = track(project, chapterId),
    start = round(Math.max(0, Math.min(at, total - MIN_CUE))),
    next = current.cues.find((cue) => cue.start > start),
    end = round(Math.min(total, start + 2, next ? Math.max(next.start, start + MIN_CUE) : total));
  const cue = { id: newCueId(), start, end, text: '' };
  current.cues = orderedCues([...current.cues, cue]);
  return cue.id;
}

export function removeCue(project, chapterId, id) {
  const current = track(project, chapterId);
  current.cues = current.cues.filter((cue) => cue.id !== id);
}

// The lines a clip films: its plan's claimed line beats in scene order, else the speech tags in
// the shot's own action text.
function clipLines(byId, shot) {
  const claimed = new Set(planOf(shot).beats),
    beats = sceneBeats(byId.get(shot?.parentId))
      .filter((beat) => claimed.has(beat.id) && beat.kind === 'line' && beat.text.trim())
      .map((beat) => beat.text.trim());
  return beats.length ? beats : spokenLines(shot?.description);
}

/**
 * Cues from the script lines each clip films, spread over the clip in proportion to their
 * length. A long line becomes several cues of at most 2 × 18 characters, each timed by its
 * share of the line. `clips` are the editor's included clips with `from`/`frames` on the cut
 * timeline and `fps`.
 */
export function cuesFromLines(project, clips, fps) {
  const byId = new Map(project.entities.map((entity) => [entity.id, entity])),
    cues = [];
  for (const clip of clips) {
    const lines = clipLines(byId, byId.get(clip.id));
    if (!lines.length || !(clip.frames > 0)) continue;
    const start = clip.from / fps,
      length = clip.frames / fps,
      weight = lines.reduce((sum, line) => sum + chars(line), 0);
    let at = start;
    for (const line of lines) {
      const span = (length * chars(line)) / weight,
        parts = splitCaptionText(line),
        size = parts.reduce((sum, part) => sum + chars(part.replace(/\n/g, '')), 0);
      let from = at;
      for (const part of parts) {
        const piece = (span * chars(part.replace(/\n/g, ''))) / size;
        cues.push({
          id: newCueId(),
          start: round(from),
          end: round(Math.max(from + MIN_CUE, from + piece)),
          text: part.slice(0, MAX_CUE_TEXT),
        });
        from += piece;
      }
      at += span;
    }
  }
  return cues;
}

/**
 * Confirm this version of the captions for burn-in: the export compares the stored snapshot with
 * the current captions and timeline, so any later change asks for a new confirmation.
 */
export function confirmCaptions(project, chapterId) {
  if (captionProblems(project, chapterId).length) throw Error('editor.captionsHaveProblems');
  const snapshot = captionSignature(project, chapterId);
  project.journey = { ...(project.journey || {}) };
  project.journey.captionTracks = {
    ...(project.journey.captionTracks || {}),
    [chapterId]: {
      ...captionTrack(project, chapterId),
      confirmedSnapshot: snapshot,
      confirmedAt: new Date().toISOString(),
    },
  };
}

/** Replace the chapter's cues with ones built from the script lines. */
export function fillCuesFromLines(project, chapterId, clips, fps) {
  const cues = cuesFromLines(project, clips, fps);
  if (!cues.length) throw Error('editor.noLines');
  track(project, chapterId).cues = cues;
}
