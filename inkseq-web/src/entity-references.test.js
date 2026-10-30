import test from 'node:test';
import assert from 'node:assert/strict';
import { createStore, validateProject } from './store.js';
import { deletionImpact } from './entity-references.js';
import { deleteLook, effectiveLook } from './character-model.js';

function fixture() {
  const entries = new Map(),
    store = createStore({
      storage: {
        getItem: (key) => entries.get(key) ?? null,
        setItem: (key, value) => entries.set(key, value),
      },
    });
  store.newProject('引用删除验收');
  const c = store.addEntity('chapter'),
    s = store.addEntity('scene', c),
    shot = store.addEntity('shot', s),
    image = store.addEntity('image'),
    audio = store.addEntity('audio'),
    character = store.addEntity('character'),
    video = store.addEntity('video');
  store.editProject((p) => {
    const find = (id) => p.entities.find((e) => e.id === id);
    find(character).data.looks = [
      { id: 'rain', name: '雨衣', gallery: { front: image }, version: 1 },
      { id: 'office', name: '便服', gallery: {}, version: 1 },
    ];
    find(s).data.cast = [{ characterId: character, lookId: 'rain' }];
    find(shot).data = {
      ...find(shot).data,
      selectedAssetId: video,
      h3: {
        guides: [{ media_id: image, time_seconds: 0, use_audio: false }],
        video_audio: { [video]: false },
      },
    };
    p.journey = {
      sound: { mode: 'music' },
      soundTracks: {
        [c]: [
          { id: 'track', assetId: audio, shotId: shot, offset: 1, start: 0, end: 2, gain: 0.5 },
        ],
      },
      delivery: { coverAssetId: image },
    };
    p.jobs = [{ id: 'historical', input: { image, character, audio, shot } }];
  });
  store.enterCloudProject(store.exportProject(), { account: 'test-owner', version: 1 });
  return { store, c, s, shot, image, audio, character, video };
}
test('deleting gallery image detaches live references and undo restores the complete cloud draft', () => {
  const f = fixture(),
    before = f.store.exportProject(),
    impact = deletionImpact(before, f.image);
  assert.equal(impact.gallery, 1);
  assert.equal(impact.guides, 1);
  assert.equal(impact.cover, 1);
  assert.equal(f.store.deleteEntity(f.image), true);
  const p = f.store.exportProject(),
    character = p.entities.find((e) => e.id === f.character),
    shot = p.entities.find((e) => e.id === f.shot);
  assert.equal(character.data.looks[0].gallery.front, '');
  assert.deepEqual(shot.data.h3.guides, []);
  assert.equal(p.journey.delivery.coverAssetId, '');
  assert.equal(shot.status, 'review');
  assert.equal(shot.data.selectedAssetId, f.video);
  assert.deepEqual(p.jobs, before.jobs);
  assert.equal(validateProject(p).ok, true);
  assert.equal(f.store.getState().workspace.dirty, true);
  f.store.undo();
  assert.deepEqual(f.store.exportProject(), before);
  f.store.redo();
  assert.deepEqual(f.store.exportProject(), p);
});
test('deleting a cast character clears scene bindings and marks inherited shots for review', () => {
  const f = fixture(),
    before = f.store.exportProject();
  assert.equal(deletionImpact(before, f.character).cast, 1);
  f.store.deleteEntity(f.character);
  const p = f.store.exportProject();
  assert.deepEqual(p.entities.find((e) => e.id === f.s).data.cast, []);
  assert.equal(p.entities.find((e) => e.id === f.shot).status, 'review');
  assert.ok(p.entities.some((e) => e.id === f.image));
  f.store.undo();
  assert.deepEqual(f.store.exportProject(), before);
});
test('deleting audio or shot keeps broken track bindings for explicit repair; deleting chapter is undoable', () => {
  for (const key of ['audio', 'shot', 'c']) {
    const f = fixture(),
      before = f.store.exportProject();
    assert.equal(deletionImpact(before, f[key]).tracks, 1);
    f.store.deleteEntity(f[key]);
    const p = f.store.exportProject();
    if (key === 'c') assert.equal(p.journey.soundTracks[f.c], undefined);
    else assert.deepEqual(p.journey.soundTracks[f.c], before.journey.soundTracks[f.c]);
    assert.deepEqual(p.jobs, before.jobs);
    if (key !== 'audio') assert.ok(p.entities.some((e) => e.id === f.audio));
    f.store.undo();
    assert.deepEqual(f.store.exportProject(), before);
  }
});
test('look replacement is one undoable edit preserving cloud candidates and clean references', () => {
  const f = fixture(),
    before = f.store.exportProject();
  f.store.editProject((p) => deleteLook(p, f.character, 'rain', 'office').ok);
  const p = f.store.exportProject(),
    shot = p.entities.find((e) => e.id === f.shot);
  assert.equal(effectiveLook(p, shot, f.character).lookId, 'office');
  assert.equal(shot.data.selectedAssetId, f.video);
  assert.equal(shot.status, 'review');
  assert.equal(validateProject(p).ok, true);
  f.store.undo();
  assert.deepEqual(f.store.exportProject(), before);
});
