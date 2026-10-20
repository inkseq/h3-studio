import test from 'node:test';
import assert from 'node:assert/strict';
import { validDemandStart } from './demand-start-model.js';

const sample = {
  enabled: false,
  revision: null,
  hard_max_machines: 2,
  running_machines: 0,
  starting_machines: 0,
  uncertain_machines: 0,
  queued_generation_jobs: 0,
  queued_by_mode: { fl: 0, ref: 0 },
  blocked_reason: 'disabled',
};

test('accepts the exact CR43 admin demand_start contract', () => {
  assert.equal(validDemandStart(sample), true);
  assert.equal(
    validDemandStart({ ...sample, enabled: true, revision: 'a'.repeat(64), blocked_reason: null }),
    true,
  );
});

test('refuses unknown reasons, bad counts and malformed revisions', () => {
  assert.equal(validDemandStart({ ...sample, blocked_reason: 'gpu_vendor_x' }), false);
  assert.equal(validDemandStart({ ...sample, running_machines: -1 }), false);
  assert.equal(validDemandStart({ ...sample, queued_by_mode: { fl: 1 } }), false);
  assert.equal(validDemandStart({ ...sample, revision: 'short' }), false);
  assert.equal(validDemandStart(null), false);
});
