import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { Module } from 'node:module';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createP4OperatorClient } from './p4-operator-client.js';
import { createP4OperatorController } from './p4-operator-controller.js';
import {
  batchRows,
  costLabel,
  groupedNodes,
  initialRows,
  lifetimeBody,
  phase,
  preparationSteps,
  preparationView,
  transferProgress,
  previewCurrent,
  queueLabel,
  rate,
  testTaskBody,
  todayMetrics,
  workspaceStale,
} from './p4-operator-model.js';

const memory = () => {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
};
function client(overrides = {}) {
  let account;
  return {
    setAccount: (value) => {
      account = value;
    },
    reset() {},
    state: async () => ({
      capacity_backend: 'dstack-v1',
      operator: { account, admin: false },
      nodes: [],
      groups: [],
      queue: [],
    }),
    catalog: async () => ({ profiles: [] }),
    ...overrides,
  };
}
const lifecycle = (changes) => ({
  version: 2,
  keep_on: 'idle',
  hold_until: 0,
  idle_shutdown_seconds: 600,
  reservation_seconds: 3600,
  expected_runtime_s: 1800,
  budget_account_ids: ['owner-cap'],
  ...changes,
});

test('live workspace reads require identity and arrays instead of dummy zero machines', async () => {
  const responses = [
    { nodes: [], groups: [], queue: [] },
    { capacity_backend: 'dstack-v1', nodes: [], groups: [] },
    { capacity_backend: 'dstack-v1', nodes: [{ node_id: 'original' }], groups: [], queue: [] },
  ];
  const api = createP4OperatorClient({
    fetcher: async () =>
      new Response(JSON.stringify(responses.shift()), {
        headers: { 'Content-Type': 'application/json' },
      }),
  });
  await assert.rejects(api.state(), /console state could not be confirmed/);
  await assert.rejects(api.state(), /console state could not be confirmed/);
  assert.equal((await api.state()).nodes[0].id, 'original');
});

test('workspace freshness tolerates bounded clock skew while preserving aged and invalid observations', async (context) => {
  let clock = 1000000,
    resolve;
  context.mock.method(Date, 'now', () => clock);
  const controller = createP4OperatorController({
    storage: memory(),
    client: client({
      state: () =>
        new Promise((done) => {
          resolve = done;
        }),
    }),
  });
  const tick = Date.now(),
    initial = controller.setAccount('superdan');
  clock += 250;
  resolve({
    operator: { account: 'superdan' },
    observed_at: (tick + 100) / 1000,
    nodes: [],
    groups: [],
    queue: [],
  });
  await initial;
  const snapshot = controller.getState().snapshot;
  assert.equal(
    workspaceStale(snapshot, tick),
    false,
    'a small server/client clock difference does not disable the workspace',
  );
  assert.equal(workspaceStale(snapshot), false, 'the returned state is fresh at rendering time');
  clock = snapshot.observed_at * 1000 + 30000;
  assert.equal(
    workspaceStale(snapshot),
    false,
    'the existing30-second freshness boundary is retained',
  );
  clock++;
  assert.equal(workspaceStale(snapshot), true, 'an old readback does not stay actionable');
  assert.equal(
    workspaceStale({ ...snapshot, observed_at: (clock + 5000) / 1000 }),
    false,
    'up to5 seconds of clock skew is accepted',
  );
  assert.equal(
    workspaceStale({ ...snapshot, observed_at: (clock + 5001) / 1000 }),
    true,
    'larger future observations remain rejected',
  );
  assert.equal(workspaceStale({ ...snapshot, observed_at: null }), true);
  assert.equal(workspaceStale({ ...snapshot, observed_at: NaN }), true);
  assert.equal(workspaceStale(snapshot, Infinity), true);
  assert.equal(workspaceStale(null), false);
  controller.destroy();
});

test('group/admin commands preserve cookie actor, body, and ordinary idempotency keys', async () => {
  const calls = [],
    api = createP4OperatorClient({
      fetcher: async (path, options) => {
        calls.push({ path, options });
        return new Response(
          JSON.stringify(
            path.endsWith('/workspace')
              ? {
                  capacity_backend: 'dstack-v1',
                  operator: { account: 'superdan', admin: true },
                  nodes: [],
                  groups: [],
                  queue: [],
                }
              : { group_id: 'group' },
          ),
          {
            headers: { 'Content-Type': 'application/json', 'X-Authenticated-Account': 'superdan' },
          },
        );
      },
    });
  api.setAccount('superdan');
  await api.adminState();
  await api.createGroup({ name: 'pair' }, 'create-original');
  await api.move('other/node', { group_id: null, expected_version: 1 }, 'move-original');
  await api.stopImpact('group', 'original/group');
  await api.groupStop('g', { acknowledge_capacity_loss: 'impact-fingerprint' }, 'stop-original');
  await api.groupHold('g', { keep_on: 'until_stopped', hold_seconds: 0 }, 'hold-original');
  assert.deepEqual(
    calls.map((call) => call.path),
    [
      '/v1/operator/dstack/admin/workspace',
      '/v1/operator/dstack/groups',
      '/v1/operator/dstack/nodes/other%2Fnode/group',
      '/v1/operator/dstack/groups/original%2Fgroup/stop-impact',
      '/v1/operator/dstack/groups/g/stop',
      '/v1/operator/dstack/groups/g/hold',
    ],
  );
  for (const call of calls) {
    assert.equal(call.options.credentials, 'same-origin');
    assert.equal(call.options.headers['X-Expected-Account'], 'superdan');
  }
  assert.equal(calls[4].options.headers['Idempotency-Key'], 'stop-original');
  assert.deepEqual(JSON.parse(calls[2].options.body), { group_id: null, expected_version: 1 });
});

test('unknown P4 stop retains queue acknowledgement and original replay identity through reload', async () => {
  const calls = [],
    storage = memory();
  let fail = true;
  const api = client({
    state: async () => ({
      operator: { account: 'superdan' },
      nodes: [{ id: 'original', owner_id: 'superdan', version: 3 }],
      groups: [],
      queue: [],
    }),
    stop: async (id, body, key) => {
      calls.push({ id, body, key });
      if (fail) throw Error('lost response');
      return { node_id: id, state: 'accepted' };
    },
  });
  const first = createP4OperatorController({ client: api, storage });
  await first.setAccount('superdan');
  await assert.rejects(
    first.stop(
      { id: 'original', version: 3 },
      { warnings: [{ mode: 'fl', waiting_jobs: 3 }], fingerprint: 'three-fl' },
    ),
  );
  const original = structuredClone(first.getState().pending);
  first.destroy();
  const resumed = createP4OperatorController({ client: api, storage });
  await resumed.setAccount('superdan');
  assert.deepEqual(resumed.getState().pending, original);
  assert.equal(calls.length, 1);
  fail = false;
  await resumed.recover();
  assert.deepEqual(calls[1], calls[0]);
  assert.deepEqual(calls[0].body, { expected_version: 3, acknowledge_capacity_loss: 'three-fl' });
  assert.equal(resumed.getState().pending, null);
});

test('unknown first start blocks a later machine instead of duplicating the pending rental', async () => {
  let calls = 0;
  const controller = createP4OperatorController({
    client: client({
      start: async () => {
        calls++;
        throw Error('lost');
      },
    }),
    storage: memory(),
  });
  await controller.setAccount('superdan');
  await assert.rejects(controller.start({ preview_id: 'first' }));
  await assert.rejects(controller.start({ preview_id: 'second' }), /原操作结果/);
  assert.equal(calls, 1);
});

test('known application capacity refusal clears only its start journal and allows a new explicit click', async () => {
  const storage = memory(),
    calls = [];
  const api = createP4OperatorClient({
    fetcher: async (path, options) => {
      if (path.endsWith('/starts')) {
        calls.push({ body: JSON.parse(options.body), key: options.headers['Idempotency-Key'] });
        return new Response(
          JSON.stringify({ code: 'capacity_unavailable', detail: '容量不足，未新增付费执行' }),
          { status: 429, headers: { 'Content-Type': 'application/json' } },
        );
      }
      return new Response(
        JSON.stringify(
          path.endsWith('/workspace')
            ? {
                capacity_backend: 'dstack-v1',
                operator: { account: 'supervan' },
                nodes: [],
                groups: [],
                queue: [],
              }
            : { profiles: [] },
        ),
        { headers: { 'Content-Type': 'application/json' } },
      );
    },
  });
  const controller = createP4OperatorController({ client: api, storage });
  await controller.setAccount('supervan');
  await assert.rejects(
    controller.start({ preview_id: 'first' }),
    /机器池尚未配置或已达到容量上限.*capacity_unavailable/,
  );
  assert.equal(calls.length, 1, 'a refused write is never automatically retried');
  assert.equal(controller.getState().pending, null);
  assert.equal(storage.getItem('sixnine:operator:dstack:pending:supervan'), null);
  assert.match(controller.getState().error, /没有新增付费执行/);
  await assert.rejects(controller.start({ preview_id: 'second' }), /capacity_unavailable/);
  assert.equal(calls.length, 2);
  assert.deepEqual(
    calls.map((call) => call.body),
    [{ preview_id: 'first' }, { preview_id: 'second' }],
  );
  assert.notEqual(
    calls[0].key,
    calls[1].key,
    'only the second explicit click creates a new operation',
  );
  controller.destroy();
});

test('unclassified 429 and uncertain start failures retain the original request across reload', async () => {
  for (const error of [
    Object.assign(Error('rate limited'), { status: 429 }),
    Object.assign(Error('provider limited'), {
      status: 429,
      detail: { code: 'provider_rate_limited' },
    }),
    Object.assign(Error('timed out'), { status: 408, detail: { code: 'capacity_unavailable' } }),
    Object.assign(Error('server failed'), {
      status: 503,
      detail: { code: 'capacity_unavailable' },
    }),
  ]) {
    const storage = memory(),
      calls = [],
      api = client({
        start: async (body, key) => {
          calls.push({ body, key });
          throw error;
        },
      });
    const first = createP4OperatorController({ client: api, storage });
    await first.setAccount('supervan');
    await assert.rejects(first.start({ preview_id: 'original' }));
    const original = structuredClone(first.getState().pending);
    first.destroy();
    assert.ok(original);
    const resumed = createP4OperatorController({ client: api, storage });
    await resumed.setAccount('supervan');
    assert.deepEqual(resumed.getState().pending, original);
    await assert.rejects(resumed.start({ preview_id: 'replacement' }), /原操作结果/);
    assert.equal(calls.length, 1);
    await assert.rejects(resumed.recover());
    assert.deepEqual(calls[1], calls[0]);
    assert.deepEqual(resumed.getState().pending, original);
    resumed.destroy();
  }
});

test('a known capacity code cannot clear an uncertain stop or lose its original recovery key', async () => {
  const storage = memory(),
    calls = [],
    api = client({
      state: async () => ({
        operator: { account: 'supervan' },
        nodes: [{ id: 'owned', owner_id: 'supervan', version: 2 }],
        groups: [],
        queue: [],
      }),
      stop: async (id, body, key) => {
        calls.push({ id, body, key });
        throw Object.assign(Error('capacity unavailable'), {
          status: 429,
          detail: { code: 'capacity_unavailable' },
        });
      },
    });
  const controller = createP4OperatorController({ client: api, storage });
  await controller.setAccount('supervan');
  await assert.rejects(
    controller.stop({ id: 'owned', owner_id: 'supervan', version: 1 }, { warnings: [] }),
  );
  const original = structuredClone(controller.getState().pending);
  assert.ok(original);
  await assert.rejects(controller.recover());
  assert.deepEqual(calls[1], calls[0]);
  assert.deepEqual(controller.getState().pending, original);
  controller.destroy();
});

test('heartbeat versions are freshly read immediately before stop, hold and move are journaled', async () => {
  const calls = [];
  let version = 1;
  const api = client({
    state: async () => ({
      operator: { account: 'superdan' },
      nodes: [{ id: 'original', owner_id: 'superdan', version: ++version }],
      groups: [],
      queue: [],
    }),
    ...Object.fromEntries(
      ['stop', 'hold', 'move'].map((method) => [
        method,
        async (id, body, key) => {
          calls.push({ method, id, body, key });
          return { node_id: id };
        },
      ]),
    ),
  });
  const controller = createP4OperatorController({ client: api, storage: memory() });
  await controller.setAccount('superdan');
  const old = { id: 'original', owner_id: 'superdan', version: 1 };
  await controller.stop(old, { warnings: [], fingerprint: 'no-loss' });
  await controller.hold(old, { keep_on: 'until_stopped', hold_seconds: 0 });
  await controller.move(old, null);
  assert.deepEqual(
    calls.map((call) => call.body.expected_version),
    [3, 5, 7],
  );
  assert.ok(calls.every((call) => call.id === 'original'));
  assert.equal(calls[2].body.group_id, null);
});

test('account switch during command pre-read rejects before creating a pending operation or writing', async () => {
  const storage = memory();
  let deferred = false,
    resolve,
    account = 'superdan',
    writes = 0;
  const snapshot = (owner) => ({
    operator: { account: owner },
    nodes: [{ id: 'original', owner_id: owner, version: 3 }],
    groups: [],
    queue: [],
  });
  const api = client({
    setAccount: (value) => {
      account = value;
    },
    state: () =>
      deferred
        ? new Promise((done) => {
            resolve = done;
          })
        : Promise.resolve(snapshot(account)),
    stop: async () => {
      writes++;
      return {};
    },
  });
  const controller = createP4OperatorController({ client: api, storage });
  await controller.setAccount('superdan');
  deferred = true;
  const command = controller.stop(
    { id: 'original', owner_id: 'superdan', version: 1 },
    { warnings: [] },
  );
  deferred = false;
  await controller.setAccount('supervan');
  resolve(snapshot('superdan'));
  await assert.rejects(command, /账户已改变/);
  assert.equal(writes, 0);
  assert.equal(controller.getState().pending, null);
  assert.equal(storage.getItem('sixnine:operator:dstack:pending:superdan'), null);
  assert.equal(storage.getItem('sixnine:operator:dstack:pending:supervan'), null);
});

test('fresh command read rejects missing or changed node identity without journaling an operation', async () => {
  for (const nodes of [
    [],
    [{ id: 'original', owner_id: 'other', version: 5 }],
    [{ id: 'other-node', owner_id: 'superdan', version: 5 }],
  ]) {
    const storage = memory();
    let writes = 0;
    const controller = createP4OperatorController({
      storage,
      client: client({
        state: async () => ({ operator: { account: 'superdan' }, nodes, groups: [], queue: [] }),
        hold: async () => {
          writes++;
          return {};
        },
      }),
    });
    await controller.setAccount('superdan');
    await assert.rejects(
      controller.hold(
        { id: 'original', owner_id: 'superdan', version: 1 },
        { keep_on: 'idle', hold_seconds: 0 },
      ),
      /身份或当前版本/,
    );
    assert.equal(writes, 0);
    assert.equal(controller.getState().pending, null);
  }
});

test('cross-account node stop refreshes admin projection but keeps the original cookie actor', async () => {
  const calls = [];
  let administrativeReads = 0;
  const api = client({
      state: async () => ({
        operator: { account: 'superdan', admin: true },
        nodes: [],
        groups: [],
        queue: [],
      }),
      adminState: async () => {
        administrativeReads++;
        return {
          operator: { account: 'superdan', admin: true },
          nodes: [{ id: 'foreign', owner_id: 'supervan', version: 9 }],
          groups: [],
          queue: [],
        };
      },
      stop: async (id, body, key) => {
        calls.push({ id, body, key });
        return {};
      },
    }),
    controller = createP4OperatorController({ client: api, storage: memory() });
  await controller.setAccount('superdan');
  await controller.stop({ id: 'foreign', owner_id: 'supervan', version: 1 }, { warnings: [] });
  assert.equal(calls[0].body.expected_version, 9);
  assert.equal(calls[0].id, 'foreign');
  assert.ok(administrativeReads >= 1);
  assert.equal(controller.getState().account, 'superdan');
});

test('definitive version conflict refreshes and asks for a new click without an automatic write retry', async () => {
  const calls = [];
  let version = 1;
  const controller = createP4OperatorController({
    storage: memory(),
    client: client({
      state: async () => ({
        operator: { account: 'superdan' },
        nodes: [{ id: 'original', owner_id: 'superdan', version: ++version }],
        groups: [],
        queue: [],
      }),
      stop: async (id, body, key) => {
        calls.push({ id, body, key });
        if (calls.length === 1)
          throw Object.assign(Error('operator_node_version_conflict'), {
            status: 409,
            detail: { detail: 'operator_node_version_conflict' },
          });
        return {};
      },
    }),
  });
  await controller.setAccount('superdan');
  const node = { id: 'original', owner_id: 'superdan', version: 1 };
  await assert.rejects(controller.stop(node, { warnings: [] }), /请重试/);
  assert.equal(calls.length, 1);
  assert.equal(controller.getState().pending, null);
  await controller.stop(node, { warnings: [] });
  assert.equal(calls.length, 2);
  assert.notEqual(calls[0].key, calls[1].key);
  assert.ok(calls[1].body.expected_version > calls[0].body.expected_version);
});

test('detail stage metrics cannot cross an account switch or a different machine identity', async () => {
  let resolve;
  const api = client({
    metrics: () =>
      new Promise((done) => {
        resolve = done;
      }),
  });
  const controller = createP4OperatorController({ client: api, storage: memory() });
  await controller.setAccount('superdan');
  const pending = controller.metrics('original');
  await controller.setAccount('supervan');
  resolve({ node_id: 'original', metrics: { average_generate_seconds: 80 } });
  await assert.rejects(pending, /账户已改变/);
  const mismatched = controller.metrics('original');
  resolve({ node_id: 'other', metrics: { average_generate_seconds: 80 } });
  await assert.rejects(mismatched, /机器统计身份/);
});

test('independent successful starts use distinct original keys and fresh preview IDs', async () => {
  const calls = [],
    api = client({
      start: async (body, key) => {
        calls.push({ body, key });
        return { node_id: 'n' + calls.length };
      },
    }),
    controller = createP4OperatorController({ client: api, storage: memory() });
  await controller.setAccount('superdan');
  await controller.start({ preview_id: 'p1' });
  await controller.start({ preview_id: 'p2' });
  assert.notEqual(calls[0].key, calls[1].key);
  assert.deepEqual(
    calls.map((call) => call.body),
    [{ preview_id: 'p1' }, { preview_id: 'p2' }],
  );
});

test('admin response and stored original commands stay isolated when the cookie actor changes', async () => {
  let denied = false;
  const storage = memory(),
    api = client({
      state: async () => {
        if (denied) throw Object.assign(Error('denied'), { status: 403 });
        return { operator: { account: 'superdan', admin: true }, nodes: [], groups: [], queue: [] };
      },
      adminState: async () => ({
        operator: { account: 'superdan', admin: true },
        nodes: [{ id: 'other-account-node' }],
      }),
      groupStop: async () => {
        throw Error('unknown');
      },
    });
  const controller = createP4OperatorController({ client: api, storage });
  await controller.setAccount('superdan');
  await controller.readAdmin();
  await assert.rejects(controller.groupStop({ id: 'g', version: 1 }, { warnings: [] }));
  const saved = controller.getState().pending;
  denied = true;
  await assert.rejects(controller.refresh(true));
  assert.equal(controller.getState().adminSnapshot, null);
  assert.equal(controller.getState().snapshot, null);
  assert.deepEqual(controller.getState().pending, saved);
  await controller.setAccount('supervan');
  assert.equal(controller.getState().pending, null);
  assert.equal(controller.getState().adminSnapshot, null);
});

test('admin demotion clears all-account data without corrupting owned machine history', async () => {
  let admin = true;
  const controller = createP4OperatorController({
    storage: memory(),
    client: client({
      state: async () => ({
        operator: { account: 'superdan', admin },
        nodes: [{ id: 'owned' }],
        groups: [],
        queue: [],
      }),
      adminState: async () => ({
        operator: { account: 'superdan', admin: true },
        nodes: [{ id: 'other-owner' }],
      }),
    }),
  });
  await controller.setAccount('superdan');
  await controller.readAdmin();
  assert.equal(controller.getState().adminSnapshot.nodes[0].id, 'other-owner');
  admin = false;
  await controller.refresh(true);
  assert.equal(controller.getState().adminSnapshot, null);
  assert.equal(controller.getState().snapshot.nodes[0].id, 'owned');
});

test('observed lifecycle keeps stopping machines visible and charged until terminal readback', () => {
  const node = {
    state: 'starting',
    desired_state: 'running',
    observation_fresh: true,
    hourly_cost_microusd: 850000,
  };
  assert.equal(phase({ ...node, lifecycle_phase: 'renting' }).label, '申请机器中');
  assert.equal(phase({ ...node, lifecycle_phase: 'downloading_weights' }).label, '下载权重中');
  assert.equal(phase({ ...node, lifecycle_phase: 'loading_model' }).label, '加载模型中');
  assert.equal(
    phase({ ...node, state: 'ready', ready: false, lifecycle_phase: 'ready' }).label,
    '准备中',
  );
  assert.equal(phase({ ...node, state: 'ready', ready: true }).label, '就绪');
  assert.equal(phase({ ...node, desired_state: 'stopped' }).label, '停止中');
  assert.equal(rate({ ...node, desired_state: 'stopped' }).value, 850000);
  assert.equal(
    groupedNodes([{ ...node, id: 'n', desired_state: 'stopped' }], [])[0].nodes.length,
    1,
  );
  assert.equal(groupedNodes([{ ...node, id: 'n', state: 'stopped' }], [])[0].nodes.length, 0);
});

test('default batch is FL+REF but group/add/find/queue defaults to exactly one machine', () => {
  const profiles = [
    { id: 'fl', model_id: 'pruned', backend: 'vastai', mode: 'fl' },
    { id: 'ref', model_id: 'pruned', backend: 'vastai', mode: 'ref' },
  ];
  assert.deepEqual(
    initialRows(profiles).map((row) => row.profile_id),
    ['fl', 'ref'],
  );
  assert.equal(initialRows(profiles, { one: true }).length, 1);
  assert.deepEqual(
    initialRows(profiles, { mode: 'ref' }).map((row) => row.profile_id),
    ['ref'],
  );
  assert.deepEqual(
    batchRows([{ id: 'row', profile_id: 'fl', quantity: 2 }], profiles).map((row) => row.number),
    [1, 2],
  );
  assert.equal(batchRows([{ profile_id: 'fl', quantity: 0 }], profiles), null);
  assert.equal(batchRows([{ profile_id: 'unknown', quantity: 1 }], profiles), null);
});

test('manual stop policy accepts no fixed deadline and finite keep time must be future', () => {
  assert.deepEqual(lifetimeBody({ keep_on: 'until_stopped', idle_minutes: 10 }), {
    keep_on: 'until_stopped',
    hold_seconds: 0,
    idle_shutdown_seconds: 600,
  });
  assert.equal(
    lifetimeBody({ keep_on: 'until', idle_minutes: 10, until: '1970-01-01T00:00' }),
    null,
  );
  assert.equal(lifetimeBody({ keep_on: 'idle', idle_minutes: 0 }), null);
  const selection = {
      profile_id: 'fl',
      group_id: null,
      keep_on: 'idle',
      hold_seconds: 0,
      idle_shutdown_seconds: 600,
    },
    preview = {
      capacity_backend: 'dstack-v1',
      can_start: true,
      preview_id: 'p',
      expires_at: 1100,
      hard_deadline: null,
      lifecycle: lifecycle(),
      hourly_cost_microusd: 850000,
      reservation_microusd: 920834,
    };
  assert.equal(previewCurrent(preview, selection, 1000000), true);
  assert.equal(previewCurrent(preview, { ...selection, keep_on: 'until_stopped' }, 1000000), false);
  assert.equal(previewCurrent({ ...preview, profile_id: 'ref' }, selection, 1000000), false);
  assert.equal(previewCurrent({ ...preview, group_id: 'another' }, selection, 1000000), false);
});

test('task budget attribution is explicit and a preview cannot switch its frozen acceptance task', () => {
  assert.deepEqual(testTaskBody(false, ''), {});
  assert.deepEqual(testTaskBody(true, 'issue-267'), { test_task_id: 'issue-267' });
  for (const invalid of ['', null, 'new task', 'a'.repeat(101), 'issue/267'])
    assert.equal(testTaskBody(true, invalid), null);
  const selection = {
      profile_id: 'fl',
      group_id: null,
      keep_on: 'idle',
      hold_seconds: 0,
      test_task_id: 'issue-267',
    },
    preview = {
      capacity_backend: 'dstack-v1',
      can_start: true,
      preview_id: 'p',
      expires_at: 1100,
      hard_deadline: null,
      lifecycle: lifecycle({ test_gpu_guard: { version: 2, task_id: 'issue-267' } }),
      hourly_cost_microusd: 850000,
      reservation_microusd: 920834,
    };
  assert.equal(previewCurrent(preview, selection, 1000000), true);
  assert.equal(
    previewCurrent(preview, { ...selection, test_task_id: 'another-task' }, 1000000),
    false,
  );
  assert.equal(previewCurrent({ ...preview, lifecycle: lifecycle() }, selection, 1000000), false);
});

test('missing queue estimates and costs are unavailable rather than zero or ready capacity', () => {
  assert.match(queueLabel({ waiting: 3, rough_wait_seconds: null }), /等待兼容/);
  assert.match(queueLabel({ waiting: 3, rough_wait_seconds: 600 }), /10 分钟/);
  assert.deepEqual(
    todayMetrics({
      usage: { today_cost_microusd: null, today_video_count: null, cost_basis: 'unavailable' },
    }),
    { cost: '—', count: '—', basis: '尚未确认' },
  );
  assert.equal(costLabel('approved_ceiling_estimate'), '上限估算');
});

test('actual detail and rows expose measured-only stats and never hide keep policy at intermediate widths', async () => {
  const path = fileURLToPath(new URL('./P4OperatorConsole.jsx', import.meta.url)),
    contents = await readFile(path, 'utf8');
  const bundled = await build({
    stdin: {
      contents:
        contents + "\nexport {I18nProvider as TestI18nProvider} from './i18n/I18nProvider.jsx';\n",
      resolveDir: path.replace(/[/\\][^/\\]+$/, ''),
      loader: 'jsx',
    },
    bundle: true,
    write: false,
    platform: 'node',
    format: 'cjs',
    packages: 'external',
    loader: { '.css': 'empty' },
  });
  const module = new Module(path);
  module.filename = path;
  module.paths = Module._nodeModulePaths(path.replace(/[/\\][^/\\]+$/, ''));
  module._compile(bundled.outputFiles[0].text, path);
  const node = {
    id: 'original',
    runtime_profile_id: 'runtime',
    provider: 'vast',
    mode: 'ref',
    state: 'ready',
    ready: true,
    observation_fresh: true,
    hard_deadline: null,
    hold_until: 0,
    lifecycle: lifecycle({ keep_on: 'until_stopped' }),
    usage: {
      today_cost_microusd: 250000,
      today_cost_basis: 'approved_ceiling_estimate',
      today_video_count: 2,
      timezone: 'Australia/Sydney',
    },
    metrics: {
      average_generate_seconds: null,
      average_generation_cost_microusd: null,
      average_completion_seconds: 210,
      samples: 2,
      basis: 'ledger_submit_to_validated_result',
    },
  };
  const row = renderWithLocale(
    module,
    React.createElement(module.exports.MachineRow, { node, profiles: [], onDetail: () => {} }),
  );
  assert.match(row, /直到我手动停止/);
  assert.match(row, /p4-keep/);
  const ended = renderWithLocale(
    module,
    React.createElement(module.exports.MachineRow, {
      node: { ...node, state: 'stopped', lifecycle_phase: 'stopped' },
      profiles: [],
      onDetail: () => {},
    }),
  );
  assert.match(ended, /已结束/);
  assert.doesNotMatch(ended, /等待准备完成/);
  const details = renderWithLocale(
    module,
    React.createElement(module.exports.MachineDetail, {
      node,
      profiles: [],
      groups: [],
      onClose: () => {},
    }),
  );
  assert.match(details, /平均生成耗时/);
  assert.match(details, /暂无样本/);
  assert.match(details, /平均提交至交付 210 秒/);
  assert.match(details, /今日总费用.*上限估算/);
  assert.match(details, /今日已生成视频/);
  assert.doesNotMatch(details, /热机.*相同设置/);
  const css = await readFile(new URL('./p4-operator.css', import.meta.url), 'utf8');
  assert.doesNotMatch(css, /\.p4-keep\s*\{[^}]*display\s*:\s*none/);
});

const base = {
  id: 'original',
  owner_id: 'supervan',
  provider: 'vastai',
  mode: 'fl',
  runtime_profile_id: 'pruned',
  state: 'starting',
  desired_state: 'running',
  ready: false,
  observation_fresh: true,
  lifecycle_phase: 'preparing',
};
const progress = (changes) => ({
  phase: 'downloading_weights',
  started_at: 1000,
  phase_started_at: 1300,
  updated_at: 1600,
  elapsed_seconds: 600,
  phase_elapsed_seconds: 300,
  observation_age_seconds: 5,
  stale: false,
  stalled: false,
  reason_code: null,
  reason_text: null,
  download: {
    bytes_complete: 1024 ** 3,
    bytes_total: 2 * 1024 ** 3,
    files_complete: 1,
    files_total: 3,
    percent: 50,
  },
  image: null,
  model: { state: 'not_loaded', updated_at: 1500 },
  ...changes,
});

test('observed model-load phase does not create ready capacity or alter the original job identity', () => {
  const node = {
    ...base,
    current_job: { job_id: 'first-original-job' },
    preparation: progress({ phase: 'loading_model', model: { state: 'loading' } }),
  };
  assert.equal(preparationView(node).status.label, '加载模型中');
  assert.equal(
    phase(node).id,
    'running',
    'the original busy slot remains the scheduling authority',
  );
  assert.equal(node.ready, false);
  assert.equal(node.current_job.job_id, 'first-original-job');
  const lazy = preparationView({ ...base, preparation: progress({ phase: 'awaiting_first_job' }) });
  assert.match(lazy.modelLabel, /将在首个任务加载/);
  assert.equal(preparationSteps(lazy).find((step) => step.id === 'loading_model').pending, true);
  const premature = preparationView({
    ...base,
    preparation: progress({ phase: 'ready', model: { state: 'loaded' } }),
  });
  assert.match(
    premature.status.label,
    /等待执行槽/,
    'runtime observation alone cannot grant a business worker',
  );
  for (const oldPhase of ['ready', 'awaiting_first_job', 'downloading_weights'])
    assert.equal(
      preparationView({
        ...node,
        preparation: progress({ phase: oldPhase, model: { state: 'loaded' } }),
      }).status.id,
      'running_job',
      'an old preparation phase cannot hide the current business job',
    );
  assert.equal(
    preparationView({
      ...base,
      ready: true,
      preparation: progress({ phase: 'downloading_weights', stale: true }),
    }).status.id,
    'ready',
    'a fresh ready worker dominates old preparation observations',
  );
  assert.equal(
    preparationView({
      ...node,
      preparation: progress({ phase: 'loading_model', stale: true, model: { state: 'loading' } }),
    }).status.id,
    'running_job',
    'a stale model observation cannot replace the live job stage',
  );
});

test('download counters preserve partial and zero values without inventing percentages or totals', () => {
  assert.deepEqual(transferProgress(null), { label: '下载量尚未上报', percent: null });
  assert.equal(transferProgress({ bytes_complete: 1024 ** 3 }).label, '已下载 1.00 GiB');
  assert.equal(transferProgress({ bytes_complete: 0, bytes_total: 2048 }).label, '0 B / 2.00 KiB');
  assert.equal(
    transferProgress({ bytes_complete: 1024, bytes_total: 2048 }).percent,
    null,
    'no percentage was actually reported',
  );
  assert.equal(
    transferProgress({ bytes_complete: 1024, percent: 50 }).percent,
    null,
    'unknown total cannot support a percent bar',
  );
  assert.equal(
    transferProgress({ bytes_complete: 4096, bytes_total: 2048, percent: 100 }).percent,
    null,
    'inconsistent total is not a completion proof',
  );
  assert.equal(
    transferProgress({ bytes_complete: -1, bytes_total: NaN, files_complete: -3, percent: 50 })
      .percent,
    null,
  );
  assert.equal(transferProgress(progress().download).percent, 50);
  assert.equal(
    transferProgress({ ...progress().download, basis: 'completed_files' }).label,
    '已完成文件 1.00 GiB / 2.00 GiB · 1 / 3 个文件',
  );
  assert.match(
    transferProgress({ bytes_complete: 1024, extracted_bytes: 2048 }).label,
    /已解压 2.00 KiB/,
  );
  assert.equal(
    transferProgress({
      bytes_complete: 3 * 1024 ** 3,
      bytes_total: 6 * 1024 ** 3,
      downloaded_bytes: 2 * 1024 ** 3,
      extracted_bytes: 1024 ** 3,
      percent: 50,
      basis: 'download_and_extract',
    }).label,
    '拉取＋解压 3.00 GiB / 6.00 GiB · 已下载 2.00 GiB · 已解压 1.00 GiB',
  );
});

test('stale and stalled progress remain observations rather than fabricated machine failures', () => {
  const node = {
    ...base,
    preparation: progress({
      stale: true,
      stalled: true,
      reason_code: 'weight_download_not_advancing',
      reason_text: '权重下载尚未继续',
    }),
  };
  const view = preparationView(node);
  assert.equal(view.status.id, 'downloading_weights');
  assert.equal(view.stale, true);
  assert.equal(view.stalled, true);
  assert.equal(view.elapsedSeconds, 600);
  assert.equal(view.phaseElapsed, 300);
  assert.equal(node.state, 'starting');
  assert.equal(view.reasonCode, 'weight_download_not_advancing');
  const ended = preparationView({ ...node, state: 'stopped' });
  assert.equal(ended.status.id, 'stopped');
  assert.equal(ended.elapsedSeconds, 600);
  assert.equal(preparationSteps(ended).at(-1).id, 'stopped');
  const older = preparationView(base);
  assert.equal(older.source, null);
  assert.equal(older.elapsedSeconds, null);
  assert.equal(older.download.percent, null);
  assert.equal(
    preparationView({ ...base, reason_code: '<script>invalid</script>' }).reasonCode,
    null,
  );
});

test('machine rows and detail share live amounts, elapsed, safe causes and distinct model loading', async () => {
  const path = fileURLToPath(new URL('./P4OperatorConsole.jsx', import.meta.url)),
    contents = await readFile(path, 'utf8');
  const bundled = await build({
    stdin: {
      contents:
        contents + "\nexport {I18nProvider as TestI18nProvider} from './i18n/I18nProvider.jsx';\n",
      resolveDir: path.replace(/[/\\][^/\\]+$/, ''),
      loader: 'jsx',
    },
    bundle: true,
    write: false,
    platform: 'node',
    format: 'cjs',
    packages: 'external',
    loader: { '.css': 'empty' },
  });
  const module = new Module(path);
  module.filename = path;
  module.paths = Module._nodeModulePaths(path.replace(/[/\\][^/\\]+$/, ''));
  module._compile(bundled.outputFiles[0].text, path);
  const node = {
    ...base,
    preparation: progress({
      stalled: true,
      stale: true,
      reason_code: 'weight_download_not_advancing',
      reason_text: '下载源暂未继续传输',
    }),
  };
  const row = renderWithLocale(
    module,
    React.createElement(module.exports.MachineRow, { node, profiles: [], onDetail: () => {} }),
  );
  assert.match(row, /下载权重中/);
  assert.match(row, /1.00 GiB \/ 2.00 GiB/);
  assert.match(row, /1 \/ 3 个文件/);
  assert.match(row, /启动至今 10 分钟.*本阶段 5 分钟/);
  assert.match(row, /下载源暂未继续传输/);
  assert.match(row, /状态观测已过期/);
  assert.doesNotMatch(row, /等待准备完成|机器失败/);
  assert.match(row, /p4-keep/);
  const detail = renderWithLocale(
    module,
    React.createElement(module.exports.MachineDetail, {
      node,
      profiles: [],
      groups: [],
      onClose: () => {},
    }),
  );
  assert.match(detail, /准备进度/);
  assert.match(detail, /当前阶段耗时/);
  assert.match(detail, /weight_download_not_advancing/);
  assert.match(detail, /将在首个任务加载/);
  assert.match(detail, /progress aria-label="权重下载进度" max="100" value="50"/);
  const loading = {
    ...base,
    current_job: { job_id: 'first-original-job' },
    preparation: progress({ phase: 'loading_model', model: { state: 'loading' } }),
  };
  const loadingRow = renderWithLocale(
    module,
    React.createElement(module.exports.MachineRow, {
      node: loading,
      profiles: [],
      onDetail: () => {},
    }),
  );
  assert.match(loadingRow, /加载模型中/);
  assert.match(loadingRow, /正在加载模型到 GPU/);
  assert.doesNotMatch(loadingRow, /#first-or/);
  const loadingDetail = renderWithLocale(
    module,
    React.createElement(module.exports.MachineDetail, {
      node: loading,
      profiles: [],
      groups: [],
      onClose: () => {},
    }),
  );
  assert.match(loadingDetail, /#first-or/);
  const missing = renderWithLocale(
    module,
    React.createElement(module.exports.MachinePreparation, { node: base, detail: true }),
  );
  assert.match(missing, /下载量尚未上报/);
  assert.match(missing, /尚未收到详细准备观测/);
  assert.doesNotMatch(missing, /<progress/);
  const ended = renderWithLocale(
    module,
    React.createElement(module.exports.MachinePreparation, {
      node: { ...node, state: 'stopped' },
      detail: true,
    }),
  );
  assert.match(ended, /本次启动历时/);
  assert.doesNotMatch(ended, /累计运行|启动至今/);
  const escaped = renderWithLocale(
    module,
    React.createElement(module.exports.MachinePreparation, {
      node: { ...base, preparation: progress({ reason_text: '<script>unsafe</script>' }) },
      detail: true,
    }),
  );
  assert.doesNotMatch(escaped, /<script>/);
  assert.match(escaped, /&lt;script&gt;/);
});

function renderWithLocale(module, element) {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: { getItem: () => 'zh' },
  });
  try {
    return renderToStaticMarkup(
      React.createElement(module.exports.TestI18nProvider, null, element),
    );
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'localStorage', descriptor);
    else delete globalThis.localStorage;
  }
}
