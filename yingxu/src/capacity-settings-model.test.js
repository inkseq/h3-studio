import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  baseline,
  buildChanges,
  confirmBody,
  draftFrom,
  fleetShortfall,
  offDefault,
  pendingControl,
} from './capacity-settings-model.js';

// Shape of the staging GET on 2026-10-22 (policy not adopted, legacy values in force).
const settings = JSON.parse(
  readFileSync(new URL('./capacity-settings.fixture.json', import.meta.url), 'utf8'),
);

test('unchanged draft sends nothing, even with legacy values outside the new bounds', () => {
  const draft = draftFrom(baseline(settings));
  assert.equal(draft.tenant_daily_limit_microusd, '100');
  assert.equal(draft.tenant_daily_warning_fraction, '80');
  assert.equal(draft.idle_shutdown_seconds, '10');
  assert.equal(draft['mode_hourly_ceiling_microusd.ref'], '1.5');
  assert.deepEqual(buildChanges(settings, draft), { changes: {}, errors: {}, changed: [] });
});

test('only changed fields are sent; object fields go whole', () => {
  const draft = {
    ...draftFrom(baseline(settings)),
    fleet_hourly_limit_microusd: '3',
    'mode_hourly_ceiling_microusd.fl': '0.9',
    idle_active_seconds: '20',
  };
  const { changes, errors } = buildChanges(settings, draft);
  assert.deepEqual(errors, {});
  assert.deepEqual(changes, {
    fleet_hourly_limit_microusd: 3000000,
    mode_hourly_ceiling_microusd: { ref: 1500000, fl: 900000, other: 850000 },
    idle_active_seconds: 1200,
  });
});

test('values are checked against the server bounds', () => {
  const draft = {
    ...draftFrom(baseline(settings)),
    max_machines: '9',
    tenant_daily_warning_fraction: '99',
    test_task_limit_microusd: 'abc',
    'batch_guaranteed_share_slots.numerator': '4',
  };
  const { errors } = buildChanges(settings, draft);
  assert.deepEqual(Object.keys(errors).sort(), [
    'batch_guaranteed_share_slots.numerator',
    'max_machines',
    'tenant_daily_warning_fraction',
    'test_task_limit_microusd',
  ]);
  assert.deepEqual(errors.max_machines, { code: 'range', minimum: '1', maximum: '8' });
  assert.equal(errors.test_task_limit_microusd.code, 'number');
});

test('the legacy fleet cap is flagged against the recommendation', () => {
  assert.deepEqual(offDefault(settings).sort(), [
    'fleet_hourly_limit_microusd',
    'max_hourly_price_microusd',
  ]);
  assert.equal(pendingControl(settings, 'idle_quiet_seconds'), true);
  assert.equal(pendingControl(settings, 'max_machines'), false);
});

test('fleet arithmetic says how many machines really fit', () => {
  const consequence = {
    code: 'fleet_limits_combination',
    machines: 3,
    required_hourly_microusd: 2550000,
    fleet_hourly_limit_microusd: 850000,
  };
  assert.deepEqual(fleetShortfall(consequence), { machines: 3, allowed: 1 });
  assert.equal(fleetShortfall({ ...consequence, fleet_hourly_limit_microusd: 3000000 }), null);
});

test('confirmation repeats exactly what preview returned plus the reason', () => {
  const preview = {
    expected_version: 1,
    confirmation_digest: 'abc',
    profile_snapshots: { p: { snapshot: 's' } },
  };
  assert.deepEqual(confirmBody(preview, { max_machines: 2 }, '  owner decision '), {
    expected_version: 1,
    changes: { max_machines: 2 },
    reason: 'owner decision',
    confirmation_digest: 'abc',
    profile_snapshots: { p: { snapshot: 's' } },
  });
});
