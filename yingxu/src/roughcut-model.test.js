import test from 'node:test';
import assert from 'node:assert/strict';
import { roughCutSnapshot, roughCutSummary, roughCutJobs } from './roughcut-model.js';
import { stableJSON } from './cloud-model.js';

const entity = (id, type, parentId, order = 0, data = {}) => ({
  id,
  type,
  title: id,
  parentId,
  order,
  version: 1,
  description: '',
  data,
});
function project() {
  return {
    id: 'p',
    entities: [
      entity('c', 'chapter', null),
      entity('s2', 'scene', 'c', 1),
      entity('s1', 'scene', 'c', 0),
      entity('b', 'shot', 's2', 0, { seconds: 4, selectedAssetId: 'v2' }),
      entity('a', 'shot', 's1', 0, { seconds: 6, selectedAssetId: 'v1' }),
      entity('v1', 'video', null, 0, {
        fileId: 'cloud_artifact_a1',
        cloudArtifactId: 'a1',
        metadata: { duration: 6 },
        simulation: true,
      }),
      entity('v2', 'video', null, 0, {
        fileId: 'cloud_asset_a2',
        cloudAssetId: 'a2',
        metadata: { duration: 4 },
      }),
      entity('music', 'audio', null, 0, {
        fileId: 'cloud_asset_m',
        cloudAssetId: 'm',
        metadata: { duration: 20 },
      }),
      entity('person', 'character', null),
    ],
    links: [],
    journey: {
      brief: { aspect: '9:16' },
      sound: { mode: 'music' },
      soundTracks: {
        c: [
          {
            id: 't',
            assetId: 'music',
            fileId: 'cloud_asset_m',
            role: 'music',
            shotId: 'b',
            offset: 1,
            start: 2,
            end: 5,
            gain: 0.5,
            muted: false,
            duration: 20,
          },
        ],
      },
    },
  };
}

test('rough-cut summary preserves story order, audio source offset and simulation evidence', () => {
  const p = project(),
    before = structuredClone(p),
    summary = roughCutSummary(p, 'c');
  assert.deepEqual(
    summary.shots.map((s) => [s.id, s.start, s.end]),
    [
      ['a', 0, 6],
      ['b', 6, 10],
    ],
  );
  assert.equal(summary.total, 10);
  assert.deepEqual(summary.tracks[0].placement, { start: 7, end: 10 });
  assert.equal(summary.tracks[0].start, 2);
  assert.equal(summary.simulation, true);
  assert.deepEqual(p, before);
});

test('snapshot invalidates cut inputs while ignoring unrelated story text', () => {
  const p = project(),
    initial = stableJSON(roughCutSnapshot(p, 'c', { fps: 24 }));
  p.entities.find((e) => e.id === 'person').description = 'New character note';
  assert.equal(stableJSON(roughCutSnapshot(p, 'c', { fps: 24 })), initial);
  for (const modify of [
    (q) => (q.entities.find((e) => e.id === 'a').data.seconds = 5),
    (q) => (q.entities.find((e) => e.id === 'a').data.selectedAssetId = 'v2'),
    (q) => (q.entities.find((e) => e.id === 'v1').data.fileId = 'replacement'),
    (q) => (q.entities.find((e) => e.id === 's1').order = 3),
    (q) => (q.journey.soundTracks.c[0].gain = 0.8),
    (q) => (q.journey.delivery = { aspect: '16:9' }),
  ]) {
    const q = structuredClone(p);
    modify(q);
    assert.notEqual(stableJSON(roughCutSnapshot(q, 'c', { fps: 24 })), initial);
  }
  assert.notEqual(stableJSON(roughCutSnapshot(p, 'c', { fps: 30 })), initial);
});

test('silent mode excludes stored tracks without deleting them and chapter jobs do not become shot jobs', () => {
  const p = project();
  p.journey.sound.mode = 'silent';
  assert.deepEqual(roughCutSnapshot(p, 'c').tracks, []);
  assert.equal(p.journey.soundTracks.c.length, 1);
  const base = {
    recipe_id: 'chapter-roughcut-v1',
    client_ref: { project_id: 'p', chapter_id: 'c' },
  };
  assert.deepEqual(
    roughCutJobs(
      [
        { ...base, id: 'right' },
        { ...base, id: 'other', client_ref: { project_id: 'other', chapter_id: 'c' } },
        { ...base, id: 'shot', recipe_id: 'h3-base-ref2va-v1' },
      ],
      'p',
      'c',
    ).map((j) => j.id),
    ['right'],
  );
});
