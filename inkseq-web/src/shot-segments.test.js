import test from 'node:test';
import assert from 'node:assert/strict';
import {
  clock,
  move,
  segmentsAction,
  segmentsTotal,
  shotSegments,
  snapRange,
  sourceTime,
} from './shot-segments.js';
import { segments } from './storyboard-ui-model.js';

test('kept parts keep the person order and repeats; malformed ranges are dropped', () => {
  const value = [
    { start: 5, end: 6 },
    { start: 1, end: 3 },
    { start: 5, end: 6 },
    { start: 4, end: 4 },
    { start: -1, end: 2 },
    { start: '1', end: 2 },
  ];
  assert.deepEqual(shotSegments(value), [
    { start: 5, end: 6 },
    { start: 1, end: 3 },
    { start: 5, end: 6 },
  ]);
  assert.deepEqual(shotSegments(undefined), []);
  assert.equal(segmentsTotal(shotSegments(value)), 4);
});

test('snapping matches the server grid: start rounds up, end rounds down', () => {
  assert.deepEqual(snapRange(1.01, 2.99), { start: 25 / 24, end: 71 / 24 });
  assert.deepEqual(snapRange(1, 3), { start: 1, end: 3 });
  assert.equal(snapRange(1.01, 1.02), null);
});

test('shot playback maps onto the parts in order', () => {
  const list = [
    { start: 9, end: 10 },
    { start: 1, end: 3 },
  ];
  assert.equal(sourceTime(list, 0), 9);
  assert.equal(sourceTime(list, 0.5), 9.5);
  assert.equal(sourceTime(list, 1), 1);
  assert.equal(sourceTime(list, 2.5), 2.5);
  assert.equal(sourceTime(list, 9), 3);
  assert.equal(sourceTime([], 4), 4);
});

test('list edits replace the whole ordered list; [] clears', () => {
  const list = [
    { start: 1, end: 3 },
    { start: 5, end: 6 },
  ];
  assert.deepEqual(segmentsAction('shot-1', move(list, 1, 0)), {
    op: 'shot.segments',
    shot_id: 'shot-1',
    segments: [
      { start: 5, end: 6 },
      { start: 1, end: 3 },
    ],
  });
  assert.deepEqual(segmentsAction('shot-1', []).segments, []);
  assert.equal(clock(65.5), '1:05.5');
  assert.equal(clock(3), '0:03');
});

test('the story timeline counts a shot by its kept parts', () => {
  const version = { id: 'v1', status: 'done', duration: 10 };
  const shot = (segs) => ({ id: 's', versions: [version], chosen: 'v1', segments: segs });
  assert.equal(segments([shot([])])[0].dur, 10);
  assert.equal(
    segments([
      shot([
        { start: 1, end: 3 },
        { start: 9, end: 10 },
      ]),
    ])[0].dur,
    3,
  );
});
