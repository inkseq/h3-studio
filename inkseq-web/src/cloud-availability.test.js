import test from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from './store.js';
import { createCloudController } from './cloud-controller.js';
import { createCloudClient, CloudError } from './cloud-client.js';

const recipes = ['fl', 'ref'].map((mode) => ({
  id: 'h3-' + mode,
  mode,
  controls: {
    duration: { type: 'integer', default: 5 },
    resolution: { type: 'string', enum: ['480P'], default: '480P' },
  },
}));
const capabilities = { capabilities_version: 'capacity-test-v1', execution_enabled: true, recipes };
const snapshot = (fl = 'ready', ref = 'ready', profile = null) => ({
  version: 1,
  observed_at: Date.now() / 1000,
  expires_at: Date.now() / 1000 + 10,
  poll_after_seconds: 10,
  advisory_only: true,
  profiles: [
    {
      deployment_profile_id: profile,
      modes: Object.fromEntries(
        recipes.map((recipe) => {
          const state = recipe.mode === 'fl' ? fl : ref;
          return [
            recipe.mode,
            {
              recipe_id: recipe.id,
              state,
              available: ['ready', 'busy'].includes(state),
              reason_code: 'test_' + state,
            },
          ];
        }),
      ),
    },
  ],
});
async function harness() {
  const map = new Map(),
    storage = {
      getItem: (key) => map.get(key) ?? null,
      setItem: (key, value) => map.set(key, value),
    },
    store = createStore({ storage }),
    calls = [],
    remote = new Map();
  store.newProject('Local original');
  const chapter = store.addEntity('chapter'),
    scene = store.addEntity('scene', chapter),
    shot = store.addEntity('shot', scene, {
      description: 'A moving camera',
      data: { seconds: 5, h3: { recipeId: 'h3-fl' } },
    });
  let availability = snapshot(),
    reads = 0;
  const api = {
    reset() {},
    login: async () => ({ username: 'superdan' }),
    logout: async () => ({}),
    me: async () => ({ username: 'superdan', operator_capacity: { view: true } }),
    projects: async () => ({ projects: [] }),
    capabilities: async () => capabilities,
    generationAvailability: async () => {
      reads++;
      return structuredClone(availability);
    },
    createProject: async (project) => {
      const record = { id: project.id, version: 1, project: structuredClone(project) };
      remote.set(record.id, record);
      return structuredClone(record);
    },
    project: async (id) => structuredClone(remote.get(id)),
    saveProject: async (id, project, version) => {
      calls.push(['save']);
      const record = { id, project: structuredClone(project), version: version + 1 };
      remote.set(id, record);
      return record;
    },
    jobs: async () => ({ jobs: [] }),
    plan: async (payload) => {
      const id = 'plan-' + payload.client_ref.shot_id;
      calls.push(['plan', id]);
      return { plan_id: id, status: 'ready', expires_at: Date.now() / 1000 + 600 };
    },
    submit: async (plan, key) => {
      calls.push(['submit', plan, key]);
      return {
        id: 'job',
        plan_id: plan,
        status: 'queued',
        client_ref: { project_id: store.getState().project.id, shot_id: shot },
      };
    },
    batch: async (project, plans, key) => {
      calls.push(['batch', project, plans, key]);
      return { id: 'batch', status: 'active', items: [] };
    },
  };
  const controller = createCloudController({ store, storage, client: api, mediaHandlers() {} });
  await controller.login('superdan', 'synthetic');
  await controller.create('Cloud copy', { copy: true });
  return {
    store,
    controller,
    api,
    calls,
    map,
    shot,
    scene,
    setAvailability: (value) => {
      availability = value;
    },
    reads: () => reads,
  };
}

test('Yingxu saves/preflights without capacity but blocks new submission on absent, starting, disabled, stale or wrong-profile capacity', async () => {
  const h = await harness();
  try {
    for (const value of [
      snapshot('unavailable'),
      snapshot('starting'),
      snapshot('disabled'),
      snapshot('unknown'),
      snapshot('ready', 'ready', 'another-profile'),
      { ...snapshot(), observed_at: Date.now() / 1000 - 20, expires_at: Date.now() / 1000 - 10 },
    ]) {
      h.setAvailability(value);
      await h.controller.save();
      await h.controller.prepare(h.shot);
      await assert.rejects(h.controller.submit(h.shot));
      assert.equal(
        h.calls.some((c) => c[0] === 'submit'),
        false,
      );
      assert.equal(Object.keys(h.controller.getState().unknown).length, 0);
    }
    assert.equal(
      [...h.map.keys()].some((key) => key.includes(':submit:')),
      false,
    );
  } finally {
    h.controller.destroy();
  }
});

test('Yingxu checks fresh mode at submit, allows busy, and never borrows ready FL for REF', async () => {
  const h = await harness();
  try {
    await h.controller.prepare(h.shot);
    h.setAvailability(snapshot('busy', 'unavailable'));
    const before = h.reads();
    await h.controller.submit(h.shot);
    assert.ok(h.reads() > before);
    assert.equal(h.calls.filter((c) => c[0] === 'submit').length, 1);
    const reference = h.store.addEntity('image', null, {
      data: { fileId: 'cloud_asset_reference', cloudAssetId: 'reference' },
    });
    const refShot = h.store.addEntity('shot', h.scene, {
      description: 'A reference motion',
      data: { seconds: 5, h3: { recipeId: 'h3-ref' } },
    });
    h.store.addLink(reference, refShot, 'reference');
    await h.controller.prepare(refShot);
    await assert.rejects(h.controller.submit(refShot), /Ref2VA/);
    assert.equal(h.calls.filter((c) => c[0] === 'submit').length, 1);
  } finally {
    h.controller.destroy();
  }
});

test('Yingxu older-server read failure stays unknown and never creates a paid intent', async () => {
  const h = await harness();
  try {
    await h.controller.prepare(h.shot);
    h.api.generationAvailability = async () => {
      throw new CloudError('PRIVATE provider detail', { status: 404 });
    };
    await assert.rejects(
      h.controller.submit(h.shot),
      (error) => !error.message.includes('PRIVATE') && error.message.includes('状态'),
    );
    assert.equal(h.controller.getState().availability, null);
    assert.equal(
      h.calls.some((c) => c[0] === 'submit'),
      false,
    );
    assert.equal(
      [...h.map.keys()].some((key) => key.includes(':submit:')),
      false,
    );
  } finally {
    h.controller.destroy();
  }
});

test('Yingxu original single and batch submissions replay unchanged despite unavailable reads', async () => {
  for (const batch of [false, true]) {
    const h = await harness();
    try {
      await h.controller.prepare(h.shot);
      const attempts = [];
      const name = batch ? 'batch' : 'submit',
        original = h.api[name];
      h.api[name] = async (...args) => {
        attempts.push(args);
        if (attempts.length === 1) throw new CloudError('Lost response');
        return original(...args);
      };
      await assert.rejects(
        batch ? h.controller.submitBatch([h.shot]) : h.controller.submit(h.shot),
      );
      h.api.generationAvailability = async () => {
        throw Error('Must not read on replay');
      };
      h.store.updateEntity(h.shot, { description: 'Later draft' });
      const before = h.reads();
      await (batch ? h.controller.recoverBatch() : h.controller.submit(h.shot));
      assert.deepEqual(attempts[1], attempts[0]);
      assert.equal(h.reads(), before);
    } finally {
      h.controller.destroy();
    }
  }
});

test('Yingxu rejects entire new mixed-mode batch before persisting intent if one mode lacks capacity', async () => {
  const h = await harness();
  try {
    const image = h.store.addEntity('image', null, {
        data: { fileId: 'cloud_asset_reference', cloudAssetId: 'reference' },
      }),
      refShot = h.store.addEntity('shot', h.scene, {
        description: 'Reference motion',
        data: { seconds: 5, h3: { recipeId: 'h3-ref' } },
      });
    h.store.addLink(image, refShot, 'reference');
    await h.controller.prepareBatch([h.shot, refShot]);
    h.setAvailability(snapshot('ready', 'unavailable'));
    await assert.rejects(h.controller.submitBatch([h.shot, refShot]), /Ref2VA/);
    assert.equal(h.controller.getState().batchPending, null);
    assert.equal(
      h.calls.some((c) => c[0] === 'batch'),
      false,
    );
  } finally {
    h.controller.destroy();
  }
});

test('late availability cannot reappear after account departure', async () => {
  const h = await harness();
  try {
    let resolve;
    h.api.generationAvailability = () =>
      new Promise((done) => {
        resolve = done;
      });
    const reading = h.controller.reloadAvailability();
    await h.controller.logout();
    resolve(snapshot());
    await assert.rejects(reading, /账户/);
    assert.equal(h.controller.getState().availability, null);
    assert.equal(h.controller.getState().operatorAccess, false);
  } finally {
    h.controller.destroy();
  }
});

test('shared browser availability request is authenticated same-origin GET only', async () => {
  const calls = [],
    client = createCloudClient({
      fetcher: async (path, options) => {
        calls.push([path, options]);
        return new Response(JSON.stringify(snapshot()), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      },
    });
  client.setAccount('supervan');
  await client.generationAvailability();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/v1/generation-availability');
  assert.equal(calls[0][1].method, 'GET');
  assert.equal(calls[0][1].credentials, 'same-origin');
  assert.equal(calls[0][1].headers['X-Expected-Account'], 'supervan');
  assert.equal(calls[0][1].body, undefined);
});
