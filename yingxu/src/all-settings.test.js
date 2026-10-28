import test from 'node:test';
import assert from 'node:assert/strict';
import { NOT_STORED, capacityRows, priceRows } from './all-settings-model.js';

test('capacity rows show the value in force and its latest change', () => {
  const rows = capacityRows({
    value: { fleet_hourly_limit_microusd: 20_000_000, max_machines: 10 },
    capacity_policy: {
      defaults: { idle_shutdown_seconds: 600 },
      latest_changes: { max_machines: { actor_id: 'browser:owner', observed_at: 1, reason: 'x' } },
    },
  });
  const by = Object.fromEntries(rows.map((row) => [row.id, row]));
  assert.equal(by.fleet_hourly_limit_microusd.value, '20');
  assert.equal(by.max_machines.value, '10');
  assert.equal(by.max_machines.change.reason, 'x');
  assert.equal(by.idle_shutdown_seconds.value, '10');
  assert.deepEqual(capacityRows(null), []);
});

test('price rows list tier rates, welcome credits and packs', () => {
  const rows = priceRows(
    { rates: { standard: 4, refine: 12 }, welcome_credits: 200 },
    { packs: [{ id: 'starter', credits: 500, amount_cents: 500 }] },
  );
  assert.deepEqual(
    rows.map((row) => [row.id, row.value]),
    [
      ['rate.standard', '4'],
      ['rate.refine', '12'],
      ['welcome', '200'],
      ['pack.starter', '500 / 5.00'],
    ],
  );
});

test('every not-yet-stored number says where it lives today', () => {
  for (const row of NOT_STORED) assert.ok(['code', 'hostFile', 'planned'].includes(row.where));
});

test('inventory rows group, format and link to the editing page', async () => {
  const { inventoryGroups, displayValue, editPage, pendingFields, settingKey } =
    await import('./all-settings-model.js');
  const inventory = {
    items: [
      {
        id: 'capacity.fleet_hourly_limit_microusd',
        group: 'capacity',
        value: 20_000_000,
        unit: 'micro_usd',
      },
      { id: 'capacity.idle_shutdown_seconds', group: 'capacity', value: 600, unit: 'seconds' },
      { id: 'switches.modal', group: 'switches', value: false, unit: 'boolean' },
    ],
    unavailable: ['pending:busy_mode', 'other'],
  };
  assert.deepEqual(
    inventoryGroups(inventory).map(([group, rows]) => [group, rows.length]),
    [
      ['capacity', 2],
      ['switches', 1],
    ],
  );
  assert.equal(displayValue(inventory.items[0]), '$20');
  assert.equal(displayValue(inventory.items[1]), '10 min');
  assert.equal(displayValue(inventory.items[2]), 'off');
  assert.equal(settingKey(inventory.items[0]), 'fleet_hourly_limit_microusd');
  assert.equal(editPage('/v1/operator/admin/spending-settings'), '/admin?section=limits');
  assert.deepEqual([...pendingFields(inventory)], ['busy_mode']);
});

test('inventory hides covered rows and picks label keys', async () => {
  const { stillNotStored, labelKeys, humanize, rowScope, NOT_STORED } =
    await import('./all-settings-model.js');
  const inventory = { items: [{ id: 'runtime.startup_timeout_seconds', group: 'runtime' }] };
  const left = stillNotStored(inventory).map((row) => row.id);
  assert.equal(left.includes('startupTimeout'), false);
  assert.equal(left.length, NOT_STORED.length - 1);
  assert.equal(stillNotStored(null), NOT_STORED);
  assert.deepEqual(labelKeys({ id: 'start_rules.p.job_seconds', group: 'start_rules' }), [
    'rules.field.job_seconds',
  ]);
  assert.deepEqual(labelKeys({ id: 'packs.p1.credits', group: 'packs' }), [
    'allSettings.key.packs.credits',
    'allSettings.key.credits',
  ]);
  assert.equal(humanize('max_containers'), 'Max containers');
  assert.equal(rowScope({ profile_id: 'v7', mode: 'ref' }), 'v7 · ref');
});
