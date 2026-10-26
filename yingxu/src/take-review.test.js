import test from 'node:test';
import assert from 'node:assert/strict';
import {
  MIN_RANGE,
  cleanMark,
  marksBody,
  newMark,
  nextVerdict,
  rangeFrom,
} from './take-review-model.js';

test('the whole-take verdict toggles and clears on a second click', () => {
  assert.equal(nextVerdict(null, 'keep'), 'keep');
  assert.equal(nextVerdict('keep', 'redo'), 'redo');
  assert.equal(nextVerdict('redo', 'redo'), null);
});

test('a dragged range is ordered, clamped to the take and never shorter than the minimum', () => {
  assert.deepEqual(rangeFrom(3, 1, 5), { start_s: 1, end_s: 3 });
  assert.deepEqual(rangeFrom(-1, 9, 5), { start_s: 0, end_s: 5 });
  const tiny = rangeFrom(4.95, 5, 5);
  assert.ok(tiny.end_s - tiny.start_s >= MIN_RANGE - 1e-9);
  assert.equal(tiny.end_s, 5);
  assert.equal(rangeFrom(1, 2, NaN), null);
});

test('new marks get unique ids and default to a change with no tags', () => {
  const mark = newMark({ start_s: 1, end_s: 2 }, new Set(['mark-1']));
  assert.equal(mark.id, 'mark-2');
  assert.equal(mark.verdict, 'change');
  assert.deepEqual(mark.tags, []);
});

test('the saved body has only server fields, unique known tags and a bounded note', () => {
  const mark = cleanMark(
    {
      id: 'm',
      start_s: 2,
      end_s: 1,
      tags: ['camera', 'camera', 'nope'],
      verdict: 'odd',
      note: 'x'.repeat(600),
      extra: true,
    },
    5,
  );
  assert.deepEqual(Object.keys(mark).sort(), ['end_s', 'id', 'note', 'start_s', 'tags', 'verdict']);
  assert.deepEqual(mark.tags, ['camera']);
  assert.equal(mark.verdict, 'change');
  assert.equal(mark.note.length, 500);
  assert.throws(() => marksBody([mark], NaN), /review.durationUnknown/);
  assert.deepEqual(
    marksBody([{ ...mark, id: 'b', start_s: 3, end_s: 4 }, mark], 5).map((m) => m.id),
    ['m', 'b'],
  );
});
