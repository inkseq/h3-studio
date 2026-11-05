import test from 'node:test';
import assert from 'node:assert/strict';
import { cornerState, itemHref, openItems } from './inky/inky-model.js';

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
