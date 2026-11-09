// Editing a chapter's caption track in the film editor. Cues live on
// project.journey.captionTracks[chapterId].cues as { id, start, end, text } in seconds on the
// cut timeline (caption-model.js); any edit leaves an earlier burn-in confirmation stale.
import { captionTrack, orderedCues } from './caption-model.js';
import { planOf } from './shot-plan.js';
import { sceneBeats } from './script-beats.js';

export const MAX_CUE_TEXT = 200;
const MIN_CUE = 0.3;
const round = (value) => Math.round(value * 100) / 100;

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

/**
 * Cues from the script lines each clip films (its plan's claimed beats, in scene order),
 * spread over the clip in proportion to their length. `clips` are the editor's included
 * clips with `from`/`frames` on the cut timeline and `fps`.
 */
export function cuesFromLines(project, clips, fps) {
  const byId = new Map(project.entities.map((entity) => [entity.id, entity])),
    cues = [];
  for (const clip of clips) {
    const shot = byId.get(clip.id),
      claimed = new Set(planOf(shot).beats),
      lines = sceneBeats(byId.get(shot?.parentId)).filter(
        (beat) => claimed.has(beat.id) && beat.kind === 'line' && beat.text.trim(),
      );
    if (!lines.length || !(clip.frames > 0)) continue;
    const start = clip.from / fps,
      length = clip.frames / fps,
      weight = lines.reduce((sum, line) => sum + line.text.trim().length, 0);
    let at = start;
    for (const line of lines) {
      const span = (length * line.text.trim().length) / weight;
      cues.push({
        id: newCueId(),
        start: round(at),
        end: round(Math.max(at + MIN_CUE, at + span)),
        text: line.text.trim().slice(0, MAX_CUE_TEXT),
      });
      at += span;
    }
  }
  return cues;
}

/** Replace the chapter's cues with ones built from the script lines. */
export function fillCuesFromLines(project, chapterId, clips, fps) {
  const cues = cuesFromLines(project, clips, fps);
  if (!cues.length) throw Error('editor.noLines');
  track(project, chapterId).cues = cues;
}
