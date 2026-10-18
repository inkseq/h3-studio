import { generatedTrackState } from './generated-sound-model.js';
import { chapterShots } from './delivery-model.js';
import { chapterTiming, trackPlacement } from './media-timing.js';
import { captionTrack } from './caption-model.js';

export const roughCutRecipe = 'chapter-roughcut-v1';
const clone = (value) => structuredClone(value);
const source = (entity) =>
  entity
    ? {
        id: entity.id,
        type: entity.type,
        title: entity.title,
        version: entity.version,
        fileId: entity.data.fileId || null,
        cloudAssetId: entity.data.cloudAssetId || null,
        cloudArtifactId: entity.data.cloudArtifactId || null,
        missingFile: !!entity.data.missingFile,
        metadata: clone(entity.data.metadata || {}),
        simulation: !!entity.data.simulation,
      }
    : null;

// A client-side stale-plan guard. The server separately validates an immutable timeline.
// Only edit/output inputs belong here: changing an unrelated character note is not a recut.
export function roughCutSnapshot(project, chapterId, options = {}) {
  const chapter = project.entities.find((e) => e.id === chapterId && e.type === 'chapter');
  const shots = chapterShots(project, chapterId),
    find = (id) => project.entities.find((e) => e.id === id);
  const soundMode = project.journey?.sound?.mode || null;
  const tracks =
    soundMode === 'silent'
      ? []
      : (project.journey?.soundTracks?.[chapterId] || [])
          .filter((t) => !t.muted)
          .map((track) => {
            const effective = generatedTrackState(project, track);
            return {
              ...clone(effective.track),
              ...(track.generatedFrom ? { generatedIssue: effective.issue } : {}),
              source: source(find(track.assetId || track.audioId)),
            };
          });
  return {
    projectId: project.id,
    chapterId: chapter?.id || null,
    chapterTitle: chapter?.title || '',
    aspect: project.journey?.delivery?.aspect || project.journey?.brief?.aspect || '9:16',
    options: clone(options),
    shots: shots.map((shot) => ({
      id: shot.id,
      title: shot.title,
      sceneId: shot.parentId,
      seconds: shot.data.seconds,
      ...(shot.data.selectedVideoRange
        ? { selectedVideoRange: clone(shot.data.selectedVideoRange) }
        : {}),
      selectedAssetId: shot.data.selectedAssetId || null,
      source: source(find(shot.data.selectedAssetId)),
    })),
    soundMode,
    tracks,
    ...(options.burn_subtitles
      ? {
          captions: {
            cues: clone(captionTrack(project, chapterId).cues),
            confirmedSnapshot: clone(captionTrack(project, chapterId).confirmedSnapshot ?? null),
          },
        }
      : {}),
  };
}

export function roughCutSummary(project, chapterId) {
  const shots = chapterShots(project, chapterId),
    timeline = chapterTiming(shots),
    snapshot = roughCutSnapshot(project, chapterId);
  const total = timeline.at(-1)?.end || 0;
  return {
    chapterTitle: snapshot.chapterTitle,
    total,
    soundMode: snapshot.soundMode,
    simulation:
      snapshot.shots.some((s) => s.source?.simulation) ||
      snapshot.tracks.some((t) => t.source?.simulation),
    shots: timeline.map((item, index) => ({
      ...item,
      source: snapshot.shots[index].source,
      seconds: item.end - item.start,
    })),
    tracks: snapshot.tracks.map((track) => ({
      ...track,
      placement: trackPlacement(track, timeline),
    })),
  };
}

export const isRoughCutJob = (job) => job?.recipe_id === roughCutRecipe;
export function roughCutJobs(jobs, projectId, chapterId) {
  return jobs.filter(
    (job) =>
      isRoughCutJob(job) &&
      job.client_ref?.project_id === projectId &&
      job.client_ref?.chapter_id === chapterId,
  );
}
