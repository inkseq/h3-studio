/* Film editor (#347) on the existing v4 story document. No second store.
 * Order: the exporter reads scene.order then shot.order, so reordering rewrites
 * those (a moved shot joins its new neighbour's scene).
 * Inclusion: journey.filmCuts[chapterId].shotIds lists the shots that go into the
 * cut, in order. Absent means every shot (today's behaviour). */
import { chapterShots } from './storyboard-domain.js';
import { children } from './store.js';
import { applyVideoRange, cutFps, videoBinding, videoCut } from './video-cut-model.js';

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
      frames =
        Number.isInteger(cut.requestedFrames) && cut.requestedFrames > 0 ? cut.requestedFrames : 0,
      startFrame = cut.aligned?.startFrame ?? 0,
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

function writeInclusion(project, chapterId, included) {
  const all = chapterShots(project, chapterId).map((shot) => shot.id),
    keep = all.filter((id) => included.has(id)),
    cuts = { ...(project.journey?.filmCuts || {}) };
  if (keep.length === all.length) delete cuts[chapterId];
  else cuts[chapterId] = { shotIds: keep };
  project.journey = { ...(project.journey || {}) };
  if (Object.keys(cuts).length) project.journey.filmCuts = cuts;
  else delete project.journey.filmCuts;
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
  if (project.journey?.filmCuts?.[chapterId]) writeInclusion(project, chapterId, included);
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
  const range = { ...videoBinding(asset), start, end };
  return applyVideoRange(project, shotId, range, {
    duration: sourceDuration,
    useRangeDuration: true,
  });
}

/** Snap seconds to the 24 fps grid the exporter uses. */
export const snap = (seconds) => Math.round(seconds * cutFps) / cutFps;
