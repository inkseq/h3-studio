import test from 'node:test';
import assert from 'node:assert/strict';
import { selectShot, emptySelection } from './storyboard-selection.js';
import { storyboardDockLayout } from './storyboard-ui-model.js';
test('automatic dock fills sparse chapters, yields to a long wall and preserves a manual preference', () => {
  assert.equal(storyboardDockLayout({ height: 760, content: 240 }).size, 441);
  assert.equal(storyboardDockLayout({ height: 760, content: 1100 }).size, 190);
  assert.equal(storyboardDockLayout({ height: 760, content: 240, manual: 280 }).size, 280);
  assert.equal(storyboardDockLayout({ height: 460, content: 1100, manual: 540 }).size, 320);
  assert.equal(storyboardDockLayout({ height: 900, content: 1100, manual: 540 }).size, 540);
  assert.equal(storyboardDockLayout({ height: 760, content: 240, manual: 110 }).size, 110);
});
test('range uses visible order, preserves toggle and drops removed identities', () => {
  const ids = ['a', 'b', 'c', 'd'];
  let s = selectShot(emptySelection(), ids, 'b', { toggle: true });
  s = selectShot(s, ids, 'd', { range: true });
  assert.deepEqual(s.ids, ['b', 'c', 'd']);
  s = selectShot(s, ids, 'c', { toggle: true });
  assert.deepEqual(s.ids, ['b', 'd']);
  s = selectShot(s, ['d', 'a'], 'a', { range: true });
  assert.deepEqual(s.ids, ['a']);
  assert.deepEqual(selectShot(s, ['d', 'a'], 'foreign'), s);
});
