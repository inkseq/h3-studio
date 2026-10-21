import test from 'node:test';
import assert from 'node:assert/strict';
import {
  addAudioTrack,
  audioBlocks,
  moveTrack,
  removeTrack,
  setTrack,
  trimTrack,
} from './film-audio-model.js';

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
function story() {
  return {
    entities: [
      node('ch', 'chapter', null, 1),
      node('sa', 'scene', 'ch', 1),
      node('a1', 'shot', 'sa', 1, { seconds: 4 }),
      node('a2', 'shot', 'sa', 2, { seconds: 4 }),
      node('music', 'audio', null, 0, { fileId: 'file-music', metadata: { duration: 30 } }),
    ],
    journey: {},
  };
}

test('adding music places it at an absolute time and turns sound on', () => {
  const p = story();
  const track = addAudioTrack(p, 'ch', p.entities.at(-1), 2, 30);
  assert.equal(p.journey.sound.mode, 'mixed');
  const [block] = audioBlocks(p, 'ch');
  assert.equal(block.track.id, track.id);
  assert.deepEqual(block.placement, { start: 2, end: 32 });
  assert.equal(block.title, 'music');
});

test('move, trim at either edge, volume, mute and remove', () => {
  const p = story();
  const { id } = addAudioTrack(p, 'ch', p.entities.at(-1), 0, 30);
  moveTrack(p, 'ch', id, 3);
  assert.deepEqual(audioBlocks(p, 'ch')[0].placement, { start: 3, end: 33 });
  // Trimming the start keeps the remaining audio where it was on the film.
  trimTrack(p, 'ch', id, 'start', 5);
  let t = p.journey.soundTracks.ch[0];
  assert.equal(t.start, 5);
  assert.deepEqual(audioBlocks(p, 'ch')[0].placement, { start: 8, end: 33 });
  trimTrack(p, 'ch', id, 'end', -20);
  t = p.journey.soundTracks.ch[0];
  assert.equal(t.end, 10);
  trimTrack(p, 'ch', id, 'end', 100);
  assert.equal(p.journey.soundTracks.ch[0].end, 30);
  // A start trim that would place the audio before the film start is ignored.
  moveTrack(p, 'ch', id, 0);
  trimTrack(p, 'ch', id, 'start', -10);
  assert.equal(p.journey.soundTracks.ch[0].start, 5);
  setTrack(p, 'ch', id, { gain: 1.4, muted: true });
  assert.equal(p.journey.soundTracks.ch[0].gain, 1);
  assert.equal(p.journey.soundTracks.ch[0].muted, true);
  removeTrack(p, 'ch', id);
  assert.equal(p.journey.soundTracks.ch.length, 0);
});

test('a clip-following generated track cannot be moved or trimmed by hand', () => {
  const p = story();
  p.journey.soundTracks = {
    ch: [
      {
        id: 'g',
        assetId: 'music',
        fileId: 'file-music',
        shotId: 'a2',
        offset: 0,
        start: 0,
        end: 4,
        gain: 0.7,
        generatedFrom: { jobId: 'j' },
      },
    ],
  };
  assert.deepEqual(audioBlocks(p, 'ch')[0].placement, { start: 4, end: 8 });
  assert.throws(() => moveTrack(p, 'ch', 'g', 1), /audioFollowsClip/);
  assert.throws(() => trimTrack(p, 'ch', 'g', 'end', -1), /audioFollowsClip/);
});
