import test from 'node:test';
import assert from 'node:assert/strict';
import { guideField, guideHref, highlightGuideField } from './guide.js';

test('guide links address a story, a shot and one editor field', () => {
  assert.equal(guideHref({ projectId: 'p1' }), '/?view=workspace&project=p1');
  assert.equal(
    guideHref({ projectId: 'p1', shotId: 's-2', field: 'action' }),
    '/?view=workspace&project=p1&entity=s-2&field=action',
  );
  assert.equal(guideHref({ projectId: 'p1', field: 'action' }), null, 'a field needs a shot');
  assert.equal(guideHref({ projectId: 'p1', shotId: 's', field: 'steps' }), null);
  assert.equal(guideHref({ projectId: 'a/b' }), null);
  assert.equal(guideHref({}), null);
});

test('only known fields are read back from a link', () => {
  assert.equal(guideField('?view=workspace&entity=s&field=cast'), 'cast');
  assert.equal(guideField('?field=onclick'), null);
  assert.equal(guideField(''), null);
});

test('highlighting focuses the field input and pulses once', () => {
  const classes = new Set(),
    timers = [];
  let focused = null,
    scrolled = null;
  const input = { focus: () => (focused = 'textarea') };
  const node = {
    matches: () => false,
    querySelector: () => input,
    scrollIntoView: (options) => (scrolled = options.behavior),
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) },
    offsetWidth: 1,
  };
  const root = {
    querySelector: (selector) => (selector === '[data-guide-field="action"]' ? node : null),
    defaultView: { setTimeout: (fn) => timers.push(fn) },
  };
  assert.equal(highlightGuideField(root, 'action', { reducedMotion: true }), node);
  assert.equal(focused, 'textarea');
  assert.equal(scrolled, 'auto');
  assert.ok(classes.has('guide-highlight'));
  timers.forEach((fn) => fn());
  assert.ok(!classes.has('guide-highlight'));
  assert.equal(highlightGuideField(root, 'refs'), null, 'not rendered yet');
  assert.equal(highlightGuideField(root, 'x'), null);
});
