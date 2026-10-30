import test from 'node:test';
import assert from 'node:assert/strict';
import { validateRange, chapterTiming, trackPlacement, trackAtTime } from './media-timing.js';
test('media ranges reject missing, negative, reversed, oversized and unknown-duration values', () => {
  assert.ok(validateRange('', 2, 4));
  assert.ok(validateRange(-1, 2, 4));
  assert.ok(validateRange(3, 3, 4));
  assert.ok(validateRange(3, 2, 4));
  assert.ok(validateRange(0, 5, 4));
  assert.ok(validateRange(0, 2, NaN));
  assert.ok(validateRange('bad', 2, 4));
  assert.equal(validateRange(0, 4, 4), '');
  assert.equal(validateRange(1.25, 2.75, 4), '');
});
test('chapter-relative tracks follow shot reordering; source trim, mute and endpoints are respected', () => {
  const a = { id: 'a', title: 'A', data: { seconds: 4 } },
    b = { id: 'b', title: 'B', data: { seconds: 6 } },
    track = { shotId: 'b', offset: 1, start: 2, end: 5 };
  const timeline = chapterTiming([a, b]);
  assert.deepEqual(trackPlacement(track, timeline), { start: 5, end: 8 });
  assert.equal(trackAtTime(track, timeline, 4.99), null);
  assert.equal(trackAtTime(track, timeline, 5), 2);
  assert.equal(trackAtTime(track, timeline, 7.5), 4.5);
  assert.equal(trackAtTime(track, timeline, 8), null);
  assert.equal(trackAtTime({ ...track, muted: true }, timeline, 6), null);
  assert.deepEqual(trackPlacement(track, chapterTiming([b, a])), { start: 1, end: 4 });
  assert.equal(trackPlacement(track, chapterTiming([a])), null);
  assert.deepEqual(trackPlacement({ ...track, shotId: '' }, timeline), { start: 1, end: 4 });
  assert.equal(trackPlacement({ ...track, offset: -1 }, timeline), null);
  assert.equal(trackPlacement({ ...track, start: -2 }, timeline), null);
  assert.equal(trackPlacement({ ...track, end: Infinity }, timeline), null);
});
