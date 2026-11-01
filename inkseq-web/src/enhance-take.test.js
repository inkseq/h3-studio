import { test } from 'node:test';
import assert from 'node:assert/strict';
import { batchEnhanceTakes, takeName, takeQuality } from './enhance-take.js';

test('takes drop the service placeholder name', () => {
  assert.equal(takeName('Generated result', 'v3'), 'v3');
  assert.equal(takeName('', 'v2'), 'v2');
  assert.equal(takeName('窗台伸懒腰', 'v1'), '窗台伸懒腰');
});

test('a take shows its playback size: enhanced by tier, others by the short side', () => {
  assert.equal(takeQuality({ enhanced: true, effectiveRequest: { tier: '1080p' } }), '1080P');
  assert.equal(takeQuality({ width: 832, height: 480 }), '480P');
  assert.equal(takeQuality({ width: 768, height: 1344 }), '768P');
  assert.equal(takeQuality({ width: 2560, height: 1440 }), '2K');
  assert.equal(takeQuality({ width: 1920 }), '');
  assert.equal(takeQuality(undefined), '');
});

test('a batch takes the current take of selected shots that can be enhanced', () => {
  const on = { enhance: { enabled: true } };
  const done = (id, extra = {}) => ({ id, status: 'done', asset: { entity: { id } }, ...extra });
  const shots = [
    { id: 'a', take: done('a1') },
    { id: 'b', take: done('b1', { enhanced: true }) },
    { id: 'c', take: { id: 'c1', status: 'running' } },
    { id: 'd', take: null },
    { id: 'e', take: done('e1') },
  ];
  const chosen = (shot) => shot.take;
  const ids = (takes) => takes.map((take) => take.id);
  assert.deepEqual(ids(batchEnhanceTakes(shots, ['a', 'b', 'c', 'd'], on, chosen)), ['a1']);
  assert.deepEqual(ids(batchEnhanceTakes(shots, ['a', 'e'], on, chosen)), ['a1', 'e1']);
  assert.deepEqual(batchEnhanceTakes(shots, ['a'], {}, chosen), []);
});
