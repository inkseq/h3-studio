import test from 'node:test';
import assert from 'node:assert/strict';
import {
  askFor,
  canRewrite,
  cornerState,
  itemHref,
  openItems,
  otherHref,
} from './inky/inky-model.js';

const item = (id, kind = 'vague_action', shot_id = 's1') => ({ id, kind, shot_id });

test('at most three valid, undismissed items for shots that still exist', () => {
  const items = [
    item('a'),
    item('b', 'needs_keyframe'),
    { id: 'bad', kind: 'rewrite_everything', shot_id: 's1' },
    item('c', 'upload_card', 'gone'),
    item('d'),
    item('e'),
  ];
  const open = openItems(items, { ignored: new Set(['d']), shotIds: new Set(['s1']) });
  assert.deepEqual(
    open.map((i) => i.id),
    ['a', 'b', 'e'],
  );
  assert.deepEqual(openItems(null), []);
});

test('each kind points at its editor field', () => {
  assert.equal(itemHref(item('a'), 'p1'), '/?view=workspace&project=p1&entity=s1&field=action');
  assert.equal(
    itemHref(item('b', 'needs_keyframe'), 'p1'),
    '/?view=workspace&project=p1&entity=s1&field=frames',
  );
  assert.equal(itemHref(item('c', 'upload_card'), 'p1').endsWith('field=cast'), true);
  for (const kind of ['emotion_not_shown', 'multiple_camera_moves', 'action_too_long'])
    assert.equal(itemHref(item('x', kind), 'p1').endsWith('field=action'), true);
});

test('corner state: quiet while typing, notify only for unseen items', () => {
  assert.equal(cornerState({ count: 2, typing: true, seen: 0 }), 'quiet');
  assert.equal(cornerState({ count: 2, typing: false, seen: 1 }), 'notify');
  assert.equal(cornerState({ count: 2, typing: false, seen: 2 }), 'idle');
});

test('two shots that disagree are for the person to settle, with a way to the other shot', () => {
  assert.equal(canRewrite('continuity_mismatch'), false);
  assert.equal(canRewrite('continuity_question'), false);
  assert.equal(canRewrite('vague_action'), true);
  const note = { ...item('c', 'continuity_mismatch', 's2'), other_shot_id: 's1' };
  assert.equal(openItems([note]).length, 1);
  assert.match(otherHref(note, 'project-1'), /entity=s1/);
  assert.match(itemHref(note, 'project-1'), /entity=s2/);
});

test('pre-shoot advice opens the action field, has no draft, and asks in its own words', () => {
  const tip = {
    id: 'p',
    kind: 'pitfall',
    shot_id: 's2',
    field: 'action',
    note: 'Words like "slowly" make the movement slow.',
    ask: 'Remove the slow words from shot {number}',
  };
  assert.equal(openItems([tip]).length, 1);
  assert.equal(canRewrite('pitfall'), false);
  assert.equal(itemHref(tip, 'p1').includes('s2'), true);
  const t = (key, values) => `${key}:${values.number}`;
  assert.equal(askFor([tip], 's2', 3, t), 'Remove the slow words from shot 3');
  assert.equal(askFor([tip], 's1', 3, t), '');
  assert.equal(askFor([{ ...tip, ask: undefined }], 's2', 3, t), '');
  assert.equal(
    askFor([{ id: 'v', kind: 'vague_action', shot_id: 's2' }], 's2', 3, t),
    'inky.ask.vague_action:3',
  );
});
