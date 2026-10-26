import test from 'node:test';
import assert from 'node:assert/strict';
import { profileLabel, profileTier } from './model-visibility.js';

const profile = { id: 'p1', label: 'Vendor Model Pruned INT8', model_id: 'vendor-x', tier: 'fast' };

test('regular users see the quality tier, admins see the model', () => {
  assert.equal(profileLabel(profile, 'en', false), 'Fast draft');
  assert.equal(profileLabel(profile, 'zh', false), '快速草稿');
  assert.equal(profileLabel(profile, 'en', true), 'Vendor Model Pruned INT8');
  assert.equal(profileTier({ tier: 'unknown' }), 'standard');
  assert.equal(profileLabel({ id: 'p2', label: 'Secret' }, 'en', false), 'Standard');
});
