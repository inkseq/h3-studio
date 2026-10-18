import test from 'node:test';
import assert from 'node:assert/strict';
import { modeAvailability, requireModeAvailable } from './generation-availability.js';
import { createQuickChatController } from './quick-chat-controller.js';
import { createQuickChatClient } from './quick-chat-client.js';

const capabilities = {
  execution_enabled: true,
  recipes: [
    { id: 'fl-recipe', mode: 'fl' },
    { id: 'ref-recipe', mode: 'ref' },
  ],
};
const settings = {
  recipe_id: 'fl-recipe',
  deployment_profile_id: 'exact-profile',
  controls: { steps: 20 },
  copies: 1,
};
function snapshot(fl = 'ready', ref = 'ready', now = Date.now()) {
  const row = (mode, state) => ({
    recipe_id: mode + '-recipe',
    state,
    reason_code: 'capacity_' + state,
    available: ['ready', 'busy'].includes(state),
  });
  return {
    version: 1,
    observed_at: now / 1000,
    expires_at: now / 1000 + 10,
    poll_after_seconds: 10,
    advisory_only: true,
    profiles: [
      {
        deployment_profile_id: 'exact-profile',
        modes: { fl: row('fl', fl), ref: row('ref', ref) },
      },
    ],
  };
}
const memory = () => {
  const data = new Map();
  return {
    getItem: (key) => data.get(key) || null,
    setItem: (key, value) => data.set(key, value),
    removeItem: (key) => data.delete(key),
  };
};
function clientFor(overrides = {}) {
  let account;
  return {
    setAccount: (value) => (account = value),
    reset() {},
    schema: async () => ({}),
    capabilities: async () => capabilities,
    me: async () => ({ username: account, operator_capacity: { view: true } }),
    generationAvailability: async () => snapshot(),
    sessions: async () => ({ sessions: [] }),
    session: async (id) => ({
      session: { id, version: 1, next_settings: structuredClone(settings) },
    }),
    materials: async () => ({
      bindings: [{ asset_id: 'kept-image', slot: 'first_frame', enabled: true }],
    }),
    timeline: async () => ({ events: [] }),
    submission: async () => ({ id: 'submission', revision_id: 'revision', items: [] }),
    ...overrides,
  };
}
async function setup(client) {
  const controller = createQuickChatController({ client, storage: memory() });
  await controller.setAccount('owner');
  await controller.open('session');
  return controller;
}
const revision = { ...settings, id: 'revision', input_hash: 'immutable-hash' };

test('both modes are selectable only for the exact profile and recipe; authoring input stays unchanged', () => {
  const value = snapshot('ready', 'ready', 100000),
    before = structuredClone(settings);
  for (const mode of ['fl', 'ref'])
    assert.equal(modeAvailability(value, capabilities, settings, mode, 100001).available, true);
  for (const profile of ['other-profile', 'retired-profile', null, undefined])
    assert.equal(
      modeAvailability(
        value,
        capabilities,
        { ...settings, deployment_profile_id: profile },
        'fl',
        100001,
      ).available,
      false,
    );
  assert.equal(
    modeAvailability(value, capabilities, { ...settings, recipe_id: 'unknown' }, 'fl', 100001)
      .available,
    false,
  );
  value.profiles[0].modes.fl.recipe_id = 'ref-recipe';
  assert.equal(modeAvailability(value, capabilities, settings, 'fl', 100001).state, 'unknown');
  assert.deepEqual(settings, before);
});

test('ready and busy allow admission checking; starting, unavailable, disabled and unknown block', () => {
  for (const state of ['ready', 'busy', 'starting', 'unavailable', 'disabled', 'unknown']) {
    const value = snapshot(state, 'ready', 100000),
      actual = modeAvailability(value, capabilities, settings, 'fl', 100001);
    assert.equal(actual.state, state);
    assert.equal(actual.available, ['ready', 'busy'].includes(state));
    assert.equal(modeAvailability(value, capabilities, settings, 'ref', 100001).available, true);
  }
  assert.match(
    modeAvailability(snapshot('starting', 'ready', 100000), capabilities, settings, 'fl', 100001)
      .message,
    /等待就绪/,
  );
  assert.doesNotMatch(
    modeAvailability(snapshot('starting', 'ready', 100000), capabilities, settings, 'fl', 100001)
      .message,
    /管理员/,
  );
});

test('expiry, malformed rows and inconsistent flags fail closed, including duplicate profile identities', () => {
  assert.equal(
    modeAvailability(snapshot('ready', 'ready', 100000), capabilities, settings, 'fl', 110000)
      .state,
    'stale',
  );
  for (const value of [
    null,
    {},
    { ...snapshot('ready', 'ready', 100000), version: 2 },
    { ...snapshot('ready', 'ready', 100000), expires_at: 1000 },
    { ...snapshot('ready', 'ready', 100000), profiles: [null] },
  ])
    assert.equal(modeAvailability(value, capabilities, settings, 'fl', 100001).available, false);
  const bad = snapshot('ready', 'ready', 100000);
  bad.profiles[0].modes.fl.available = false;
  assert.equal(modeAvailability(bad, capabilities, settings, 'fl', 100001).state, 'unknown');
  const duplicate = snapshot('ready', 'ready', 100000);
  duplicate.profiles.push(structuredClone(duplicate.profiles[0]));
  assert.equal(modeAvailability(duplicate, capabilities, settings, 'fl', 100001).available, false);
  assert.throws(
    () =>
      requireModeAvailable(
        snapshot('unavailable', 'ready', 100000),
        capabilities,
        settings,
        'fl',
        100001,
      ),
    /原草稿和素材保持不变/,
  );
});

test('legacy settings use only a deliberate null-profile row, never the first advertised profile', () => {
  const value = snapshot('ready', 'ready', 100000),
    legacy = { recipe_id: 'fl-recipe' };
  assert.equal(modeAvailability(value, capabilities, legacy, 'fl', 100001).available, false);
  value.profiles.push({ ...structuredClone(value.profiles[0]), deployment_profile_id: null });
  assert.equal(modeAvailability(value, capabilities, legacy, 'fl', 100001).available, true);
});

test('availability reads use the shared same-origin API and expected account fence', async () => {
  let call;
  const client = createQuickChatClient({
    fetcher: async (path, options) => {
      call = { path, options };
      return new Response(JSON.stringify(snapshot()));
    },
  });
  client.setAccount('owner');
  await client.generationAvailability();
  assert.equal(call.path, '/v1/generation-availability');
  assert.equal(call.options.method, 'GET');
  assert.equal(call.options.credentials, 'same-origin');
  assert.equal(call.options.headers['X-Expected-Account'], 'owner');
});

test('new submit rechecks live availability and keeps draft/assets without creating pending command when capacity disappeared', async () => {
  let value = snapshot(),
    posts = 0,
    reads = 0;
  const controller = await setup(
    clientFor({
      generationAvailability: async () => {
        reads++;
        return value;
      },
      submit: async () => {
        posts++;
      },
    }),
  );
  const before = structuredClone({
    session: controller.getState().session,
    materials: controller.getState().materials,
  });
  value = snapshot('unavailable');
  await assert.rejects(controller.submit(revision, { id: 'preflight' }), /暂无可用机器/);
  assert.equal(reads, 2);
  assert.equal(posts, 0);
  assert.equal(controller.getState().pending, null);
  assert.deepEqual(
    { session: controller.getState().session, materials: controller.getState().materials },
    before,
  );
});

test('busy capacity accepts one confirmed submission after fresh read; unavailable other mode cannot be borrowed', async () => {
  let reads = 0,
    posts = 0;
  const controller = await setup(
    clientFor({
      generationAvailability: async () => {
        reads++;
        return snapshot('busy', 'unavailable');
      },
      submit: async (_sid, _rid, body) => {
        posts++;
        assert.equal(body.confirmed, true);
        return { id: 'submission', revision_id: 'revision', items: [] };
      },
    }),
  );
  await controller.submit(revision, { id: 'preflight' });
  assert.equal(reads, 2);
  assert.equal(posts, 1);
  await assert.rejects(
    controller.submit({ ...revision, recipe_id: 'ref-recipe' }, { id: 'another' }),
    /暂无可用机器/,
  );
  assert.equal(posts, 1);
});

test('unknown submission recovery reuses original key/body even after capacity becomes unavailable', async () => {
  let reads = 0;
  const calls = [];
  const client = clientFor({
      generationAvailability: async () => {
        reads++;
        if (reads > 2) throw Error('availability unavailable');
        return snapshot();
      },
      submit: async (...args) => {
        calls.push(structuredClone(args));
        if (calls.length === 1) throw Error('lost original receipt');
        return { id: 'submission', revision_id: 'revision', items: [] };
      },
    }),
    controller = await setup(client);
  await assert.rejects(controller.submit(revision, { id: 'preflight' }));
  assert.ok(controller.getState().pending);
  await controller.recover();
  assert.equal(reads, 2);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0], calls[1]);
  assert.equal(controller.getState().pending, null);
});

test('failed availability read clears prior ready state and prevents new paid command', async () => {
  let fail = false,
    posts = 0;
  const controller = await setup(
    clientFor({
      generationAvailability: async () => {
        if (fail) throw Error('offline');
        return snapshot();
      },
      submit: async () => {
        posts++;
      },
    }),
  );
  fail = true;
  await assert.rejects(controller.submit(revision, { id: 'preflight' }));
  assert.equal(controller.getState().availability, null);
  assert.equal(controller.getState().availabilityChecking, false);
  assert.equal(posts, 0);
  assert.equal(controller.getState().pending, null);
});

test('late availability cannot enter another account', async () => {
  let finish;
  const controller = createQuickChatController({
    client: clientFor({
      generationAvailability: () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    }),
    storage: memory(),
  });
  const original = controller.setAccount('owner');
  await Promise.resolve();
  await controller.setAccount(null);
  finish(snapshot());
  await original;
  assert.equal(controller.getState().account, null);
  assert.equal(controller.getState().availability, null);
  assert.equal(controller.getState().operatorAccess, false);
});

test('older poll cannot overwrite fresh submission read or clear its result', async () => {
  let reads = 0,
    finish;
  const controller = await setup(
    clientFor({
      generationAvailability: () => {
        reads++;
        if (reads === 2)
          return new Promise((resolve) => {
            finish = resolve;
          });
        return Promise.resolve(snapshot());
      },
      submit: async () => ({ id: 'submission', revision_id: 'revision', items: [] }),
    }),
  );
  const old = controller.refreshAvailability();
  await Promise.resolve();
  const rejected = assert.rejects(old);
  await controller.submit(revision, { id: 'preflight' });
  const fresh = controller.getState().availability;
  finish(snapshot('unavailable'));
  await rejected;
  assert.equal(controller.getState().availability, fresh);
  assert.equal(controller.getState().availabilityChecking, false);
});

test('draft turns remain writable without available machines and never submit generation', async () => {
  let turns = 0;
  const controller = await setup(
    clientFor({
      generationAvailability: async () => snapshot('unavailable', 'unavailable'),
      turn: async () => {
        turns++;
        return { id: 'turn' };
      },
    }),
  );
  await controller.turn('保存这个镜头', 'gemini-3.8-flash', 'none', { createCard: true });
  assert.equal(turns, 1);
  assert.equal(controller.getState().pending, null);
  assert.equal(controller.getState().materials[0].asset_id, 'kept-image');
});
