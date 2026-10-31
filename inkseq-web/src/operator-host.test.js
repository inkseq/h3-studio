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
  failureLabel,
  hostInventoryConfirmed,
  hostPolicyBody,
  hostPolicyReceipt,
  hostReasonText,
  inventoryError,
  inventoryOutcome,
  referenceHostOffers,
  validHostPage,
  validHostPolicyBody,
} from './operator-host-model.js';

const memory = () => {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
};
const policy = (extra = {}) => ({
  manual: 'default',
  effective: 'default',
  revision: 0,
  snapshot: 'a'.repeat(64),
  reason_code: null,
  expires_at: null,
  automatic_deny_until: null,
  ...extra,
});
const host = (id = '19492', extra = {}) => ({
  provider: 'vastai',
  machine_id: id,
  identity_confirmed: true,
  physical_id_source: 'vast_machine_id',
  policy: policy(),
  runs: 1,
  failure_count: 1,
  failure_rate: 1,
  last_failure: { stage: 'provisioning', reason_code: 'waiting_instance_limit_exceeded', at: 80 },
  last_observed_at: 90,
  latest_observation: null,
  rentals: [],
  groups: [],
  ...extra,
});
const page = (admin = false, rows = [host()], extra = {}) => ({
  account: 'superdan',
  admin,
  provider: 'vastai',
  as_of: 100,
  hosts: rows,
  limit: 100,
  offset: 0,
  has_more: false,
  ...extra,
});
const audit = (extra = {}) => ({
  account: 'superdan',
  admin: true,
  as_of: 100,
  events: [],
  limit: 100,
  offset: 0,
  has_more: false,
  ...extra,
});
function client(overrides = {}) {
  let account;
  return {
    setAccount: (value) => {
      account = value;
    },
    reset() {},
    state: async () => ({ operator: { account, admin: true }, nodes: [], groups: [], queue: [] }),
    catalog: async () => ({ profiles: [] }),
    adminState: async () => ({
      operator: { account, admin: true },
      nodes: [],
      groups: [],
      queue: [],
    }),
    hosts: async ({ admin }) => page(admin),
    hostAudit: async () => audit(),
    ...overrides,
  };
}
const receipt = (body) => ({
  state: 'completed',
  changed: true,
  host: { provider: 'vastai', machine_id: body.machine_id },
  policy: policy({
    manual: body.policy,
    effective: body.policy,
    revision: body.expected_revision + 1,
    snapshot: 'b'.repeat(64),
    reason_code: body.reason_code,
  }),
  audit_event_id: 'original-audit',
  command_id: 'original-command',
});
const offer = (machine = '152440', offerId = '53646994', extra = {}) => ({
  backend: 'vastai',
  offer_id: offerId,
  region: 'lv-latvia',
  gpu_type: 'RTX5090',
  gpu_count: 1,
  cpu_count: 24,
  memory_gib: 32,
  gpu_memory_gib: 32,
  disk_gib: 128,
  hourly_cost_microusd: 690370,
  inventory_confirmed: true,
  host_history: [
    host(machine, {
      offer_id: offerId,
      capacity_profile_id: 'approved-profile',
      offer_observed_at: 90,
      selection_expires_at: 120,
    }),
  ],
  ...extra,
});

test('host API separates owner and admin reads and preserves original cookie actor/key/body', async () => {
  const calls = [],
    api = createP4OperatorClient({
      fetcher: async (path, options) => {
        calls.push({ path, options });
        const value =
          options.method === 'POST'
            ? receipt(JSON.parse(options.body))
            : path.includes('/audit')
              ? audit()
              : page(path.includes('/admin/'));
        return new Response(JSON.stringify(value), {
          headers: { 'Content-Type': 'application/json', 'X-Authenticated-Account': 'superdan' },
        });
      },
    });
  api.setAccount('superdan');
  await api.hosts();
  await api.hosts({ admin: true, offset: 100 });
  await api.hostAudit();
  const body = hostPolicyBody(host(), 'deny');
  await api.updateHostPolicy(body, 'same-original-key');
  assert.deepEqual(
    calls.map((call) => call.path),
    [
      '/v1/operator/hosts?limit=100&offset=0',
      '/v1/operator/admin/hosts?limit=100&offset=100',
      '/v1/operator/admin/hosts/audit?limit=100&offset=0',
      '/v1/operator/admin/hosts/policy',
    ],
  );
  for (const { options } of calls) {
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.headers['X-Expected-Account'], 'superdan');
    assert.equal(options.headers.Authorization, undefined);
  }
  assert.equal(calls[3].options.headers['Idempotency-Key'], 'same-original-key');
  assert.deepEqual(JSON.parse(calls[3].options.body), body);
});

test('host pages reject account/admin drift and unconfirmed physical identity without interpreting missing timings as zero', () => {
  assert.equal(validHostPage(page(), 'superdan'), true);
  assert.equal(validHostPage(page(true), 'superdan'), false);
  assert.equal(validHostPage(page(false, [host()], { account: 'supervan' }), 'superdan'), false);
  for (const extra of [
    { machine_id: null },
    { machine_id: 'offer-19492' },
    { identity_confirmed: false },
    { physical_id_source: 'offer_id' },
    { failure_rate: NaN },
    { groups: [{ profile_id: 'private', mode: 'fl' }] },
  ])
    assert.equal(validHostPage(page(false, [host('19492', extra)]), 'superdan'), false);
  assert.equal(validHostPage(page(false, [host(), host()]), 'superdan'), false);
  assert.equal(
    failureLabel({ stage: 'model_load_unconfirmed', reason_code: 'model_load_unconfirmed' }),
    '模型加载原因未确认，不认定供应商故障',
  );
});

test('merged reference offers retain each physical host and enforce native offer/profile/freshness/deny proof', () => {
  const safe = offer(),
    other = offer('153816', '53646995'),
    groups = referenceHostOffers([safe, other], 'approved-profile', 100000);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].count, 2);
  assert.equal(groups[0].confirmed, 2);
  assert.deepEqual(
    groups[0].members.map((row) => row.host_history[0].machine_id),
    ['152440', '153816'],
  );
  for (const change of [
    { offer_id: 'other' },
    { host_history: [] },
    { host_history: [{ ...safe.host_history[0], identity_confirmed: false }] },
    { host_history: [{ ...safe.host_history[0], selection_expires_at: 99 }] },
    { host_history: [{ ...safe.host_history[0], offer_observed_at: 101 }] },
    { host_history: [{ ...safe.host_history[0], capacity_profile_id: 'different-profile' }] },
    {
      host_history: [
        {
          ...safe.host_history[0],
          policy: policy({ manual: 'prefer', effective: 'deny', automatic_deny_until: 200 }),
        },
      ],
    },
  ])
    assert.equal(hostInventoryConfirmed({ ...safe, ...change }, 'approved-profile', 100000), false);
  assert.equal(
    referenceHostOffers(
      [safe, offer('153816', '53646995', { host_history: [] })],
      'approved-profile',
      100000,
    )[0].confirmed,
    1,
  );
});

test('exact no-offer and no-joint-host advisories are empty; transport/auth/mapping uncertainty stays failed', () => {
  for (const code of ['dstack_no_compatible_offers', 'dstack_no_compatible_host_offers']) {
    for (const detail of [{ code }, { detail: code }])
      assert.deepEqual(inventoryError({ status: 409, detail }), {
        state: 'empty',
        reasonCode: code,
      });
    for (const status of [401, 403, 429, 500, 503])
      assert.equal(inventoryError({ status, detail: { code } }).state, 'failed');
  }
  for (const error of [
    { message: 'dstack_no_compatible_host_offers' },
    { status: 503, detail: { code: 'provider_read_unconfirmed' } },
    { status: 429, detail: { code: 'dstack_provider_rate_limited' } },
    { status: 409, detail: { code: 'dstack_host_offers_unconfirmed' } },
    { status: 409, detail: { code: 'host_identity_unconfirmed' } },
    new Error('timeout'),
  ])
    assert.equal(inventoryError(error).state, 'failed');
  const empty = inventoryError({ detail: { code: 'dstack_no_compatible_host_offers' } });
  assert.equal(inventoryOutcome(empty, 'en').state, 'empty');
  assert.match(inventoryOutcome(empty, 'en').message, /no jointly eligible host/);
  assert.match(inventoryOutcome(empty, 'en').message, /does not mean the whole market is empty/);
  assert.match(inventoryOutcome(empty, 'zh').message, /不代表整个市场无货/);
  assert.match(inventoryOutcome({ state: 'failed' }, 'en').label, /Stock unknown/);
  assert.equal(empty.preview, undefined);
  assert.deepEqual(referenceHostOffers([], 'approved-profile'), []);
});

test('empty inventory renders current profile filters and price as advisory, with current EN/ZH wording', async () => {
  const path = fileURLToPath(new URL('./P4OperatorConsole.jsx', import.meta.url)),
    contents = await readFile(path, 'utf8'),
    bundled = await build({
      stdin: {
        contents:
          contents +
          "\nexport {I18nProvider as TestI18nProvider} from './i18n/I18nProvider.jsx';\n",
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
  const item = {
    profile: {
      memory_gib: 29,
      gpu_memory_gib: 30,
      cpu_count: 12,
      disk_gib: 350,
      max_price_microusd: 1500000,
    },
    ...inventoryError({ status: 409, detail: { code: 'dstack_no_compatible_host_offers' } }),
  };
  const en = renderWithLocale(
    module,
    React.createElement(module.exports.InventoryAdvisory, { item }),
    'en',
  );
  for (const value of [
    'role="status"',
    'RAM ≥ 29 GiB',
    'VRAM ≥ 30 GiB',
    'CPU ≥ 12',
    'Disk ≥ 350 GiB',
    'Price cap $1.50/hour',
    'no jointly eligible host',
    'Retry or adjust Settings',
  ])
    assert.ok(en.includes(value), value);
  assert.doesNotMatch(en, /Query failed|Stock unknown|dstack_no_compatible_host_offers/);
  const zh = renderWithLocale(
    module,
    React.createElement(module.exports.InventoryAdvisory, { item }),
    'zh',
  );
  for (const value of [
    '内存 ≥ 29 GiB',
    '磁盘 ≥ 350 GiB',
    '价格上限 $1.50/小时',
    '不代表整个市场无货',
  ])
    assert.ok(zh.includes(value), value);
  const failed = renderWithLocale(
    module,
    React.createElement(module.exports.InventoryAdvisory, {
      item: {
        profile: item.profile,
        ...inventoryError({
          status: 429,
          message: 'Rate limited',
          detail: { code: 'dstack_provider_rate_limited' },
        }),
      },
    }),
    'en',
  );
  assert.match(failed, /p4-error/);
  assert.match(failed, /Rate limited/);
  assert.doesNotMatch(failed, /Current filters|no jointly eligible host/);
});

test('policy requests use closed reasons and original physical ID/revision/snapshot only', () => {
  const body = hostPolicyBody(host(), 'deny');
  assert.deepEqual(body, {
    provider: 'vastai',
    machine_id: '19492',
    policy: 'deny',
    reason_code: 'operator_host_denied',
    expected_revision: 0,
    expected_snapshot: 'a'.repeat(64),
  });
  assert.equal(validHostPolicyBody(body), true);
  for (const changed of [
    { ...body, machine_id: 'offer-19492' },
    { ...body, reason_code: 'free raw text' },
    { ...body, expected_snapshot: 'unknown' },
    { ...body, owner_id: 'other' },
    { ...body, policy: 'rent-now' },
  ])
    assert.equal(validHostPolicyBody(changed), false);
  assert.equal(hostPolicyBody(host('19492', { identity_confirmed: false }), 'deny'), null);
});

test('explicit admin numeric target lookup reads real default policy without inventing rental history or enabling an offer', async () => {
  const target = host('19492', {
    identity_confirmed: false,
    physical_id_source: 'operator_supplied_numeric',
    runs: 0,
    failure_count: 0,
    failure_rate: null,
    last_failure: null,
    last_observed_at: null,
  });
  assert.equal(validHostPage(page(true, [target]), 'superdan', { admin: true }), true);
  assert.equal(validHostPage(page(false, [target]), 'superdan'), false);
  assert.equal(hostPolicyBody(target, 'deny')?.expected_revision, 0);
  assert.equal(
    hostInventoryConfirmed(
      offer('19492', '123', {
        host_history: [
          {
            ...target,
            offer_id: '123',
            capacity_profile_id: 'approved-profile',
            offer_observed_at: 90,
            selection_expires_at: 120,
          },
        ],
      }),
      'approved-profile',
      100000,
    ),
    false,
  );
  const calls = [],
    api = createP4OperatorClient({
      fetcher: async (path, options) => {
        calls.push({ path, options });
        return new Response(JSON.stringify(page(true, [target])), {
          headers: { 'Content-Type': 'application/json', 'X-Authenticated-Account': 'superdan' },
        });
      },
    });
  api.setAccount('superdan');
  await api.hosts({ admin: true, machine_id: '19492' });
  assert.equal(calls[0].path, '/v1/operator/admin/hosts?limit=100&offset=0&machine_id=19492');
  await assert.rejects(api.hosts({ machine_id: '19492' }));
  assert.equal(calls.length, 1);
  const controller = createP4OperatorController({
    storage: memory(),
    client: client({ hosts: async () => page(true, [target]) }),
  });
  await controller.setAccount('superdan');
  await controller.readHostTarget('19492');
  assert.equal(controller.getState().hostTarget.identity_confirmed, false);
  assert.deepEqual(controller.getState().hostTarget.rentals, []);
  assert.equal(controller.getState().pending, null);
  controller.destroy();
});

test('non-admin or missing original identity cannot persist or send a policy command or rental', async () => {
  for (const [admin, row] of [
    [false, host()],
    [true, host(null)],
  ]) {
    let writes = 0,
      starts = 0;
    const controller = createP4OperatorController({
      storage: memory(),
      client: client({
        state: async () => ({
          operator: { account: 'superdan', admin },
          nodes: [],
          groups: [],
          queue: [],
        }),
        updateHostPolicy: async () => {
          writes++;
        },
        start: async () => {
          starts++;
        },
      }),
    });
    await controller.setAccount('superdan');
    await assert.rejects(controller.updateHostPolicy(row, 'deny'));
    assert.equal(writes, 0);
    assert.equal(starts, 0);
    assert.equal(controller.getState().pending, null);
    controller.destroy();
  }
});

test('unknown policy response reload/recovery uses the identical original body and key without automatic retry', async () => {
  const calls = [],
    storage = memory();
  let lost = true;
  const api = client({
    updateHostPolicy: async (body, key) => {
      calls.push({ body: structuredClone(body), key });
      if (lost) throw Error('lost');
      return receipt(body);
    },
  });
  const first = createP4OperatorController({ storage, client: api });
  await first.setAccount('superdan');
  await assert.rejects(first.updateHostPolicy(host(), 'deny'));
  const original = structuredClone(first.getState().pending);
  await assert.rejects(first.updateHostPolicy(host('152440'), 'prefer'), /原操作结果/);
  assert.equal(calls.length, 1);
  first.destroy();
  const restored = createP4OperatorController({ storage, client: api });
  await restored.setAccount('superdan');
  assert.deepEqual(restored.getState().pending, original);
  lost = false;
  await restored.recover();
  assert.deepEqual(calls[1], calls[0]);
  assert.equal(restored.getState().pending, null);
  assert.equal(calls.length, 2);
  restored.destroy();
});

test('only exact completed identity/version/audit receipt clears original policy uncertainty', async () => {
  const body = hostPolicyBody(host(), 'deny');
  for (const extra of [
    { state: 'accepted' },
    { host: { provider: 'vastai', machine_id: 'other' } },
    { audit_event_id: null },
    { command_id: null },
    {
      policy: policy({
        manual: 'prefer',
        effective: 'prefer',
        revision: 1,
        snapshot: 'b'.repeat(64),
      }),
    },
    { policy: policy({ manual: 'deny', effective: 'deny' }) },
  ]) {
    const value = { ...receipt(body), ...extra };
    assert.equal(hostPolicyReceipt(value, body), false);
    const controller = createP4OperatorController({
      storage: memory(),
      client: client({ updateHostPolicy: async () => value }),
    });
    await controller.setAccount('superdan');
    await assert.rejects(controller.updateHostPolicy(host(), 'deny'), /策略收据/);
    assert.equal(controller.getState().pending.method, 'updateHostPolicy');
    controller.destroy();
  }
});

test('atomic stale-policy rejection preserves original data and never auto-rents or retries', async () => {
  let writes = 0;
  const original = host(),
    saved = structuredClone(original),
    controller = createP4OperatorController({
      storage: memory(),
      client: client({
        updateHostPolicy: async () => {
          writes++;
          throw Object.assign(Error('operator_host_policy_changed'), { status: 409 });
        },
      }),
    });
  await controller.setAccount('superdan');
  await assert.rejects(controller.updateHostPolicy(original, 'deny'), /policy_changed/);
  assert.equal(writes, 1);
  assert.equal(controller.getState().pending, null);
  assert.deepEqual(original, saved);
  controller.destroy();
});

test('same-account admin demotion discards an in-flight aggregate host page and clears audits', async () => {
  let admin = true,
    resolve;
  const api = client({
    state: async () => ({
      operator: { account: 'superdan', admin },
      nodes: [],
      groups: [],
      queue: [],
    }),
    hosts: () =>
      new Promise((done) => {
        resolve = done;
      }),
  });
  const controller = createP4OperatorController({ storage: memory(), client: api });
  await controller.setAccount('superdan');
  await controller.readHostAudit();
  const reading = controller.readHosts({ admin: true });
  admin = false;
  await controller.refresh();
  resolve(page(true));
  await assert.rejects(reading, /权限已改变/);
  assert.equal(controller.getState().adminHosts, null);
  assert.equal(controller.getState().hostAudits, null);
  controller.destroy();
});

test('policy permission rejection removes administrator records and performs no retry', async () => {
  let writes = 0;
  const controller = createP4OperatorController({
    storage: memory(),
    client: client({
      updateHostPolicy: async () => {
        writes++;
        throw Object.assign(Error('admin_only'), { status: 403 });
      },
    }),
  });
  await controller.setAccount('superdan');
  await controller.readHosts({ admin: true });
  await controller.readHostAudit();
  await assert.rejects(controller.updateHostPolicy(host(), 'deny'));
  assert.equal(writes, 1);
  assert.equal(controller.getState().pending, null);
  assert.equal(controller.getState().adminHosts, null);
  assert.equal(controller.getState().hostAudits, null);
  assert.equal(controller.getState().snapshot.operator.admin, false);
  controller.destroy();
});

test('original unknown policy recovery rechecks account epoch after admin-read emission', async () => {
  const calls = [],
    storage = memory();
  let account = 'superdan';
  const api = client({
    setAccount: (value) => {
      account = value;
    },
    state: async () => ({ operator: { account, admin: true }, nodes: [], groups: [], queue: [] }),
    adminState: async () => ({
      operator: { account, admin: true },
      nodes: [],
      groups: [],
      queue: [],
    }),
    updateHostPolicy: async (body, key) => {
      calls.push({ account, body, key });
      throw Error('lost');
    },
  });
  const controller = createP4OperatorController({ storage, client: api });
  await controller.setAccount('superdan');
  await assert.rejects(controller.updateHostPolicy(host(), 'deny'));
  const original = structuredClone(controller.getState().pending),
    prior = controller.getState().adminSnapshot;
  let changed = false;
  const unsubscribe = controller.subscribe(() => {
    if (
      !changed &&
      controller.getState().adminSnapshot !== prior &&
      controller.getState().adminSnapshot?.operator.account === 'superdan'
    ) {
      changed = true;
      void controller.setAccount('supervan');
    }
  });
  await assert.rejects(controller.recover(), /账户已改变/);
  assert.equal(calls.length, 1);
  assert.deepEqual(
    JSON.parse(storage.getItem('sixnine:operator:dstack:pending:superdan')),
    original,
  );
  assert.equal(controller.getState().adminHosts, null);
  unsubscribe();
  controller.destroy();
});

test('bounded host pagination rejects wrong offsets and replaces duplicate original host records', async () => {
  let wrong = true;
  const controller = createP4OperatorController({
    storage: memory(),
    client: client({
      hosts: async ({ admin, offset }) =>
        offset
          ? page(admin, [host('152440')], { offset: wrong ? 0 : 100 })
          : page(admin, [host()], { has_more: true }),
    }),
  });
  await controller.setAccount('superdan');
  await controller.readHosts();
  await assert.rejects(controller.readHosts({ offset: 100 }), /分页/);
  assert.deepEqual(
    controller.getState().hosts.hosts.map((row) => row.machine_id),
    ['19492'],
  );
  wrong = false;
  await controller.readHosts({ offset: 100 });
  assert.deepEqual(
    controller.getState().hosts.hosts.map((row) => row.machine_id),
    ['19492', '152440'],
  );
  controller.destroy();
});

test('real host rendering labels allocated observations, separate IDs, controls and quote estimates without private payloads', async () => {
  const path = fileURLToPath(new URL('./OperatorHostsPanel.jsx', import.meta.url)),
    contents = await readFile(path, 'utf8'),
    bundled = await build({
      stdin: {
        contents:
          contents +
          "\nexport {I18nProvider as TestI18nProvider} from './i18n/I18nProvider.jsx';\n",
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
  const original = host('152440', {
    latest_observation: {
      observed_at: 90,
      region: 'lv-latvia',
      gpu: 'RTX5090',
      gpu_count: 1,
      allocated_ram_mib: 33004,
      allocated_vram_mib: 32768,
      cpu: 24,
      reliability: null,
      quoted_hourly_microusd: 690370,
    },
    rentals: [
      {
        node_id: 'original-node',
        dstack_run_id: 'original-run',
        offer_id: '53646994',
        provider_instance_id: '55862941',
        state: 'terminated',
        stage: 'runtime_ready',
        observed_at: 90,
        quoted_hourly_microusd: 690370,
        region: 'lv-latvia',
        profile_id: 'original-profile',
        mode: 'fl',
      },
    ],
    groups: [
      {
        profile_id: 'original-profile',
        mode: 'fl',
        resolution: '480P',
        steps: 20,
        warmth: 'warm',
        samples: 1,
        cold_start_samples: 0,
        generation_samples: 1,
        median_cold_start_s: null,
        median_generation_s: 140,
        cost_per_video_microusd: 26855,
        cost_basis: 'quote_estimate',
        download_mb_s: null,
      },
    ],
    private_prompt: 'never-render-this',
  });
  const html = renderWithLocale(
    module,
    React.createElement(module.exports.HostRecord, { host: original }),
  );
  for (const text of [
    '物理主机目标 #152440',
    '已分配内存',
    'original-node',
    'original-run',
    '53646994',
    '55862941',
    '20 步',
    '报价估算',
    '未观测',
  ])
    assert.ok(html.includes(text));
  assert.doesNotMatch(html, /never-render-this|已结算账单|内存 515620/);
  const merged = renderWithLocale(
    module,
    React.createElement(module.exports.OfferHostHistory, {
      members: [offer(), offer('153816', '53646995')],
      profileId: 'approved-profile',
      now: 100000,
    }),
  );
  assert.match(merged, /#152440/);
  assert.match(merged, /#153816/);
  assert.match(merged, /Offer #53646994/);
  assert.match(merged, /Offer #53646995/);
});

function renderWithLocale(module, element, locale = 'zh') {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: { getItem: () => locale },
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

test('host policy reasons read as words in both languages; unknown codes stay visible', () => {
  assert.equal(hostReasonText('automatic_slow_download', 'zh'), '自动禁止：模型下载太慢');
  assert.equal(
    hostReasonText('automatic_slow_download', 'en'),
    'denied automatically: model download too slow',
  );
  assert.equal(hostReasonText('operator_host_denied', 'zh'), '管理员禁止');
  assert.equal(hostReasonText('some_future_reason', 'zh'), 'some_future_reason');
});
