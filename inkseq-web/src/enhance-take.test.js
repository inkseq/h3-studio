import { test } from 'node:test';
import assert from 'node:assert/strict';
import { takeName } from './enhance-take.js';

test('takes drop the service placeholder name and enhanced copies say their size', () => {
  const sharp = { enhanced: true, effectiveRequest: { tier: '2k' } };
  assert.equal(takeName('Generated result', { n: 3 }, 'v3'), 'v3');
  assert.equal(takeName('', sharp, 'v2'), 'v2 · 2K');
  assert.equal(
    takeName('Generated result', { ...sharp, effectiveRequest: { tier: '1080p' } }, 'v4'),
    'v4 · 1080P',
  );
  assert.equal(takeName('窗台伸懒腰', sharp, 'v2'), '窗台伸懒腰 · 2K');
  // An original take, or an enhanced one whose tier is hidden, keeps its plain name.
  assert.equal(takeName('窗台伸懒腰', { n: 1 }, 'v1'), '窗台伸懒腰');
  assert.equal(takeName('', { enhanced: true }, 'v5'), 'v5');
});
