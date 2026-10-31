import { test } from 'node:test';
import assert from 'node:assert/strict';
import { takeName, takeQuality } from './enhance-take.js';

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

test('a take shows its playback size: enhanced by tier, others by the short side', () => {
  assert.equal(takeQuality({ enhanced: true, effectiveRequest: { tier: '1080p' } }), '1080P');
  assert.equal(takeQuality({ width: 832, height: 480 }), '480P');
  assert.equal(takeQuality({ width: 768, height: 1344 }), '768P');
  assert.equal(takeQuality({ width: 2560, height: 1440 }), '2K');
  assert.equal(takeQuality({ width: 1920 }), '');
  assert.equal(takeQuality(undefined), '');
});
