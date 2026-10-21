import test from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from './store.js';
import { createCloudController } from './cloud-controller.js';
import { CloudError } from './cloud-client.js';
import { installPersistenceGuard } from './persistence-guard.js';

function memory() {
  const data = new Map();
  let fail = false;
  return {
    data,
    get fail() {
      return fail;
    },
    set fail(value) {
      fail = value;
    },
    getItem: (key) => data.get(key) || null,
    setItem(key, value) {
      if (fail) throw Error('test quota');
      data.set(key, value);
    },
  };
}
function setup() {
  const storage = memory(),
    visits = memory(),
    store = createStore({ storage });
  store.newProject('本机原作');
  const local = store.exportProject(),
    remote = {
      id: 'resume-project',
      version: 3,
      project: { ...structuredClone(local), id: 'resume-project', title: '云作品' },
    };
  let owner = 'supervan',
    reads = 0;
  const api = {
    reset() {},
    setAccount() {},
    authConfig: async () => ({ authentication: 'username-only-test' }),
    me: async () => {
      if (!owner) throw new CloudError('expired', { status: 401 });
      return { username: owner };
    },
    login: async (username) => {
      owner = username;
      return { username };
    },
    logout: async () => {
      owner = null;
    },
    projects: async () => ({
      projects: [{ id: remote.id, title: remote.project.title, version: remote.version }],
    }),
    capabilities: async () => ({ execution_enabled: false }),
    project: async () => {
      reads++;
      return structuredClone(remote);
    },
    jobs: async () => ({ jobs: [] }),
  };
  const controllerFor = (s) =>
      createCloudController({
        store: s,
        client: api,
        storage,
        workspaceStorage: visits,
        mediaHandlers: () => {},
      }),
    controller = controllerFor(store);
  return {
    storage,
    visits,
    store,
    local,
    remote,
    api,
    controller,
    controllerFor,
    setOwner(value) {
      owner = value;
    },
    get reads() {
      return reads;
    },
  };
}
async function opened() {
  const h = setup();
  await h.controller.initialize();
  await h.controller.open(h.remote.id);
  return h;
}

test('same authenticated owner sees an explicit resume offer after reload without switching or modifying local work', async () => {
  const h = await opened(),
    before = h.storage.getItem('yingxu-studio-v4');
  h.controller.destroy();
  const refreshed = createStore({ storage: h.storage }),
    c = h.controllerFor(refreshed);
  await c.initialize();
  assert.equal(c.getState().account, 'supervan');
  assert.equal(c.getState().resumeOffer.projectId, h.remote.id);
  assert.equal(refreshed.getState().workspace.mode, 'local');
  assert.equal(refreshed.getState().project.id, h.local.id);
  assert.equal(h.storage.getItem('yingxu-studio-v4'), before);
  assert.equal(h.reads, 1);
  assert.equal(await c.resume(), true);
  assert.equal(refreshed.getState().project.id, h.remote.id);
  assert.equal(c.getState().resumeOffer, null);
  assert.equal(h.storage.getItem('yingxu-studio-v4'), before);
  c.destroy();
});
test('resuming a dirty draft on an unchanged cloud version continues it and keeps unsaved text', async () => {
  const h = await opened();
  h.store.updateProject({ logline: '未云同步的新文字' });
  h.controller.destroy();
  const refreshed = createStore({ storage: h.storage }),
    c = h.controllerFor(refreshed);
  await c.initialize();
  assert.equal(await c.resume(), true);
  assert.equal(c.getState().draftOffer, null);
  assert.equal(refreshed.getState().workspace.mode, 'cloud');
  assert.equal(refreshed.getState().project.logline, '未云同步的新文字');
  assert.equal(refreshed.getState().workspace.dirty, true);
  c.destroy();
});
test('resume never restores an older dirty draft over a newer remote version', async () => {
  const h = await opened();
  h.store.updateProject({ logline: '旧版脏稿' });
  h.remote.version = 4;
  h.controller.destroy();
  const refreshed = createStore({ storage: h.storage }),
    c = h.controllerFor(refreshed);
  await c.initialize();
  await c.resume();
  assert.equal(c.getState().draftOffer.record.version, 4);
  await assert.rejects(c.open(h.remote.id, { restoreDraft: true }), /较旧版本/);
  assert.equal(refreshed.getState().workspace.mode, 'local');
  assert.equal(refreshed.getCloudDraft('supervan', h.remote.id).project.logline, '旧版脏稿');
  c.destroy();
});
for (const owner of ['superdan', null])
  test(`different or revoked owner (${owner}) cannot see or fetch previous owner's resume project`, async () => {
    const h = await opened();
    h.controller.destroy();
    h.setOwner(owner);
    const refreshed = createStore({ storage: h.storage }),
      c = h.controllerFor(refreshed);
    await c.initialize();
    assert.equal(c.getState().resumeOffer, null);
    assert.equal(h.reads, 1);
    assert.equal(refreshed.getState().workspace.mode, 'local');
    c.destroy();
  });
for (const action of ['leave', 'dismiss', 'logout'])
  test(`explicit ${action} remembers local intent across refresh`, async () => {
    const h = await opened();
    if (action === 'leave') h.controller.leave();
    if (action === 'logout') await h.controller.logout();
    if (action === 'dismiss') {
      h.controller.destroy();
      const refreshed = createStore({ storage: h.storage }),
        c = h.controllerFor(refreshed);
      await c.initialize();
      c.dismissResume();
      c.destroy();
    }
    h.controller.destroy();
    h.setOwner('supervan');
    const next = h.controllerFor(createStore({ storage: h.storage }));
    await next.initialize();
    assert.equal(next.getState().resumeOffer, null);
    assert.deepEqual(JSON.parse(h.visits.getItem('yingxu-workspace-visit-v1')), { mode: 'local' });
    next.destroy();
  });
test('deleted or inaccessible last project gives recoverable explanation, leaves all drafts unchanged and clears stale navigation', async () => {
  const h = await opened();
  h.controller.destroy();
  const refreshed = createStore({ storage: h.storage }),
    c = h.controllerFor(refreshed);
  await c.initialize();
  const before = refreshed.exportProject();
  h.api.project = async () => {
    throw new CloudError('not found', { status: 404 });
  };
  await assert.rejects(c.resume());
  assert.match(c.getState().resumeError, /不存在/);
  assert.equal(c.getState().resumeOffer, null);
  assert.deepEqual(refreshed.exportProject(), before);
  assert.ok(refreshed.getCloudDraft('supervan', h.remote.id));
  c.destroy();
});
test('optional navigation storage failure never erases project or creates a fake resume identity', async () => {
  const h = setup();
  h.visits.fail = true;
  await h.controller.initialize();
  await h.controller.open(h.remote.id);
  assert.equal(h.store.getState().workspace.mode, 'cloud');
  assert.equal(h.store.getCloudDraft('supervan', h.remote.id).project.id, h.remote.id);
  h.controller.destroy();
  const c = h.controllerFor(createStore({ storage: h.storage }));
  await c.initialize();
  assert.equal(c.getState().resumeOffer, null);
  c.destroy();
});
test('late resume response after account changes cannot insert a previous owner project', async () => {
  const h = await opened();
  h.controller.destroy();
  const refreshed = createStore({ storage: h.storage }),
    c = h.controllerFor(refreshed);
  await c.initialize();
  let resolve;
  h.api.project = () => new Promise((r) => (resolve = r));
  const pending = c.resume(),
    rejected = assert.rejects(pending, /账户已经改变/);
  h.setOwner('superdan');
  await c.recheckSession();
  resolve(structuredClone(h.remote));
  await rejected;
  assert.equal(c.getState().account, 'superdan');
  assert.equal(c.getState().resumeOffer, null);
  assert.equal(refreshed.getState().workspace.mode, 'local');
  c.destroy();
});

function fakeWindow() {
  const listeners = new Map();
  return {
    listeners,
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: (name, fn) => {
      if (listeners.get(name) === fn) listeners.delete(name);
    },
    fire() {
      const event = {
        prevented: false,
        returnValue: null,
        preventDefault() {
          this.prevented = true;
        },
      };
      listeners.get('beforeunload')?.(event);
      return event;
    },
  };
}
test('beforeunload guards only failed persistence, retries checkpoint, and releases when storage recovers', async () => {
  const h = await opened(),
    win = fakeWindow(),
    stop = installPersistenceGuard(h.store, win);
  h.store.updateProject({ logline: '正常已落盘脏稿' });
  assert.equal(h.store.getState().workspace.dirty, true);
  assert.equal(win.listeners.has('beforeunload'), false);
  h.storage.fail = true;
  h.store.updateProject({ logline: '只有内存的修改' });
  assert.equal(win.listeners.has('beforeunload'), true);
  assert.equal(win.fire().prevented, true);
  assert.equal(h.store.exportProject().logline, '只有内存的修改');
  h.storage.fail = false;
  assert.equal(win.fire().prevented, false);
  assert.equal(win.listeners.has('beforeunload'), false);
  assert.equal(h.store.getCloudDraft('supervan', h.remote.id).project.logline, '只有内存的修改');
  stop();
  h.controller.destroy();
});
test('previous account volatile draft keeps unload protection after returning to local work', async () => {
  const h = await opened(),
    win = fakeWindow(),
    stop = installPersistenceGuard(h.store, win);
  h.storage.fail = true;
  h.store.updateProject({ logline: '旧账户仅内存稿' });
  h.controller.leave();
  h.storage.fail = false;
  assert.equal(h.store.getPersistenceRisk().otherCloudDrafts, 1);
  assert.equal(win.fire().prevented, true);
  await h.controller.open(h.remote.id, { restoreDraft: true });
  assert.equal(h.store.exportProject().logline, '旧账户仅内存稿');
  assert.equal(h.store.getPersistenceRisk().otherCloudDrafts, 0);
  assert.equal(win.listeners.has('beforeunload'), false);
  stop();
  h.controller.destroy();
});
test('local-only failed writes also get unload protection and cleanup removes the handler', () => {
  const h = setup(),
    win = fakeWindow(),
    stop = installPersistenceGuard(h.store, win);
  h.storage.fail = true;
  h.store.updateProject({ logline: '本机内存稿' });
  assert.equal(win.fire().prevented, true);
  stop();
  assert.equal(win.listeners.has('beforeunload'), false);
  h.controller.destroy();
});
