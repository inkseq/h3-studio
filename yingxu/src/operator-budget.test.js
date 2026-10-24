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
import { batchRows, previewCurrent } from './p4-operator-model.js';
import {
  budgetCosts,
  budgetLabel,
  budgetUsage,
  priceRows,
  referenceOffers,
  rowPrice,
  usdInput,
  usdMicrousd,
  validBudgetPage,
  wholeGiB,
  withDefaultPrices,
} from './operator-budget-model.js';

const memory = () => {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
};
const row = (patch = {}) => ({
  id: 'owner-budget',
  owner_id: 'superdan',
  limit_microusd: 100000000,
  spent_microusd: 20000000,
  reserved_microusd: 60000000,
  remaining_microusd: 20000000,
  status: 'warning',
  snapshot: 'a'.repeat(64),
  daily7: [{ date: '2026-10-14', cost_microusd: 1000000 }],
  unattributed_legacy: { cost_microusd: 2000000, reservation_count: 1 },
  ...patch,
});
const page = (budgets = [row()], patch = {}) => ({
  account: 'superdan',
  basis: 'cumulative',
  currency: 'USD',
  timezone: 'UTC',
  budgets,
  as_of: 1792000000,
  next_after_id: null,
  ...patch,
});
function fake(overrides = {}) {
  let account;
  return {
    setAccount: (value) => {
      account = value;
    },
    reset() {},
    state: async () => ({ operator: { account, admin: true }, nodes: [], groups: [], queue: [] }),
    adminState: async () => ({
      operator: { account, admin: true },
      nodes: [],
      groups: [],
      queue: [],
    }),
    catalog: async () => ({ profiles: [] }),
    adminBudgets: async () => page([row()], { account, admin: true }),
    adminPrices: async () => ({ operator: { account, admin: true }, profiles: [], audits: [] }),
    budgetAudit: async () => ({ events: [], next_before: null }),
    ...overrides,
  };
}

test('money entry is exact to six decimals and rejects scientific, negative and unsafe input', () => {
  assert.equal(usdMicrousd('0.73037'), 730370);
  assert.equal(usdMicrousd('100.8'), 100800000);
  assert.equal(usdMicrousd('0.000001'), 1);
  assert.equal(usdInput(730370), '0.73037');
  for (const value of ['-1', '1e6', '0.0000001', 'NaN', '01', '9007199255', '', null])
    assert.equal(usdMicrousd(value), null);
});

test('cumulative warning includes existing reservations and exhaustion never implies a reset', () => {
  assert.deepEqual(budgetUsage(row()), { used: 80000000, percent: 80, status: 'warning' });
  assert.equal(budgetUsage(row({ reserved_microusd: 80000000 })).status, 'exhausted');
  assert.equal(budgetUsage(row({ reserved_microusd: 59999999 })).status, 'ok');
  assert.equal(
    budgetUsage(row({ limit_microusd: 0, spent_microusd: 0, reserved_microusd: 0 })).status,
    'exhausted',
  );
});

test('retained financial labels distinguish owners, projects, shared accounts and retired caps', () => {
  assert.equal(budgetLabel(row({ owner_id: 'supervan' })), '账户预算 · supervan');
  assert.equal(
    budgetLabel(row({ owner_id: 'supervan', project_id: 'night-delivery' })),
    '项目预算 · supervan / night-delivery',
  );
  assert.equal(
    budgetLabel(row({ owner_id: null, id: 'demand-gpu-remaining-20261004' })),
    '共享 GPU 预算',
  );
  assert.equal(
    budgetLabel(row({ owner_id: null, id: 'demand-job-total-20261004' })),
    '共享生成预算',
  );
  assert.equal(
    budgetLabel(row({ owner_id: null, id: 'demand-gpu-remaining-20261004' }), 'en'),
    'Shared GPU budget',
  );
  assert.equal(budgetUsage(row({ limit_mode: 'settings', status: 'exhausted' })), null);
  assert.equal(budgetUsage(row({ legacy_projection_only: true, status: 'exhausted' })), null);
});

const costBreakdown = () => ({
  basis: 'original_account_reservations',
  account_layers_not_additive: true,
  job: {
    confirmed_cost_microusd: 0,
    confirmed_reservation_count: 0,
    pending_invoice_count: 181,
    pending_estimate_microusd: 162400000,
    pending_actual_cost_microusd: null,
  },
  instance: {
    confirmed_cost_microusd: 84715946,
    confirmed_reservation_count: 12,
    pending_invoice_count: 9,
    pending_estimate_microusd: 27759169,
    pending_actual_cost_microusd: null,
  },
});
test('invoice display keeps pending generation and known rental in their original non-additive account layer', () => {
  const costs = costBreakdown(),
    before = structuredClone(costs),
    view = budgetCosts(row({ cost_breakdown: costs }));
  assert.equal(view[0].confirmed_cost_microusd, 0);
  assert.equal(view[0].pending_invoice_count, 181);
  assert.equal(view[0].pending_estimate_microusd, 162400000);
  assert.equal(view[0].pending_actual_cost_microusd, null);
  assert.equal(view[1].confirmed_cost_microusd, 84715946);
  assert.deepEqual(costs, before);
  assert.deepEqual(budgetCosts(row()), []);
  assert.deepEqual(
    budgetCosts(
      row({ cost_breakdown: { ...costs, job: { ...costs.job, pending_actual_cost_microusd: 0 } } }),
    ),
    [],
  );
  assert.deepEqual(
    budgetCosts(row({ cost_breakdown: { ...costs, account_layers_not_additive: false } })),
    [],
  );
});

test('admin account pagination retains the original F guards and reaches shared rows after the first hundred', async () => {
  const requests = [],
    first = Array.from({ length: 100 }, (_, index) => row({ id: 'account-' + index }));
  let priceReads = 0;
  const api = fake({
    adminBudgets: async (options) => {
      requests.push(options);
      return options.after_id
        ? page(
            [
              row({ id: 'demand-gpu-remaining-20261004', owner_id: null }),
              row({ id: 'demand-job-total-20261004', owner_id: null }),
            ],
            { admin: true },
          )
        : page(first, { admin: true, next_after_id: 'account-99' });
    },
    adminPrices: async () => {
      priceReads++;
      return { operator: { account: 'superdan', admin: true }, profiles: [], audits: [] };
    },
  });
  const controller = createP4OperatorController({ client: api, storage: memory() });
  await controller.setAccount('superdan');
  await controller.readBudgetAdmin();
  await controller.readBudgetAdmin({ after_id: 'account-99' });
  assert.equal(controller.getState().adminBudgets.budgets.length, 102);
  assert.equal(controller.getState().adminBudgets.next_after_id, null);
  assert.deepEqual(requests, [{ after_id: undefined }, { after_id: 'account-99' }]);
  assert.equal(priceReads, 1);
  assert.deepEqual(
    controller
      .getState()
      .adminBudgets.budgets.slice(-2)
      .map((budget) => budgetLabel(budget)),
    ['共享 GPU 预算', '共享生成预算'],
  );
});

test('admin budget page responses are discarded when server permission is revoked', async () => {
  let allowed = false,
    reads = 0,
    resolvePage;
  const api = fake({
    state: async () => ({
      operator: { account: 'superdan', admin: allowed },
      nodes: [],
      groups: [],
      queue: [],
    }),
    adminBudgets: async (options) => {
      reads++;
      return options.after_id
        ? new Promise((resolve) => {
            resolvePage = resolve;
          })
        : page([row()], { admin: true, next_after_id: 'more' });
    },
  });
  const controller = createP4OperatorController({ client: api, storage: memory() });
  await controller.setAccount('superdan');
  await assert.rejects(controller.readBudgetAdmin(), /没有管理员权限/);
  assert.equal(reads, 0);
  allowed = true;
  await controller.refresh();
  await controller.readBudgetAdmin();
  const reading = controller.readBudgetAdmin({ after_id: 'more' });
  allowed = false;
  await controller.refresh();
  resolvePage(page([row({ id: 'shared', owner_id: null })], { admin: true }));
  await assert.rejects(reading, /管理员权限已改变/);
  assert.equal(controller.getState().adminBudgets, null);
});

test('owner read rejects another owner or a global row and binds ordinary cookie actor headers', async () => {
  const calls = [],
    values = [
      page([row({ owner_id: 'supervan' })]),
      page([row({ owner_id: null })]),
      page(),
      page([row({ owner_id: null })], { admin: true }),
    ];
  const api = createP4OperatorClient({
    fetcher: async (path, options) => {
      calls.push({ path, options });
      return new Response(JSON.stringify(values.shift()), {
        headers: { 'Content-Type': 'application/json', 'X-Authenticated-Account': 'superdan' },
      });
    },
  });
  api.setAccount('superdan');
  await assert.rejects(api.budgets(), /identity/);
  await assert.rejects(api.budgets(), /identity/);
  assert.equal((await api.budgets()).budgets[0].id, 'owner-budget');
  assert.equal((await api.adminBudgets()).admin, true);
  assert.equal(calls[0].path, '/v1/operator/budgets?limit=100');
  assert.equal(calls[3].path, '/v1/operator/admin/budgets?limit=100');
  assert.ok(
    calls.every(
      (call) =>
        call.options.credentials === 'same-origin' &&
        call.options.headers['X-Expected-Account'] === 'superdan',
    ),
  );
});

test('a budget response cannot survive an account switch or fabricate monthly/daily zero balances', async () => {
  let resolve;
  const api = createP4OperatorClient({
    fetcher: () =>
      new Promise((done) => {
        resolve = done;
      }),
  });
  api.setAccount('superdan');
  const reading = api.budgets();
  await Promise.resolve();
  api.setAccount('supervan');
  resolve(
    new Response(JSON.stringify(page()), { headers: { 'Content-Type': 'application/json' } }),
  );
  await assert.rejects(reading, /account has changed/);
  assert.equal(validBudgetPage(page([], {}), 'superdan'), true);
  assert.equal(validBudgetPage(page([], { basis: 'monthly' }), 'superdan'), false);
  assert.equal(page().budgets[0].daily7.length, 1);
});

test('each original configuration retains its exact-profile cap across mode and catalog changes', () => {
  const profiles = [
    { id: 'fl', max_price_microusd: 850000, price_ceiling_microusd: 1000000 },
    { id: 'ref', max_price_microusd: 900000, price_ceiling_microusd: 1100000 },
  ];
  const [original] = withDefaultPrices([{ id: 'row', profile_id: 'fl', quantity: 2 }], profiles),
    edited = { ...original, price_caps: { ...original.price_caps, fl: '0.7' } };
  assert.equal(rowPrice(edited, profiles[0]), 700000);
  assert.equal(rowPrice({ ...edited, profile_id: 'ref' }, profiles[1]), 900000);
  assert.equal(rowPrice(edited, { ...profiles[0], max_price_microusd: 950000 }), 700000);
  assert.deepEqual(
    priceRows([edited], profiles, batchRows([edited], profiles)).map(
      (item) => item.max_price_microusd,
    ),
    [700000, 700000],
  );
  assert.equal(
    priceRows(
      [{ ...edited, price_caps: { fl: '1.000001' } }],
      profiles,
      batchRows([edited], profiles),
    ),
    null,
  );
});

test('selected price must be frozen and echoed by the individual preview', () => {
  const selection = {
      profile_id: 'fl',
      group_id: null,
      keep_on: 'idle',
      idle_shutdown_seconds: 600,
      max_price_microusd: 700000,
    },
    preview = {
      capacity_backend: 'dstack-v1',
      can_start: true,
      preview_id: 'original-preview',
      profile_id: 'fl',
      expires_at: 1100,
      hard_deadline: null,
      lifecycle: {
        version: 2,
        keep_on: 'idle',
        idle_shutdown_seconds: 600,
        hold_until: 0,
        reservation_seconds: 3600,
        expected_runtime_s: 1800,
        budget_account_ids: ['owner-budget'],
      },
      hourly_cost_microusd: 700000,
      max_price_microusd: 700000,
      reservation_microusd: 700000,
    };
  assert.equal(previewCurrent(preview, selection, 1000000), true);
  assert.equal(
    previewCurrent({ ...preview, max_price_microusd: 850000 }, selection, 1000000),
    false,
  );
  assert.equal(
    previewCurrent({ ...preview, hourly_cost_microusd: 850000 }, selection, 1000000),
    false,
  );
  assert.equal(
    previewCurrent({ ...preview, max_price_microusd: undefined }, selection, 1000000),
    false,
  );
});

test('identical offers merge without manufacturing physical inventory and RAM shows whole GiB', () => {
  const offer = {
    backend: 'vast',
    region: 'lv-latvia',
    gpu_type: 'RTX5090',
    gpu_count: 1,
    cpu_count: 24,
    memory_gib: 32.2304,
    gpu_memory_gib: 32,
    disk_gib: 128,
    hourly_cost_microusd: 690370,
    inventory_confirmed: true,
  };
  const groups = referenceOffers([
    { ...offer, offer_id: 'one' },
    { ...offer, offer_id: 'two' },
    { ...offer, offer_id: 'three', inventory_confirmed: false },
    { ...offer, region: 'us-louisiana' },
  ]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].count, 3);
  assert.equal(groups[0].confirmed, 2);
  assert.equal(groups[0].num_instances, undefined);
  assert.equal(wholeGiB(32.2304), '32');
  assert.equal(wholeGiB(null), '—');
});

test('unknown budget PUT is journaled once and reconciled by readback without another PUT', async () => {
  let writes = 0;
  const old = row(),
    updated = row({ limit_microusd: 106000000, snapshot: 'b'.repeat(64) }),
    api = fake({
      updateBudget: async () => {
        writes++;
        throw Error('response lost');
      },
      adminBudgets: async () => page([updated], { admin: true }),
    }),
    storage = memory(),
    controller = createP4OperatorController({ client: api, storage });
  await controller.setAccount('superdan');
  await assert.rejects(controller.updateBudget(old, 106000000));
  const original = structuredClone(controller.getState().pending);
  assert.equal(original.body.expected_snapshot, old.snapshot);
  assert.equal(original.body.limit_microusd, 106000000);
  assert.equal(writes, 1);
  assert.equal((await controller.recover()).desired_values_confirmed, true);
  assert.equal(writes, 1);
  assert.equal(controller.getState().pending, null);
  assert.equal(updated.spent_microusd, old.spent_microusd);
  assert.equal(updated.reserved_microusd, old.reserved_microusd);
});

test('unconfirmed original budget values keep the journal and never blindly replay', async () => {
  let writes = 0;
  const controller = createP4OperatorController({
    client: fake({
      updateBudget: async () => {
        writes++;
        throw Error('lost');
      },
    }),
    storage: memory(),
  });
  await controller.setAccount('superdan');
  await assert.rejects(controller.updateBudget(row(), 106000000));
  const original = structuredClone(controller.getState().pending);
  await assert.rejects(controller.recover(), /尚未证实/);
  assert.deepEqual(controller.getState().pending, original);
  assert.equal(writes, 1);
});

test('price CAS preserves original snapshot/body and recovery is exact read-only', async () => {
  const policy = {
      id: 'fl',
      owner_id: 'superdan',
      max_price_microusd: 850000,
      price_ceiling_microusd: 1000000,
      snapshot: 'a'.repeat(64),
    },
    calls = [];
  const api = fake({
      updatePrice: async (id, body, key) => {
        calls.push({ id, body, key });
        throw Error('lost');
      },
      adminPrices: async () => ({
        operator: { account: 'superdan', admin: true },
        profiles: [{ ...policy, max_price_microusd: 700000, snapshot: 'b'.repeat(64) }],
        audits: [],
      }),
    }),
    controller = createP4OperatorController({ client: api, storage: memory() });
  await controller.setAccount('superdan');
  await assert.rejects(controller.updatePrice(policy, 700000, 1000000));
  assert.deepEqual(calls[0].body, {
    expected_snapshot: policy.snapshot,
    max_price_microusd: 700000,
    price_ceiling_microusd: 1000000,
  });
  await controller.recover();
  assert.equal(calls.length, 1);
  assert.equal(controller.getState().pending, null);
});

test('price change previews against the settings version, then writes reason and digest', async () => {
  const policy = {
      id: 'ref',
      owner_id: 'superdan',
      max_price_microusd: 850000,
      price_ceiling_microusd: 1000000,
      snapshot: 'a'.repeat(64),
    },
    calls = [];
  const preview = {
    expected_snapshot: policy.snapshot,
    expected_settings_version: 4,
    consequences: [],
    confirmation_digest: 'd'.repeat(64),
  };
  const api = fake({
      adminPrices: async () => ({
        operator: { account: 'superdan', admin: true },
        expected_settings_version: 4,
        profiles: [policy],
        audits: [],
      }),
      previewPrice: async (id, body) => {
        calls.push({ kind: 'preview', id, body });
        return preview;
      },
      updatePrice: async (id, body, key) => {
        calls.push({ kind: 'put', id, body, key });
        return {
          profile: { ...policy, ...body, snapshot: 'b'.repeat(64) },
          changed: true,
          audit_event_id: 'audit-1',
        };
      },
    }),
    controller = createP4OperatorController({ client: api, storage: memory() });
  await controller.setAccount('superdan');
  const reviewed = await controller.previewPrice(policy, 1500000, 3000000);
  assert.equal(reviewed, preview);
  assert.deepEqual(calls[0].body, {
    expected_snapshot: policy.snapshot,
    expected_settings_version: 4,
    max_price_microusd: 1500000,
    price_ceiling_microusd: 3000000,
  });
  await controller.updatePrice(policy, 1500000, 3000000, { preview, reason: ' owner #517 ' });
  assert.deepEqual(calls[1].body, {
    expected_snapshot: policy.snapshot,
    max_price_microusd: 1500000,
    price_ceiling_microusd: 3000000,
    expected_settings_version: 4,
    reason: 'owner #517',
    confirmation_digest: 'd'.repeat(64),
  });
  await assert.rejects(
    controller.previewPrice({ ...policy, snapshot: 'c'.repeat(64) }, 1500000, 3000000),
  );
  assert.equal(calls.length, 2);
});

test('admin permission change or stale budget CAS does not retry a mutation', async () => {
  let writes = 0;
  const controller = createP4OperatorController({
    client: fake({
      adminState: async () => ({
        operator: { account: 'superdan', admin: false },
        nodes: [],
        groups: [],
        queue: [],
      }),
      updateBudget: async () => {
        writes++;
      },
    }),
    storage: memory(),
  });
  await controller.setAccount('superdan');
  await assert.rejects(controller.updateBudget(row(), 106000000), /管理员身份/);
  assert.equal(writes, 0);
  assert.equal(controller.getState().pending, null);
  const conflict = createP4OperatorController({
    client: fake({
      updateBudget: async () => {
        writes++;
        throw Object.assign(Error('budget_snapshot_changed'), { status: 409 });
      },
    }),
    storage: memory(),
  });
  await conflict.setAccount('superdan');
  await assert.rejects(conflict.updateBudget(row(), 106000000));
  assert.equal(writes, 1);
  assert.equal(conflict.getState().pending, null);
});

test('parallel admin budget read discards all values after the actor changes', async () => {
  let resolve;
  const controller = createP4OperatorController({
    client: fake({
      adminBudgets: () =>
        new Promise((done) => {
          resolve = done;
        }),
    }),
    storage: memory(),
  });
  await controller.setAccount('superdan');
  const reading = controller.readBudgetAdmin();
  await controller.setAccount('supervan');
  resolve(page([row()], { admin: true }));
  await assert.rejects(reading, /账户已改变/);
  assert.equal(controller.getState().adminBudgets, null);
  assert.equal(controller.getState().adminPrices, null);
});

test('accepted admin change requires its original audited receipt before clearing the journal', async () => {
  const updated = row({ limit_microusd: 106000000, snapshot: 'b'.repeat(64) });
  for (const audit_event_id of [null, 'original-audit']) {
    const controller = createP4OperatorController({
      client: fake({
        updateBudget: async () => ({ budget: updated, changed: true, audit_event_id }),
      }),
      storage: memory(),
    });
    await controller.setAccount('superdan');
    if (audit_event_id) {
      assert.equal(
        (await controller.updateBudget(row(), 106000000)).audit_event_id,
        audit_event_id,
      );
      assert.equal(controller.getState().pending, null);
    } else {
      await assert.rejects(controller.updateBudget(row(), 106000000), /审核收据/);
      assert.equal(controller.getState().pending.body.expected_snapshot, row().snapshot);
    }
  }
});

test('actual budget cards show sparse recorded dates and legacy uncertainty without monthly resets', async () => {
  const path = fileURLToPath(new URL('./OperatorBudgetPanel.jsx', import.meta.url)),
    contents = await readFile(path, 'utf8'),
    directory = path.replace(/[/\\][^/\\]+$/, '');
  const result = await build({
      stdin: {
        contents:
          contents +
          "\nexport {I18nProvider as TestI18nProvider} from './i18n/I18nProvider.jsx';\n",
        resolveDir: directory,
        loader: 'jsx',
      },
      bundle: true,
      write: false,
      platform: 'node',
      format: 'cjs',
      packages: 'external',
      loader: { '.css': 'empty' },
    }),
    module = new Module(path);
  module.filename = path;
  module.paths = Module._nodeModulePaths(directory);
  module._compile(result.outputFiles[0].text, path);
  const markup = renderWithLocale(
    module,
    React.createElement(module.exports.BudgetAccounts, { budgets: [row()] }),
  );
  assert.match(markup, /累计额度/);
  assert.match(markup, /已结算/);
  assert.match(markup, /已预留/);
  assert.match(markup, /剩余额度/);
  assert.match(markup, /80%/);
  assert.match(markup, /2026-10-14/);
  assert.doesNotMatch(markup, /2026-10-13|月度|monthly/);
  assert.match(markup, /旧记录日期未确认/);
  assert.match(markup, /\$2\.00/);
  assert.match(markup, /没有记录的日期不视为零费用/);
  const exhausted = renderWithLocale(
    module,
    React.createElement(module.exports.BudgetAccounts, {
      budgets: [row({ reserved_microusd: 80000000, status: 'exhausted' })],
      compact: true,
    }),
  );
  assert.match(exhausted, /额度不足/);
  const retiredRow = row({
    limit_mode: 'settings',
    legacy_projection_only: true,
    status: 'exhausted',
    invoice_pending: {
      reservation_count: 2,
      reserved_estimate_microusd: 2000000,
      actual_cost_microusd: null,
    },
  });
  const retired = renderWithLocale(
    module,
    React.createElement(module.exports.BudgetAccounts, { budgets: [retiredRow], compact: true }),
  );
  assert.match(retired, /原固定额度已停用/);
  assert.match(retired, /待核账单预估.*\$2\.00/);
  assert.match(retired, /实际费用尚未确认/);
  assert.doesNotMatch(retired, /额度不足|<progress|剩余额度/);
  const state = {
      account: 'superdan',
      snapshot: { operator: { account: 'superdan', admin: true } },
      adminBudgets: page(
        [
          row({ owner_id: null, id: 'demand-gpu-remaining-20261004' }),
          row({ owner_id: null, id: 'demand-job-total-20261004' }),
        ],
        { admin: true },
      ),
      adminPrices: { profiles: [], audits: [] },
      budgetAudits: { events: [] },
    },
    controller = { readBudgetAdmin: async () => {} };
  const admin = renderWithLocale(
    module,
    React.createElement(module.exports.BudgetWorkspace, {
      account: 'superdan',
      controller,
      state,
      busy: false,
    }),
  );
  assert.match(admin, /预算与价格管理/);
  assert.match(admin, /共享 GPU 预算/);
  assert.match(admin, /共享生成预算/);
  assert.match(admin, /全部账户已读取/);
  assert.match(admin, /settings\?tab=admin/);
  const ordinary = renderWithLocale(
    module,
    React.createElement(module.exports.BudgetWorkspace, {
      account: 'supervan',
      controller,
      state: {
        ...state,
        account: 'supervan',
        snapshot: { operator: { account: 'supervan', admin: false } },
      },
      busy: false,
    }),
  );
  assert.doesNotMatch(ordinary, /预算与价格管理|共享 GPU 预算|共享生成预算|settings\?tab=admin/);
  const english = renderWithLocale(
    module,
    React.createElement(module.exports.BudgetAccounts, { budgets: [retiredRow], compact: true }),
    'en',
  );
  const originalCosts = row({
    cost_breakdown: costBreakdown(),
    spent_microusd: 84715946,
    reserved_microusd: 0,
  });
  const pendingCosts = renderWithLocale(
    module,
    React.createElement(module.exports.BudgetAccounts, { budgets: [originalCosts], compact: true }),
  );
  assert.match(pendingCosts, /已记录生成成本分摊.*\$0\.00/);
  assert.match(pendingCosts, /181 条生成预估等待成本分摊.*\$162\.40/);
  assert.match(pendingCosts, /已确认 GPU 租金账单.*\$84\.72/);
  assert.match(pendingCosts, /实际费用未知/);
  assert.match(pendingCosts, /不代表免费生成/);
  assert.match(pendingCosts, /账户层级不能相加/);
  const pendingEnglish = renderWithLocale(
    module,
    React.createElement(module.exports.BudgetAccounts, { budgets: [originalCosts], compact: true }),
    'en',
  );
  assert.match(pendingEnglish, /181 generation estimates await cost allocation/);
  assert.match(pendingEnglish, /Actual charges are unknown/);
  assert.match(pendingEnglish, /Confirmed GPU rental invoices/);
  assert.doesNotMatch(pendingEnglish, /operatorPanel\./);
  assert.match(english, /original fixed cap is retired/);
  assert.match(english, /Pending invoice estimate/);
  assert.doesNotMatch(english, /原固定额度|operatorPanel\./);
});

test('definite monetary start rejection clears only its journal and allows explicit budget restore without a paid retry', async () => {
  for (const code of ['budget_exceeded', 'budget_not_configured']) {
    const calls = [],
      accepted = [],
      storage = memory();
    let restored = false;
    const api = createP4OperatorClient({
        fetcher: async (path, options) => {
          calls.push({ path, options });
          let value,
            status = 200;
          if (path.endsWith('/workspace'))
            value = {
              capacity_backend: 'dstack-v1',
              operator: { account: 'superdan', admin: true },
              nodes: [],
              groups: [],
              queue: [],
            };
          else if (path.endsWith('/catalog')) value = { profiles: [] };
          else if (path === '/v1/operator/dstack/starts') {
            if (!restored) {
              status = 429;
              value = { code, detail: '额度不足，未新增付费执行。' };
            } else {
              value = { node_id: 'explicit-new-node', state: 'accepted' };
              accepted.push(JSON.parse(options.body).preview_id);
            }
          } else if (path === '/v1/operator/admin/budgets/owner-budget') {
            assert.deepEqual(JSON.parse(options.body), {
              expected_snapshot: row().snapshot,
              limit_microusd: 106000000,
            });
            restored = true;
            value = {
              budget: row({ limit_microusd: 106000000, snapshot: 'b'.repeat(64) }),
              changed: true,
              audit_event_id: 'restore-audit',
            };
          } else throw Error('unexpected request');
          return new Response(JSON.stringify(value), {
            status,
            headers: { 'Content-Type': 'application/json', 'X-Authenticated-Account': 'superdan' },
          });
        },
      }),
      controller = createP4OperatorController({ client: api, storage });
    await controller.setAccount('superdan');
    await assert.rejects(controller.start({ preview_id: 'rejected-preview' }), /额度不足/);
    assert.equal(controller.getState().pending, null);
    assert.equal(storage.getItem('sixnine:operator:dstack:pending:superdan'), null);
    assert.deepEqual(accepted, []);
    assert.equal(calls.filter((call) => call.path.endsWith('/starts')).length, 1);
    await controller.updateBudget(row(), 106000000);
    assert.deepEqual(accepted, []);
    assert.equal(
      calls.filter((call) => call.path.endsWith('/starts')).length,
      1,
      'restoring the original budget does not repeat start',
    );
    await controller.start({ preview_id: 'explicit-fresh-preview' });
    assert.deepEqual(accepted, ['explicit-fresh-preview']);
    assert.equal(calls.filter((call) => call.path.endsWith('/starts')).length, 2);
  }
});

test('generic rate limits, unconfirmed capacity, timeouts and service failures retain the original start without replay', async () => {
  for (const failure of [
    { status: 503, detail: { code: 'capacity_unavailable' } },
    { status: 429, detail: { code: 'rate_limited' } },
    { status: 429, detail: { detail: 'budget_exceeded' } },
    { status: 408, detail: { code: 'budget_exceeded' } },
    { status: 503, detail: { code: 'budget_exceeded' } },
  ]) {
    let calls = 0;
    const storage = memory(),
      controller = createP4OperatorController({
        client: fake({
          start: async () => {
            calls++;
            throw Object.assign(Error('unconfirmed'), failure);
          },
        }),
        storage,
      });
    await controller.setAccount('superdan');
    await assert.rejects(controller.start({ preview_id: 'original-preview' }));
    const original = controller.getState().pending;
    assert.equal(original.body.preview_id, 'original-preview');
    assert.equal(
      JSON.parse(storage.getItem('sixnine:operator:dstack:pending:superdan')).key,
      original.key,
    );
    await assert.rejects(controller.start({ preview_id: 'other-preview' }), /原操作结果/);
    assert.equal(calls, 1);
  }
  const controller = createP4OperatorController({
    client: fake({
      updateBudget: async () => {
        throw Object.assign(Error('uncertain other operation'), {
          status: 429,
          detail: { code: 'budget_exceeded' },
        });
      },
    }),
    storage: memory(),
  });
  await controller.setAccount('superdan');
  await assert.rejects(controller.updateBudget(row(), 106000000));
  assert.equal(
    controller.getState().pending.method,
    'updateBudget',
    'the definite start-only proof does not broaden other command semantics',
  );
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
