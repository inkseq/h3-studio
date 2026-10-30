import test from 'node:test';
import assert from 'node:assert/strict';
import { createStore, validateProject } from './store.js';
import { bindLocation, setLocationReferences, locationShots } from './location-model.js';
import {
  referenceSpecs,
  effectiveReferenceCount,
  buildPlanPayload,
  selectedRecipe,
  shotSnapshot,
} from './cloud-model.js';
import {
  createEntityWithMedia,
  uploadLocationImage,
  uploadCharacterMainImage,
  uploadLibraryFiles,
  uploadFreestyleFiles,
} from './media-operations.js';
import { storyboardPrompt } from './shot-prompt.js';

const deferred = () => {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
};
const receipt = () => ({
  type: 'image',
  data: {
    fileId: 'uploaded-file',
    cloudAssetId: 'uploaded-asset',
    mime: 'image/png',
    fileName: 'test.png',
    bytes: 12,
    missingFile: false,
  },
});
const file = () => new File(['test-image'], 'test.png', { type: 'image/png' });
function fixture() {
  const entries = new Map(),
    store = createStore({
      storage: {
        getItem: (key) => entries.get(key) || null,
        setItem: (key, value) => entries.set(key, value),
      },
    });
  store.newProject('场景媒体验收');
  const chapter = store.addEntity('chapter'),
    scene = store.addEntity('scene', chapter),
    first = store.addEntity('shot', scene, {
      description: '镜头前进',
      data: { h3: { inputMode: 'ref' } },
    }),
    second = store.addEntity('shot', scene, { description: '镜头停下' }),
    location = store.addEntity('location', null, {
      title: '雨夜车站',
      description: '旧绿色墙面与长廊',
    }),
    other = store.addEntity('location', null, { title: '房间' }),
    image = store.addEntity('image', null, {
      data: { fileId: 'image-file', cloudAssetId: 'cloud-image' },
    }),
    otherImage = store.addEntity('image', null, {
      data: { fileId: 'other-image-file', cloudAssetId: 'cloud-other-image' },
    });
  store.editProject((project) => {
    setLocationReferences(project, location, [image]);
    setLocationReferences(project, other, [otherImage]);
    bindLocation(project, scene, location);
    bindLocation(project, second, other);
  });
  return { store, chapter, scene, first, second, location, other, image, otherImage };
}
const entity = (fixture, id) =>
  fixture.store.getState().project.entities.find((entity) => entity.id === id);

test('scene location supplies real uploaded images and shot override supplies its own location', () => {
  const f = fixture(),
    project = f.store.exportProject();
  assert.deepEqual(
    referenceSpecs(project, f.first).references.map((ref) => ref.entity.id),
    [f.image],
  );
  assert.deepEqual(
    referenceSpecs(project, f.second).references.map((ref) => ref.entity.id),
    [f.otherImage],
  );
  assert.deepEqual(
    locationShots(project, f.location).map((shot) => shot.id),
    [f.first],
  );
  assert.match(storyboardPrompt(project, f.first), /雨夜车站：旧绿色墙面/);
  assert.doesNotMatch(storyboardPrompt(project, f.second), /雨夜车站/);
  const payload = buildPlanPayload(project, f.first, {
    recipe: { id: 'h3-ref', mode: 'ref', controls: { duration: { type: 'number', default: 5 } } },
    capabilitiesVersion: 'test',
  });
  assert.deepEqual(payload.inputs.images, [{ asset_id: 'cloud-image', purpose: 'reference' }]);
});

test('location references deduplicate direct inputs; updating its image invalidates preflight and review', () => {
  const f = fixture();
  f.store.addLink(f.image, f.first, 'reference');
  const before = shotSnapshot(f.store.exportProject(), f.first);
  assert.equal(referenceSpecs(f.store.exportProject(), f.first).references.length, 1);
  f.store.updateEntity(f.first, { status: 'ready' });
  f.store.updateEntity(f.second, { status: 'ready' });
  f.store.updateEntity(f.image, {
    data: { fileId: 'replaced-file', cloudAssetId: 'new-cloud-image' },
  });
  assert.equal(entity(f, f.first).status, 'review');
  assert.equal(entity(f, f.second).status, 'ready');
  assert.notDeepEqual(shotSnapshot(f.store.exportProject(), f.first), before);
});

test('switching a scene location changes inherited shots while preserving explicit overrides and undo', () => {
  const f = fixture(),
    before = f.store.exportProject();
  let result;
  f.store.editProject((project) => {
    result = bindLocation(project, f.scene, f.other);
    return result.ok;
  });
  assert.deepEqual(result.affected, [f.first]);
  assert.deepEqual(
    referenceSpecs(f.store.exportProject(), f.first).references.map((ref) => ref.entity.id),
    [f.otherImage],
  );
  assert.equal(entity(f, f.second).data.locationId, f.other);
  f.store.undo();
  assert.deepEqual(f.store.exportProject(), before);
});

test('deleting a location or its reference leaves valid data, preserves media and is undoable', () => {
  for (const field of ['location', 'image']) {
    const f = fixture(),
      before = f.store.exportProject();
    assert.equal(f.store.deleteEntity(f[field]), true);
    const project = f.store.exportProject();
    assert.equal(validateProject(project).ok, true);
    assert.equal(entity(f, f.first).status, 'review');
    if (field === 'location') {
      assert.equal(entity(f, f.scene).data.locationId, '');
      assert.ok(entity(f, f.image));
    } else assert.deepEqual(entity(f, f.location).data.referenceAssetIds, []);
    f.store.undo();
    assert.deepEqual(f.store.exportProject(), before);
  }
});

test('new character/location with media is one atomic undoable action and uses actual file identity', async () => {
  for (const kind of ['character', 'location']) {
    const f = fixture(),
      before = f.store.exportProject(),
      id = await createEntityWithMedia({
        store: f.store,
        kind,
        title: '新参考',
        file: file(),
        upload: async () => receipt(),
      });
    const owner = entity(f, id),
      project = f.store.exportProject(),
      assetId =
        kind === 'character' ? owner.data.looks[0].gallery.front : owner.data.referenceAssetIds[0];
    assert.equal(entity(f, assetId).data.fileId, 'uploaded-file');
    assert.equal(validateProject(project).ok, true);
    f.store.undo();
    assert.deepEqual(f.store.exportProject(), before);
  }
});

test('late direct creation and library upload cannot attach to another story', async () => {
  for (const operation of ['create', 'library']) {
    const f = fixture(),
      wait = deferred(),
      pending =
        operation === 'create'
          ? createEntityWithMedia({
              store: f.store,
              kind: 'location',
              title: 'late',
              file: file(),
              upload: () => wait.promise,
            })
          : uploadLibraryFiles({ store: f.store, files: [file()], upload: () => wait.promise }),
      rejected = assert.rejects(pending, /已经切换/);
    f.store.newProject('另一故事');
    wait.resolve(receipt());
    await rejected;
    assert.equal(f.store.getState().project.entities.length, 0);
  }
});

test('location upload merges current reference choices, but deleted location rejects late completion', async () => {
  const f = fixture(),
    wait = deferred(),
    pending = uploadLocationImage({
      store: f.store,
      locationId: f.location,
      file: file(),
      upload: () => wait.promise,
    });
  f.store.editProject((project) => setLocationReferences(project, f.location, [f.otherImage]).ok);
  wait.resolve(receipt());
  const id = await pending;
  assert.deepEqual(entity(f, f.location).data.referenceAssetIds, [f.otherImage, id]);
  const next = deferred(),
    count = f.store.getState().project.entities.length,
    late = uploadLocationImage({
      store: f.store,
      locationId: f.location,
      file: file(),
      upload: () => next.promise,
    }),
    rejected = assert.rejects(late, /地点已删除/);
  f.store.deleteEntity(f.location);
  next.resolve(receipt());
  await rejected;
  assert.equal(f.store.getState().project.entities.length, count - 1);
});

test('first character image creates a usable gallery without overwriting a concurrently added look', async () => {
  const f = fixture(),
    character = f.store.addEntity('character'),
    id = await uploadCharacterMainImage({
      store: f.store,
      characterId: character,
      file: file(),
      upload: async () => receipt(),
    });
  assert.equal(entity(f, character).data.looks[0].gallery.front, id);
  const second = f.store.addEntity('character'),
    wait = deferred(),
    pending = uploadCharacterMainImage({
      store: f.store,
      characterId: second,
      file: file(),
      upload: () => wait.promise,
    }),
    rejected = assert.rejects(pending, /造型已经改变/);
  f.store.updateEntity(second, {
    data: { looks: [{ id: 'manual', name: '手动造型', version: 1, gallery: {} }] },
  });
  wait.resolve(receipt());
  await rejected;
  assert.equal(entity(f, second).data.looks[0].id, 'manual');
});

test('local shot uploads work without cloud login and late mode changes reject attachment', async () => {
  const f = fixture(),
    args = {
      store: f.store,
      shotId: f.first,
      files: [file()],
      kind: 'image',
      role: 'reference',
      maximum: 9,
    };
  await uploadFreestyleFiles({ ...args, upload: async () => receipt() });
  assert.ok(
    f.store
      .getState()
      .project.links.some(
        (link) =>
          link.target === f.first && entity(f, link.source)?.data.fileId === 'uploaded-file',
      ),
  );
  const wait = deferred(),
    pending = uploadFreestyleFiles({ ...args, upload: () => wait.promise }),
    rejected = assert.rejects(pending, /生成方式已改变/);
  f.store.updateEntity(f.first, { data: { h3: { inputMode: 'fl' } } });
  wait.resolve(receipt());
  await rejected;
});

test('draft mode selects the same real recipe after cloud copy and unavailable recipe never silently falls back', () => {
  const recipes = [
    { id: 'real-fl', mode: 'fl' },
    { id: 'real-ref', mode: 'ref' },
  ];
  assert.equal(selectedRecipe(recipes, { inputMode: 'ref' }).id, 'real-ref');
  assert.equal(selectedRecipe(recipes, { recipeId: 'removed', inputMode: 'ref' }), undefined);
  assert.equal(selectedRecipe([recipes[0]], { inputMode: 'ref' }), undefined);
  assert.equal(selectedRecipe(recipes).id, 'real-fl');
});

test('ordinary image capacity counts inherited references once and keeps frames and reusable guides separate', () => {
  const f = fixture();
  f.store.addLink(f.image, f.first, 'reference');
  f.store.addLink(f.otherImage, f.first, 'firstFrame');
  f.store.updateEntity(f.first, {
    data: {
      h3: {
        inputMode: 'ref',
        guides: [{ media_id: f.otherImage, time_seconds: 0, use_audio: false }],
      },
    },
  });
  assert.equal(effectiveReferenceCount(f.store.exportProject(), f.first, 'image'), 1);
  const characterImage = f.store.addEntity('image', null, { data: { fileId: 'character-file' } }),
    character = f.store.addEntity('character', null, {
      data: {
        looks: [{ id: 'look', name: '造型', version: 1, gallery: { front: characterImage } }],
      },
    });
  f.store.updateEntity(f.scene, { data: { cast: [{ characterId: character, lookId: 'look' }] } });
  assert.equal(effectiveReferenceCount(f.store.exportProject(), f.first, 'image'), 2);
});

test('upload quota includes inherited images before transfer and rechecks changed inheritance on completion', async () => {
  const f = fixture(),
    args = {
      store: f.store,
      shotId: f.first,
      files: [file()],
      kind: 'image',
      role: 'reference',
      countAllReferences: true,
      maximum: 1,
    };
  let uploads = 0;
  await assert.rejects(
    uploadFreestyleFiles({
      ...args,
      upload: async () => {
        uploads++;
        return receipt();
      },
    }),
    /含人物与地点/,
  );
  assert.equal(uploads, 0);
  const wait = deferred(),
    before = f.store.exportProject().entities.length,
    pending = uploadFreestyleFiles({ ...args, maximum: 2, upload: () => wait.promise }),
    rejected = assert.rejects(pending, /此区域最多 2/);
  f.store.editProject(
    (project) => setLocationReferences(project, f.location, [f.image, f.otherImage]).ok,
  );
  wait.resolve(receipt());
  await rejected;
  assert.equal(f.store.exportProject().entities.length, before);
  assert.equal(effectiveReferenceCount(f.store.exportProject(), f.first, 'image'), 2);
});

test('FL keeps scene cast/location assignments without sending their shared gallery; explicit incompatible refs still fail', () => {
  const f = fixture(),
    before = f.store.exportProject(),
    recipe = { id: 'h3-fl', mode: 'fl', controls: { duration: { type: 'number', default: 5 } } };
  const payload = buildPlanPayload(before, f.first, { recipe, capabilitiesVersion: 'test' });
  assert.deepEqual(payload.inputs.images, []);
  assert.deepEqual(f.store.exportProject(), before);
  f.store.addLink(f.image, f.first, 'reference');
  assert.throws(
    () =>
      buildPlanPayload(f.store.exportProject(), f.first, { recipe, capabilitiesVersion: 'test' }),
    /不能混用全能参考/,
  );
  f.store.updateEntity(f.first, { data: { h3: { inputMode: 'fl' } } });
  assert.equal(referenceSpecs(f.store.exportProject(), f.first).references.length, 1);
  assert.equal(entity(f, f.scene).data.locationId, f.location);
});
