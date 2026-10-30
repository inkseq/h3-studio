import test from 'node:test';
import assert from 'node:assert/strict';
import { termsRequired } from './terms-gate.js';

test('only the reviewed-terms refusal opens the acceptance dialog', () => {
  assert.equal(termsRequired(409, { code: 'terms_acceptance_required', detail: 'x' }), true);
  assert.equal(termsRequired(409, { code: 'spending_confirmation_required' }), false);
  assert.equal(termsRequired(403, { code: 'terms_acceptance_required' }), false);
  assert.equal(termsRequired(409, null), false);
});
