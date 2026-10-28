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
