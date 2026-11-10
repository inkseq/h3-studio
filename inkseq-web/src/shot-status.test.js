import test from 'node:test';
import assert from 'node:assert/strict';
import {
  chosenTake,
  enhanceAllPlan,
  enhancedCopy,
  howManyFit,
  liveTake,
  shotState,
} from './shot-status.js';

// A take as storyboard-domain builds it; `trashed` drops its library entity, as the trash does.
const take = (id, status = 'done', extra = {}) => ({
  id,
  status,
  asset: extra.trashed ? null : { entity: { id, data: { fileId: 'f-' + id } } },
  ...extra,
});
const copy = (id, source, tier, status = 'done') =>
  take(id, status, { enhanced: true, sourceEntityId: source, effectiveRequest: { tier } });
const shot = (versions, chosen = '') => ({ id: 's-' + versions.length, versions, chosen });

test('every shot state, from its takes (#892)', () => {
  assert.equal(shotState(shot([])).id, 'empty');
  assert.equal(shotState(shot([take('a', 'queued')])).id, 'queued');
  assert.equal(shotState(shot([take('a', 'running')])).id, 'generating');
  assert.equal(shotState(shot([take('a', 'failed')])).id, 'failed');
  assert.equal(shotState(shot([take('a')])).id, 'pick');
  assert.equal(shotState(shot([take('a')], 'a')).id, 'chosen');
  // A newer finished take than the chosen one waits to be looked at.
  assert.equal(shotState(shot([take('a'), take('b')], 'a')).id, 'pick');
  assert.equal(shotState(shot([take('a'), take('b')], 'b')).id, 'chosen');
  // The chosen take is a sharper copy.
  assert.equal(shotState(shot([take('a'), copy('h', 'a', '2k')], 'h')).id, 'hd');
});

test('trash: a trashed take is never live, counted or chosen; nothing else is chosen', () => {
  // All takes trashed: nothing to show.
  assert.equal(shotState(shot([take('a', 'done', { trashed: true })])).id, 'empty');
  // The chosen take trashed: the shot waits for a pick, it does not take the other one.
  const s = shot([take('a', 'done', { trashed: true }), take('b')], 'a');
  assert.equal(chosenTake(s), null);
  assert.equal(shotState(s).id, 'pick');
  assert.equal(liveTake(s.versions[0]), false);
  // Restored (back in the story) but not chosen again: still a pick.
  assert.equal(shotState(shot([take('a'), take('b')], '')).id, 'pick');
});

test('enhance all: targets and why the others are left out (#893)', () => {
  const shots = [
    { id: 'empty', versions: [], chosen: '' },
    { id: 'busy', versions: [take('a'), take('b', 'running')], chosen: 'a' },
    { id: 'pick', versions: [take('c')], chosen: '' },
    { id: 'hd', versions: [take('d'), copy('dh', 'd', '1080p')], chosen: 'dh' },
    { id: 'copy', versions: [take('e'), copy('eh', 'e', '1080p', 'queued')], chosen: 'e' },
    { id: 'go', versions: [take('f'), take('g', 'failed')], chosen: 'f' },
    { id: 'go2k', versions: [take('k'), copy('kh', 'k', '2k')], chosen: 'k' },
  ];
  const plan = enhanceAllPlan(shots, '1080p', true);
  assert.deepEqual(
    plan.targets.map((t) => [t.shot.id, t.take.id]),
    [
      ['go', 'f'],
      ['go2k', 'k'],
    ],
  );
  assert.deepEqual(
    plan.skipped.map((s) => [s.shot.id, s.reason]),
    [
      ['empty', 'empty'],
      ['busy', 'busy'],
      ['pick', 'pick'],
      ['hd', 'hd'],
      ['copy', 'copy'],
    ],
  );
  // At 2K the shot with a finished 2K copy is left out; the 1080P copy in flight is not 2K.
  const at2k = enhanceAllPlan(shots, '2k', true);
  assert.deepEqual(
    at2k.targets.map((t) => t.shot.id),
    ['copy', 'go'],
  );
  assert.equal(enhancedCopy(shots[6], '2k').id, 'kh');
  // A running copy counts as a copy, so a second press never sends it again.
  assert.equal(enhancedCopy(shots[4], '1080p').id, 'eh');
  assert.equal(enhanceAllPlan(shots, '1080p', false).targets.length, 0);
});

test('how many fit in the balance, in order; unknown prices promise nothing', () => {
  assert.equal(howManyFit([100, 100, 150], 250), 2);
  assert.equal(howManyFit([300], 250), 0);
  assert.equal(howManyFit([100, null], 250), null);
  assert.equal(howManyFit([100], undefined), null);
});
