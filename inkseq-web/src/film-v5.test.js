import test from 'node:test';
import assert from 'node:assert/strict';
import {
  clipSoundCrossfade,
  cutFrames,
  editorClips,
  includedShotIds,
  setClipSoundCrossfade,
  setClipSpeed,
  setIncluded,
  trimClip,
} from './film-edit-model.js';
import { detachClipSound, effectiveMuted, setTrackFade } from './film-audio-model.js';
import { chapterTiming, trackPlacement } from './media-timing.js';
import { outputFrames, rangeShapeIssue, videoBinding } from './video-cut-model.js';
import { chapterShots } from './storyboard-domain.js';
import { validateProject } from './store.js';

const node = (id, type, parentId, order, data = {}) => ({
  id,
  type,
  title: id,
  description: '',
  parentId,
  order,
  version: 1,
  status: 'draft',
  data,
});
// Codex's combined v5 fixture (#408): C, A, D in order, each with a 5 s source video.
function story() {
  const entities = [node('ch', 'chapter', null, 1), node('sc', 'scene', 'ch', 1)];
  for (const [shot, order] of [
    ['C', 1],
    ['A', 2],
    ['D', 3],
  ])
    entities.push(
      node('v-' + shot, 'video', null, 0, {
        fileId: 'file-' + shot,
        mime: 'video/mp4',
        metadata: { duration: 5 },
      }),
      node(shot, 'shot', 'sc', order, { seconds: 4, selectedAssetId: 'v-' + shot }),
    );
  return { schemaVersion: 4, id: 'p', title: 't', logline: '', entities, links: [], jobs: [] };
}

test('output frames use exact fractions: N = floor(M / rate + 1/2)', () => {
  assert.equal(outputFrames(72, 0.75), 96);
  assert.equal(outputFrames(48, 2), 24);
  assert.equal(outputFrames(48, 0.5), 96);
  assert.equal(outputFrames(1, 2), 1); // 0.5 rounds half up
  assert.equal(outputFrames(5, 1.5), 3); // 3.33 → 3
  assert.equal(outputFrames(7, 1.25), 6); // 5.6 → 6
});

test('the v5 fixture lengths: 3 s @0.75, 2 s @2, 2 s @0.5 → 216 frames', () => {
  const p = story();
  trimClip(p, 'C', 0, 3, 5);
  trimClip(p, 'A', 1, 3, 5);
  trimClip(p, 'D', 0.5, 2.5, 5);
  setClipSpeed(p, 'C', 0.75, 5);
  setClipSpeed(p, 'A', 2, 5);
  setClipSpeed(p, 'D', 0.5, 5);
  const clips = editorClips(p, 'ch');
  assert.deepEqual(
    clips.map((c) => [c.id, c.rate, c.sourceFrames, c.frames, c.from]),
    [
      ['C', 0.75, 72, 96, 0],
      ['A', 2, 48, 24, 96],
      ['D', 0.5, 48, 96, 120],
    ],
  );
  assert.equal(cutFrames(clips), 216);
  // The story's own timing (what the exporter and sound placement read) agrees.
  const timing = chapterTiming(chapterShots(p, 'ch'));
  assert.deepEqual(
    timing.map((s) => [s.id, s.start, s.end, s.rate]),
    [
      ['C', 0, 4, 0.75],
      ['A', 4, 5, 2],
      ['D', 5, 9, 0.5],
    ],
  );
});

test('the first speed change pins the span the clip plays now, not the unused tail', () => {
  const p = story(); // 4 s planned from a 5 s source, no range yet
  setClipSpeed(p, 'C', 2, 5);
  const shot = p.entities.find((e) => e.id === 'C');
  assert.deepEqual(shot.data.selectedVideoRange, {
    ...videoBinding(p.entities.find((e) => e.id === 'v-C')),
    start: 0,
    end: 4,
    playback_rate: 2,
  });
  assert.equal(shot.data.seconds, 2);
  // Trimming later keeps the speed and the length follows the new range.
  trimClip(p, 'C', 1, 4, 5);
  assert.equal(shot.data.selectedVideoRange.playback_rate, 2);
  assert.equal(shot.data.seconds, 1.5);
});

test('range shape accepts only allow-listed speeds', () => {
  const base = {
    assetId: 'v',
    fileId: 'f',
    cloudAssetId: null,
    cloudArtifactId: null,
    start: 0,
    end: 1,
  };
  assert.equal(rangeShapeIssue({ ...base, playback_rate: 1.5 }), '');
  assert.match(rangeShapeIssue({ ...base, playback_rate: 3 }), /0\.5/);
  assert.match(rangeShapeIssue({ ...base, speed: 2 }), /来源绑定/);
});

test('clip sound blend lives beside inclusion and both survive each other', () => {
  const p = story();
  assert.equal(clipSoundCrossfade(p, 'ch'), 0.5);
  setIncluded(p, 'ch', 'A', false);
  setClipSoundCrossfade(p, 'ch', 1);
  assert.deepEqual(p.journey.filmCuts.ch, { shotIds: ['C', 'D'], clip_sound_crossfade_s: 1 });
  setIncluded(p, 'ch', 'A', true);
  assert.deepEqual(p.journey.filmCuts.ch, { clip_sound_crossfade_s: 1 });
  assert.deepEqual(includedShotIds(p, 'ch'), ['C', 'A', 'D']);
  assert.throws(() => setClipSoundCrossfade(p, 'ch', 0.4), /crossfade/);
});

test('own sound follows clip speed, detaches at its place, and fades stay within the track', () => {
  const p = story();
  trimClip(p, 'C', 0, 3, 5);
  setClipSpeed(p, 'C', 0.75, 5);
  const track = {
    id: 'sound-c',
    assetId: 'a-C',
    fileId: 'fa',
    role: 'generated',
    shotId: 'C',
    offset: 0,
    start: 0,
    end: 3,
    duration: 5,
    gain: 0.7,
    muted: false,
    generatedFrom: {
      jobId: 'j',
      videoEntityId: 'v-C',
      videoArtifactId: 'va',
      audioArtifactId: 'aa',
    },
  };
  const music = { ...track, id: 'm', shotId: '', generatedFrom: undefined, offset: 5, end: 2 };
  delete music.generatedFrom;
  p.journey = { soundTracks: { ch: [track, music] } };
  const timing = chapterTiming(chapterShots(p, 'ch'));
  // 3 s of source at 0.75× fills the clip's 4 s.
  assert.deepEqual(trackPlacement(track, timing), { start: 0, end: 4 });
  assert.equal(effectiveMuted(track, timing[0]), false);
  // Below 0.75× own sound is silent unless explicitly turned on.
  assert.equal(effectiveMuted(track, { rate: 0.5 }), true);
  assert.equal(effectiveMuted({ ...track, slow_rate_audio_enabled: true }, { rate: 0.5 }), false);
  detachClipSound(p, 'ch', 'sound-c', timing);
  assert.equal(track.detached, true);
  assert.equal(track.shotId, '');
  assert.equal(track.offset, 0);
  assert.deepEqual(trackPlacement(track, timing), { start: 0, end: 3 }); // normal speed now
  setTrackFade(p, 'ch', 'm', 'in', 1);
  setTrackFade(p, 'ch', 'm', 'out', 1);
  assert.equal(music.fade_in_s, 1);
  assert.throws(() => setTrackFade(p, 'ch', 'm', 'out', 1.5), /fadeTooLong/);
  setTrackFade(p, 'ch', 'm', 'in', 0);
  assert.equal('fade_in_s' in music, false);
});

test('a detached clip sound and a speed binding keep the story valid for saving', () => {
  const p = story();
  p.layout = { positions: {}, viewport: { x: 0, y: 0, zoom: 1 } };
  trimClip(p, 'C', 0, 3, 5);
  setClipSpeed(p, 'C', 0.75, 5);
  const generatedFrom = {
    jobId: 'j',
    videoEntityId: 'v-C',
    videoArtifactId: 'va',
    audioArtifactId: 'aa',
  };
  const track = {
    id: 's',
    assetId: 'a',
    fileId: 'f',
    role: 'generated',
    shotId: 'C',
    offset: 0,
    start: 0,
    end: 3,
    duration: 5,
    gain: 0.7,
    muted: false,
    generatedFrom,
  };
  p.journey = { soundTracks: { ch: [track] }, filmCuts: { ch: { clip_sound_crossfade_s: 0.5 } } };
  assert.equal(validateProject(p).ok, true, validateProject(p).error);
  detachClipSound(p, 'ch', 's', chapterTiming(chapterShots(p, 'ch')));
  assert.equal(validateProject(p).ok, true, validateProject(p).error);
  // A coupled track still may not drift from its shot's start.
  p.journey.soundTracks.ch.push({ ...track, id: 's2', detached: false, shotId: 'C', offset: 1 });
  assert.equal(validateProject(p).ok, false);
});

test('a speed binding sets the length even when seconds are stale or the file is unreadable', () => {
  // Shaped like a story edited through the API: seconds still hold the pre-speed values.
  const p = story();
  for (const [id, start, end, rate] of [
    ['C', 0, 3, 0.75],
    ['A', 3, 5, 2],
    ['D', 5, 7, 0.5],
  ]) {
    const shot = p.entities.find((e) => e.id === id),
      video = p.entities.find((e) => e.id === 'v-' + id);
    shot.data.seconds = end - start;
    shot.data.selectedVideoRange = { ...videoBinding(video), start, end, playback_rate: rate };
  }
  p.entities.find((e) => e.id === 'v-C').data.missingFile = true;
  assert.deepEqual(
    editorClips(p, 'ch').map((c) => c.frames),
    [96, 24, 96],
  );
  assert.deepEqual(
    chapterTiming(chapterShots(p, 'ch')).map((s) => [s.start, s.end]),
    [
      [0, 4],
      [4, 5],
      [5, 9],
    ],
  );
});
