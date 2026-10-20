import test from 'node:test';
import assert from 'node:assert/strict';
import { chapterShots } from './storyboard-domain.js';
import {
  cutFrames,
  editorClips,
  includedShotIds,
  moveShot,
  setIncluded,
  trimClip,
} from './film-edit-model.js';

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
  const entities = [
    node('ch', 'chapter', null, 1),
    node('sa', 'scene', 'ch', 1),
    node('sb', 'scene', 'ch', 2),
  ];
  // a1, a2 in scene A; b1 in scene B. Each shot has a 5 s video selected.
  for (const [shot, scene, order] of [
    ['a1', 'sa', 1],
    ['a2', 'sa', 2],
    ['b1', 'sb', 1],
  ]) {
    entities.push(
      node('v-' + shot, 'video', null, 0, {
        fileId: 'file-' + shot,
        mime: 'video/mp4',
        metadata: { duration: 5 },
      }),
      node(shot, 'shot', scene, order, { seconds: 4, selectedAssetId: 'v-' + shot }),
    );
  }
  return { schemaVersion: 4, id: 'p', title: 't', logline: '', entities, links: [], jobs: [] };
}
const order = (p) => chapterShots(p, 'ch').map((s) => s.id);

test('clips follow story order with 24 fps timing', () => {
  const clips = editorClips(story(), 'ch');
  assert.deepEqual(
    clips.map((c) => [c.id, c.frames, c.from, c.included]),
    [
      ['a1', 96, 0, true],
      ['a2', 96, 96, true],
      ['b1', 96, 192, true],
    ],
  );
  assert.equal(cutFrames(clips), 288);
});

test('moving a shot rewrites scene/shot order the exporter reads', () => {
  const p = story();
  moveShot(p, 'ch', 'b1', 0);
  assert.deepEqual(order(p), ['b1', 'a1', 'a2']);
  // b1 joined scene A because its new neighbour is there.
  assert.equal(p.entities.find((e) => e.id === 'b1').parentId, 'sa');
  moveShot(p, 'ch', 'a1', 2);
  assert.deepEqual(order(p), ['b1', 'a2', 'a1']);
});

test('excluding keeps the shot and stores an ordered inclusion list', () => {
  const p = story();
  setIncluded(p, 'ch', 'a2', false);
  assert.deepEqual(p.journey.filmCuts.ch.shotIds, ['a1', 'b1']);
  assert.ok(p.entities.some((e) => e.id === 'a2'));
  const clips = editorClips(p, 'ch');
  assert.equal(clips.find((c) => c.id === 'b1').from, 96);
  assert.equal(cutFrames(clips), 192);
  // Reordering keeps the inclusion list in the new order.
  moveShot(p, 'ch', 'b1', 0);
  assert.deepEqual(p.journey.filmCuts.ch.shotIds, ['b1', 'a1']);
  // Putting everything back removes the entry (today's default).
  setIncluded(p, 'ch', 'a2', true);
  assert.equal(p.journey.filmCuts, undefined);
  assert.deepEqual(includedShotIds(p, 'ch'), order(p));
  assert.throws(() => {
    const q = story();
    for (const id of ['a1', 'a2', 'b1']) setIncluded(q, 'ch', id, false);
  }, /editor.keepOne/);
});

test('trimming binds the source range and the cut length follows it', () => {
  const p = story();
  trimClip(p, 'a1', 1, 3.5, 5);
  const shot = p.entities.find((e) => e.id === 'a1');
  assert.equal(shot.data.selectedVideoRange.start, 1);
  assert.equal(shot.data.selectedVideoRange.assetId, 'v-a1');
  assert.equal(shot.data.seconds, 2.5);
  const clip = editorClips(p, 'ch')[0];
  assert.deepEqual([clip.startFrame, clip.frames], [24, 60]);
  assert.throws(() => trimClip(p, 'a1', 2, 9, 5));
});
