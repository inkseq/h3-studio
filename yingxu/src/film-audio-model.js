/* Film editor sound (#347/#408) on the existing chapter sound tracks the exporter already mixes:
 * journey.soundTracks[chapterId] = [{id, assetId, fileId, role, shotId, offset, start, end,
 * duration, gain, muted, generatedFrom?}]. A clip's own sound is the generated FLAC track that
 * follows its shot; music and voice are ordinary tracks placed at an absolute time.
 * The exported film contains only these tracks, never the MP4's embedded audio. */
import { chapterShots } from './storyboard-domain.js';
import { chapterTiming, trackPlacement } from './media-timing.js';
import { generatedAudioOption, upsertGeneratedSound } from './generated-sound-model.js';

const MAX_TRACKS = 32,
  MIN_LENGTH = 0.1;
const round = (value) => Math.round(value * 1000) / 1000;

export const chapterTimeline = (project, chapterId) =>
  chapterTiming(chapterShots(project, chapterId));

function tracksOf(project, chapterId) {
  project.journey ||= {};
  project.journey.soundTracks ||= {};
  return (project.journey.soundTracks[chapterId] ||= []);
}
function findTrack(project, chapterId, trackId) {
  const track = tracksOf(project, chapterId).find((t) => t.id === trackId);
  if (!track) throw Error('editor.audioMissing');
  return track;
}
// Adding audible sound to a silent or unset chapter switches the export to mixed sound.
function audible(project) {
  const mode = project.journey?.sound?.mode;
  if (!mode || mode === 'silent')
    project.journey.sound = { ...(project.journey.sound || {}), mode: 'mixed' };
}

/** Tracks with their timeline placement (seconds) and display title. */
export function audioBlocks(project, chapterId) {
  const timeline = chapterTimeline(project, chapterId);
  return (project.journey?.soundTracks?.[chapterId] || []).map((track) => {
    const asset = project.entities.find((e) => e.id === (track.assetId || track.audioId));
    const shot = track.generatedFrom ? timeline.find((s) => s.id === track.shotId) : null;
    return {
      track,
      generated: !!track.generatedFrom,
      title: shot ? shot.title : asset?.title || track.id,
      placement: trackPlacement(track, timeline),
      fileId: asset?.data.fileId || track.fileId,
      asset,
    };
  });
}

/** The clip's own (generated) sound: 'on', 'off', or an unavailable reason. */
export function clipSound(project, chapterId, shotId) {
  const track = (project.journey?.soundTracks?.[chapterId] || []).find(
    (t) => t.generatedFrom && t.shotId === shotId,
  );
  if (track) return { state: track.muted ? 'off' : 'on', track };
  const option = generatedAudioOption(project, shotId);
  return option.issue ? { state: 'none', issue: option.issue } : { state: 'off', track: null };
}

/** Turn a clip's own sound on or off. Mutates `project`. */
export function toggleClipSound(project, chapterId, shotId) {
  const current = clipSound(project, chapterId, shotId);
  if (current.state === 'none') throw Error(current.issue);
  if (!current.track) {
    upsertGeneratedSound(project, shotId);
    return;
  }
  current.track.muted = current.state === 'on';
  if (!current.track.muted) audible(project);
}

/** Add music or voice from a project audio entity at `at` seconds. Mutates `project`. */
export function addAudioTrack(project, chapterId, asset, at, duration, role = 'music') {
  if (asset?.type !== 'audio' || !asset.data.fileId) throw Error('editor.audioMissing');
  if (!Number.isFinite(duration) || duration <= 0) throw Error('editor.audioDuration');
  const tracks = tracksOf(project, chapterId);
  if (tracks.length >= MAX_TRACKS) throw Error('editor.audioLimit');
  const track = {
    id: 'sound-' + crypto.randomUUID(),
    assetId: asset.id,
    fileId: asset.data.fileId,
    role,
    shotId: '',
    offset: round(Math.max(0, at)),
    start: 0,
    end: round(duration),
    duration,
    gain: 0.7,
    muted: false,
  };
  tracks.push(track);
  audible(project);
  return track;
}

/** Move a music/voice track so it starts at `start` seconds on the film. */
export function moveTrack(project, chapterId, trackId, start) {
  const track = findTrack(project, chapterId, trackId);
  if (track.generatedFrom) throw Error('editor.audioFollowsClip');
  track.shotId = '';
  track.offset = round(Math.max(0, start));
}

/** Trim a music/voice track by `delta` seconds at one edge; the audio stays in place. */
export function trimTrack(project, chapterId, trackId, side, delta) {
  const track = findTrack(project, chapterId, trackId);
  if (track.generatedFrom) throw Error('editor.audioFollowsClip');
  const timeline = chapterTimeline(project, chapterId),
    placement = trackPlacement(track, timeline);
  if (!placement) throw Error('editor.audioMissing');
  const duration = Number.isFinite(track.duration) ? track.duration : track.end;
  if (side === 'start') {
    const start = Math.min(Math.max(0, track.start + delta), track.end - MIN_LENGTH);
    const shift = start - track.start;
    if (placement.start + shift < 0) return;
    track.shotId = '';
    track.offset = round(placement.start + shift);
    track.start = round(start);
  } else {
    track.end = round(Math.max(track.start + MIN_LENGTH, Math.min(duration, track.end + delta)));
  }
}

/** Change volume (0–1) or mute. */
export function setTrack(project, chapterId, trackId, patch) {
  const track = findTrack(project, chapterId, trackId);
  if ('gain' in patch) track.gain = round(Math.min(1, Math.max(0, Number(patch.gain) || 0)));
  if ('muted' in patch) {
    track.muted = !!patch.muted;
    if (!track.muted) audible(project);
  }
}

export function removeTrack(project, chapterId, trackId) {
  const tracks = tracksOf(project, chapterId);
  project.journey.soundTracks[chapterId] = tracks.filter((t) => t.id !== trackId);
}
