import test from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from './store.js';
import { uploadFile, setCloudMediaHandlers, nativeMediaSource, getFile } from './media.js';
import { captureWorkspace, assertWorkspace } from './workspace-context.js';
import { uploadLookImage, uploadFreestyleFiles } from './media-operations.js';

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((a, b) => {
    resolve = a;
    reject = b;
  });
  return { promise, resolve, reject };
};
function fixture({ cloud = false } = {}) {
  const values = new Map(),
    storage = { getItem: (k) => values.get(k) || null, setItem: (k, v) => values.set(k, v) },
    store = createStore({ storage });
  store.newProject('上传延迟测试');
  const chapter = store.addEntity('chapter'),
    scene = store.addEntity('scene', chapter),
    shot = store.addEntity('shot', scene, { data: { h3: { recipeId: 'fl' } } }),
    image = store.addEntity('image', null, { data: { fileId: 'old-file', note: '旧备注' } }),
    other = store.addEntity('image', null, { data: { fileId: 'other-file' } }),
    character = store.addEntity('character', null, {
      data: { looks: [{ id: 'look', name: '外套', version: 1, gallery: { front: image } }] },
    });
  if (cloud) store.enterCloudProject(store.exportProject(), { account: 'superdan', version: 1 });
  return { store, values, chapter, scene, shot, image, other, character };
}
const file = () => new File(['test-image-bytes'], 'local-only.png', { type: 'image/png' });
const uploaded = () => ({
  type: 'image',
  data: {
    fileId: 'new-file',
    fileName: 'new.png',
    mime: 'image/png',
    bytes: 16,
    source: 'upload',
    missingFile: false,
  },
});
const entity = (h, id) => h.store.getState().project.entities.find((e) => e.id === id);
test('native view resolver never replaces explicit byte reads and cloud rejection has no local fallback', async () => {
  let reads = 0,
    resolutions = 0;
  const source = Object.freeze({
    href: '/v1/assets/source/content?expected_account=superdan',
    assertCurrent() {},
  });
  setCloudMediaHandlers({
    read: async () => {
      reads++;
      return new Blob(['original bytes']);
    },
    native: () => {
      resolutions++;
      return source;
    },
  });
  try {
    assert.equal(nativeMediaSource('local-file'), null);
    assert.equal(resolutions, 0);
    assert.equal(nativeMediaSource('cloud_asset_source'), source);
    assert.equal(reads, 0);
    assert.equal(await (await getFile('cloud_asset_source')).text(), 'original bytes');
    assert.equal(reads, 1);
    assert.throws(() => nativeMediaSource('cloud_asset_../foreign'), /client.mediaPath/);
    assert.equal(resolutions, 1);
    setCloudMediaHandlers({
      read: async () => {
        reads++;
        return new Blob(['must not fall back']);
      },
      native: () => {
        throw Error('source context changed');
      },
    });
    assert.throws(() => nativeMediaSource('cloud_asset_source'), /source context changed/);
    assert.equal(reads, 1);
    setCloudMediaHandlers();
    assert.throws(() => nativeMediaSource('cloud_asset_source'), /client.mediaAccount/);
  } finally {
    setCloudMediaHandlers();
  }
});
test('audio guide upload binds its own audio without requesting a nonexistent original video track', async () => {
  const h = fixture();
  await uploadFreestyleFiles({
    store: h.store,
    shotId: h.shot,
    files: [new File(['audio'], 'source.wav', { type: 'audio/wav' })],
    guide: true,
    maximum: 1,
    upload: async () => ({
      type: 'audio',
      data: { fileId: 'audio-file', mime: 'audio/wav', bytes: 5 },
    }),
  });
  const guide = entity(h, h.shot).data.h3.guides[0];
  assert.equal(guide.use_audio, false);
  assert.equal(
    h.store.getState().project.entities.find((item) => item.id === guide.media_id).type,
    'audio',
  );
});
test('late guided upload cannot join a changed deployment profile even when FL/REF and recipe stay unchanged', async () => {
  const h = fixture(),
    wait = deferred();
  h.store.updateEntity(h.shot, { data: { h3: { recipeId: 'fl', deploymentProfileId: 'old' } } });
  const pending = uploadFreestyleFiles({
      store: h.store,
      shotId: h.shot,
      files: [file()],
      kind: 'image',
      role: 'firstFrame',
      maximum: 1,
      upload: () => wait.promise,
    }),
    rejected = assert.rejects(pending, /方式已改变/);
  h.store.updateEntity(h.shot, { data: { h3: { recipeId: 'fl', deploymentProfileId: 'new' } } });
  wait.resolve(uploaded());
  await rejected;
  assert.equal(
    h.store
      .getState()
      .project.links.some((link) => link.target === h.shot && link.role === 'firstFrame'),
    false,
  );
  assert.equal(entity(h, h.shot).data.h3.deploymentProfileId, 'new');
});

test('workspace visits invalidate pending work across same-ID import and cloud return, never ordinary edits', () => {
  const h = fixture(),
    ctx = captureWorkspace(h.store),
    original = h.store.exportProject();
  h.store.updateEntity(h.image, { description: 'new description' });
  assert.doesNotThrow(() => assertWorkspace(h.store, ctx));
  h.store.replaceProject(original);
  assert.throws(() => assertWorkspace(h.store, ctx), /已经切换/);
  const next = captureWorkspace(h.store);
  h.store.enterCloudProject(original, { account: 'superdan', version: 1 });
  h.store.leaveCloudProject();
  assert.throws(() => assertWorkspace(h.store, next), /已经切换/);
  assert.ok(h.store.getState().workspaceEpoch > next.epoch);
  assert.equal(Object.hasOwn(h.store.exportProject(), 'workspaceEpoch'), false);
  assert.equal(
    Object.hasOwn(JSON.parse(h.values.get('yingxu-studio-v4')), 'workspaceEpoch'),
    false,
  );
});

for (const change of ['same ID import', 'different project', 'cloud entry', 'cloud roundtrip'])
  test(`delayed local media write retains Blob but refuses attachment after ${change}`, async () => {
    const h = fixture(),
      wait = deferred(),
      blobs = new Map(),
      original = h.store.exportProject();
    setCloudMediaHandlers();
    const pending = uploadFile(file(), 'image', {
      store: h.store,
      writeFile: async (id, blob) => {
        blobs.set(id, blob);
        await wait.promise;
      },
    });
    const rejected = assert.rejects(pending, /已经切换/);
    if (change === 'same ID import') h.store.replaceProject(original);
    if (change === 'different project') h.store.newProject('新项目');
    if (change.startsWith('cloud')) {
      h.store.enterCloudProject(original, { account: 'superdan', version: 1 });
      if (change === 'cloud roundtrip') h.store.leaveCloudProject();
    }
    wait.resolve();
    await rejected;
    assert.equal(blobs.size, 1);
    assert.equal(await [...blobs.values()][0].text(), 'test-image-bytes');
    assert.equal(
      h.store.getState().project.entities.some((e) => e.data.fileName === 'local-only.png'),
      false,
    );
  });

test('delayed cloud upload is rejected after same project enters another account; no local fallback', async () => {
  const h = fixture({ cloud: true }),
    wait = deferred();
  let localWrites = 0;
  setCloudMediaHandlers({ upload: () => wait.promise });
  try {
    const pending = uploadFile(file(), 'image', {
        store: h.store,
        writeFile: async () => localWrites++,
      }),
      rejected = assert.rejects(pending, /已经切换/);
    h.store.enterCloudProject(h.store.exportProject(), { account: 'supervan', version: 1 });
    wait.resolve(uploaded());
    await rejected;
    assert.equal(localWrites, 0);
  } finally {
    setCloudMediaHandlers();
  }
  await assert.rejects(
    uploadFile(file(), 'image', { store: h.store, writeFile: async () => localWrites++ }),
    /云上传尚未就绪/,
  );
  assert.equal(localWrites, 0);
});

test('gallery manual selection wins over delayed upload; next explicit upload can still work', async () => {
  const h = fixture(),
    wait = deferred(),
    before = h.store.getState().project.entities.length,
    pending = uploadLookImage({
      store: h.store,
      characterId: h.character,
      lookId: 'look',
      slot: 'front',
      file: file(),
      upload: () => wait.promise,
    }),
    rejected = assert.rejects(pending, /已另选/);
  h.store.editProject((p) => {
    p.entities.find((e) => e.id === h.character).data.looks[0].gallery.front = h.other;
  });
  wait.resolve(uploaded());
  await rejected;
  assert.equal(entity(h, h.character).data.looks[0].gallery.front, h.other);
  assert.equal(h.store.getState().project.entities.length, before);
  const fresh = await uploadLookImage({
    store: h.store,
    characterId: h.character,
    lookId: 'look',
    slot: 'front',
    file: file(),
    upload: async () => uploaded(),
  });
  assert.equal(entity(h, h.character).data.looks[0].gallery.front, fresh);
});

test('freestyle rechecks live slot capacity after transfer without adding an orphan entity or overwriting manual input', async () => {
  const h = fixture({ cloud: true }),
    wait = deferred(),
    count = h.store.getState().project.entities.length;
  const pending = uploadFreestyleFiles({
      store: h.store,
      shotId: h.shot,
      files: [file()],
      kind: 'image',
      role: 'firstFrame',
      maximum: 1,
      upload: () => wait.promise,
    }),
    rejected = assert.rejects(pending, /最多 1 份/);
  h.store.addLink(h.image, h.shot, 'firstFrame');
  wait.resolve(uploaded());
  await rejected;
  assert.equal(h.store.getState().project.entities.length, count);
  assert.equal(
    h.store.getState().project.links.find((l) => l.role === 'firstFrame').source,
    h.image,
  );
});

test('freestyle recipe change rejects pending input and accepts a fresh retry in current recipe', async () => {
  const h = fixture({ cloud: true }),
    wait = deferred(),
    args = {
      store: h.store,
      shotId: h.shot,
      files: [file()],
      kind: 'image',
      role: 'reference',
      maximum: 2,
    };
  const pending = uploadFreestyleFiles({ ...args, upload: () => wait.promise }),
    rejected = assert.rejects(pending, /生成方式已改变/);
  h.store.updateEntity(h.shot, { data: { h3: { recipeId: 'ref' } } });
  wait.resolve(uploaded());
  await rejected;
  await uploadFreestyleFiles({ ...args, upload: async () => uploaded() });
  const added = h.store.getState().project.entities.filter((e) => e.data.fileId === 'new-file');
  assert.equal(added.length, 1);
  assert.equal(
    h.store.getState().project.links.some((l) => l.source === added[0].id && l.target === h.shot),
    true,
  );
});

test('freestyle rejects an oversized drop before any transfer and attaches a valid multi-drop atomically per file', async () => {
  const h = fixture({ cloud: true });
  let calls = 0;
  const args = {
    store: h.store,
    shotId: h.shot,
    kind: 'image',
    role: 'reference',
    maximum: 2,
    upload: async () => {
      calls++;
      return { ...uploaded(), data: { ...uploaded().data, fileId: `file-${calls}` } };
    },
  };
  await assert.rejects(
    uploadFreestyleFiles({ ...args, files: [file(), file(), file()] }),
    /最多 2 份/,
  );
  assert.equal(calls, 0);
  await uploadFreestyleFiles({ ...args, files: [file(), file()] });
  assert.equal(calls, 2);
  assert.equal(
    h.store.getState().project.links.filter((l) => l.target === h.shot && l.role === 'reference')
      .length,
    2,
  );
  h.store.undo();
  assert.equal(
    h.store.getState().project.links.filter((l) => l.target === h.shot && l.role === 'reference')
      .length,
    1,
  );
  assert.equal(
    h.store.getState().project.entities.some((e) => e.data.fileId === 'file-2'),
    false,
  );
});
