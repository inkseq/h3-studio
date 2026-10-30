/* Film editor (#347) on the existing v4 story document. No second store.
 * Order: the exporter reads scene.order then shot.order, so reordering rewrites
 * those (a moved shot joins its new neighbour's scene).
 * Inclusion: journey.filmCuts[chapterId].shotIds lists the shots that go into the
 * cut, in order. Absent means every shot (today's behaviour). */
import { chapterShots } from './storyboard-domain.js';
import { children } from './store.js';
import { applyVideoRange, cutFps, rateOf, videoBinding, videoCut } from './video-cut-model.js';
import { shotSegments } from './shot-segments.js';

// Kept parts (#512) as whole source frames, snapped like the server (start up, end down).
function partFrames(shot, video) {
  if (!video) return null;
  const parts = shotSegments(shot.data.videoSegments)
    .map((r) => {
      const startFrame = Math.ceil(r.start * cutFps - 1e-7),
        endFrame = Math.floor(r.end * cutFps + 1e-7);
      return { startFrame, frames: endFrame - startFrame };
    })
    .filter((part) => part.frames > 0);
  return parts.length ? parts : null;
}

const find = (project, id) => project.entities.find((e) => e.id === id);
const sourceSeconds = (asset) =>
  asset?.data.metadata?.duration ?? asset?.data.metadata?.duration_s ?? null;

export function includedShotIds(project, chapterId) {
  const ids = chapterShots(project, chapterId).map((shot) => shot.id),
    stored = project.journey?.filmCuts?.[chapterId]?.shotIds;
  if (!Array.isArray(stored)) return ids;
  const keep = new Set(stored);
  return ids.filter((id) => keep.has(id));
}

/** Clips in story order with their trim, length in frames and timeline position. */
export function editorClips(project, chapterId) {
  const included = new Set(includedShotIds(project, chapterId));
  let from = 0;
  return chapterShots(project, chapterId).map((shot, index) => {
    const asset = find(project, shot.data.selectedAssetId),
      video = asset?.type === 'video' ? asset : null,
      cut = videoCut(shot, video),
      parts = partFrames(shot, video),
      frames = parts
        ? parts.reduce((sum, part) => sum + part.frames, 0)
        : Number.isInteger(cut.requestedFrames) && cut.requestedFrames > 0
          ? cut.requestedFrames
          : 0,
      startFrame = parts ? parts[0].startFrame : (cut.aligned?.startFrame ?? 0),
      inCut = included.has(shot.id),
      clip = {
        id: shot.id,
        index,
        title: shot.title,
        sceneId: shot.parentId,
        asset: video,
        sourceSeconds: sourceSeconds(video),
        startFrame,
        frames,
        // Source frames the clip uses (more than `frames` when slowed down).
        sourceFrames: cut.rate ? cut.aligned.frames : frames,
        rate: parts ? 1 : rateOf(shot.data.selectedVideoRange),
        // Several kept parts of the take play back to back in this clip's slot.
        parts,
        included: inCut,
        // A clip without a usable video still holds its slot so timing matches export.
        playable: !!video?.data.fileId && !video.data.missingFile,
        issue: cut.issue,
        from: inCut ? from : null,
      };
    if (inCut) from += frames;
    return clip;
  });
}

export const cutFrames = (clips) =>
  clips.filter((clip) => clip.included).reduce((sum, clip) => sum + clip.frames, 0);

// filmCuts[chapterId] holds shotIds (inclusion) and clip_sound_crossfade_s; each is kept
// when the other changes, and the entry disappears only when both are at their default.
function writeCut(project, chapterId, patch) {
  const cuts = { ...(project.journey?.filmCuts || {}) },
    entry = { ...(cuts[chapterId] || {}), ...patch };
  for (const key of Object.keys(entry)) if (entry[key] === undefined) delete entry[key];
  if (Object.keys(entry).length) cuts[chapterId] = entry;
  else delete cuts[chapterId];
  project.journey = { ...(project.journey || {}) };
  if (Object.keys(cuts).length) project.journey.filmCuts = cuts;
  else delete project.journey.filmCuts;
}

function writeInclusion(project, chapterId, included) {
  const all = chapterShots(project, chapterId).map((shot) => shot.id),
    keep = all.filter((id) => included.has(id));
  writeCut(project, chapterId, { shotIds: keep.length === all.length ? undefined : keep });
}

export const CROSSFADES = [0, 0.25, 0.5, 1];
/** Seconds of sound blending at each boundary between clips' own sound (render v5). */
export const clipSoundCrossfade = (project, chapterId) =>
  project.journey?.filmCuts?.[chapterId]?.clip_sound_crossfade_s ?? 0.5;
export function setClipSoundCrossfade(project, chapterId, value) {
  if (!CROSSFADES.includes(value)) throw Error('editor.crossfadeInvalid');
  writeCut(project, chapterId, { clip_sound_crossfade_s: value });
}

/** Move one shot to a new position in the chapter's flat order. Mutates `project`. */
export function moveShot(project, chapterId, shotId, toIndex) {
  const shots = chapterShots(project, chapterId),
    from = shots.findIndex((shot) => shot.id === shotId);
  if (from < 0) throw Error('editor.missingShot');
  const included = new Set(includedShotIds(project, chapterId));
  const flat = shots.slice();
  const [moved] = flat.splice(from, 1);
  const to = Math.max(0, Math.min(flat.length, toIndex));
  flat.splice(to, 0, moved);
  const neighbour = flat[to - 1] || flat[to + 1];
  if (neighbour) moved.parentId = neighbour.parentId;
  const scenes = children(project, chapterId).filter((e) => e.type === 'scene');
  for (const scene of scenes)
    flat
      .filter((shot) => shot.parentId === scene.id)
      .forEach((shot, index) => {
        shot.order = index + 1;
      });
  if (Array.isArray(project.journey?.filmCuts?.[chapterId]?.shotIds))
    writeInclusion(project, chapterId, included);
}

/** Put a shot in or out of the cut. The shot itself is always kept. Mutates `project`. */
export function setIncluded(project, chapterId, shotId, inCut) {
  const included = new Set(includedShotIds(project, chapterId));
  if (inCut) included.add(shotId);
  else included.delete(shotId);
  if (!included.size) throw Error('editor.keepOne');
  writeInclusion(project, chapterId, included);
}

/** Trim a clip to [start, end) seconds of its source; the cut length follows the range. */
export function trimClip(project, shotId, start, end, sourceDuration) {
  const shot = find(project, shotId),
    asset = find(project, shot?.data.selectedAssetId);
  if (!asset || asset.type !== 'video') throw Error('editor.noVideoError');
  const old = shot.data.selectedVideoRange,
    range = { ...videoBinding(asset), start, end };
  if (old && 'playback_rate' in old) range.playback_rate = old.playback_rate;
  return applyVideoRange(project, shotId, range, {
    duration: sourceDuration,
    useRangeDuration: true,
  });
}

/** Snap seconds to the 24 fps grid the exporter uses. */
export const snap = (seconds) => Math.round(seconds * cutFps) / cutFps;

/** Change a clip's speed. The first change pins the span the clip plays now, so the clip keeps
 * its content and its length becomes that span divided by the rate. Mutates `project`. */
export function setClipSpeed(project, shotId, rate, sourceDuration) {
  const shot = find(project, shotId),
    asset = find(project, shot?.data.selectedAssetId);
  if (!asset || asset.type !== 'video') throw Error('editor.noVideoError');
  const cut = videoCut(shot, asset, sourceDuration);
  if (cut.issue) throw Error(cut.issue);
  const range = {
    ...videoBinding(asset),
    start: cut.sourceStart,
    end: cut.sourceEnd,
    playback_rate: rate,
  };
  return applyVideoRange(project, shotId, range, { duration: sourceDuration });
}
