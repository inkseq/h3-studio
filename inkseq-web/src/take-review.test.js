import test from 'node:test';
import assert from 'node:assert/strict';
import {
  MIN_RANGE,
  choiceOf,
  cleanMark,
  cutRanges,
  keptAfterCuts,
  withChoice,
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
  assert.equal(choiceOf(mark), 'reshoot');
  assert.deepEqual(mark.tags, []);
});

test('a stretch is kept, cut, fixed or reshot; older change marks still read', () => {
  const base = { id: 'm', start_s: 1, end_s: 2, tags: [], verdict: 'change', note: '' };
  assert.equal(choiceOf(base), 'change');
  for (const choice of ['keep', 'cut', 'fix', 'reshoot']) {
    const mark = withChoice(base, choice);
    assert.equal(choiceOf(mark), choice);
    const body = cleanMark(mark, 5);
    assert.equal(body.verdict, choice === 'keep' ? 'keep' : 'change');
    assert.equal(body.disposition, choice === 'keep' ? undefined : choice);
  }
  assert.equal(
    cleanMark({ ...base, verdict: 'keep', disposition: 'cut' }, 5).disposition,
    undefined,
  );
  assert.deepEqual(
    cutRanges([
      withChoice({ ...base, id: 'b', start_s: 3, end_s: 4 }, 'cut'),
      withChoice(base, 'cut'),
      withChoice({ ...base, id: 'c' }, 'fix'),
    ]),
    [
      { start: 1, end: 2, id: 'm' },
      { start: 3, end: 4, id: 'b' },
    ],
  );
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

test('cut marks leave the rest of the take as kept parts', () => {
  const cut = (start_s, end_s) => ({
    id: 'c' + start_s,
    start_s,
    end_s,
    verdict: 'change',
    disposition: 'cut',
  });
  assert.deepEqual(
    keptAfterCuts(5, [cut(1, 2), cut(1.5, 3), { ...cut(4, 4.5), disposition: 'fix' }]),
    [
      { start: 0, end: 1 },
      { start: 3, end: 5 },
    ],
  );
  assert.deepEqual(keptAfterCuts(5, [cut(0, 0.1), cut(4.9, 5)]), [{ start: 0.1, end: 4.9 }]);
  assert.deepEqual(keptAfterCuts(NaN, [cut(1, 2)]), []);
});
