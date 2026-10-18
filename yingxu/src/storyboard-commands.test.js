import test from 'node:test';
import assert from 'node:assert/strict';
import { storyboardCommandStore, verifyStoryboardReceipt } from './storyboard-commands.js';
const storage = () => {
  const m = new Map();
  return {
    getItem: (k) => m.get(k),
    setItem: (k, v) => m.set(k, v),
    removeItem: (k) => m.delete(k),
  };
};
test('pending survives lost response and keeps exact original body/key across reconstruction', () => {
  const s = storage(),
    a = storyboardCommandStore({
      storage: s,
      account: 'a',
      projectId: 'p',
      keyFactory: () => 'fixed',
    }),
    body = {
      expected_version: 3,
      actions: [{ op: 'storyboard.bulk', shot_ids: ['x'], operation: 'final' }],
    },
    op = a.begin('actions', body);
  body.actions[0].shot_ids.push('y');
  assert.deepEqual(a.read().body.actions[0].shot_ids, ['x']);
  assert.throws(() => a.begin('actions', body));
  const b = storyboardCommandStore({ storage: s, account: 'a', projectId: 'p' });
  assert.deepEqual(b.read(), op);
  assert.equal(
    verifyStoryboardReceipt(op, { id: 'other', version: 4, project: { id: 'other' } }),
    false,
  );
  assert.equal(storyboardCommandStore({ storage: s, account: 'b', projectId: 'p' }).read(), null);
  b.clear(op);
  assert.equal(a.read(), null);
});

test('preflight receipt binds every original shot/copy pair and distinct plan identity', () => {
  const op = {
    kind: 'preflight',
    projectId: 'p',
    body: { expected_version: 3, shot_ids: ['a', 'b'], copies: 2 },
  };
  const value = {
    id: 'original',
    project_id: 'p',
    project_version: 3,
    items: ['a', 'b'].flatMap((shot_id) =>
      [0, 1].map((index) => ({ shot_id, index, plan_id: shot_id + index })),
    ),
  };
  assert.equal(verifyStoryboardReceipt(op, value), true);
  for (const change of [
    (v) => (v.items[1] = { ...v.items[0] }),
    (v) => (v.items[1].index = 2),
    (v) => (v.items[2].plan_id = v.items[0].plan_id),
    (v) => (v.project_version = 4),
  ]) {
    const bad = structuredClone(value);
    change(bad);
    assert.equal(verifyStoryboardReceipt(op, bad), false);
  }
});
test('preference receipt must reflect the exact submitted values, not merely a newer version', () => {
  const op = {
    kind: 'preferences',
    body: { expected_version: 3, defaults: { controls: { steps: 12 }, recipe_id: 'ref' } },
  };
  assert.equal(
    verifyStoryboardReceipt(op, {
      version: 4,
      defaults: { recipe_id: 'ref', controls: { steps: 12 } },
    }),
    true,
  );
  assert.equal(
    verifyStoryboardReceipt(op, {
      version: 4,
      defaults: { recipe_id: 'ref', controls: { steps: 8 } },
    }),
    false,
  );
  const copies = {
    kind: 'copies',
    body: { expected_version: 1, copies_max: 6, takes_fold_count: 3 },
  };
  assert.equal(
    verifyStoryboardReceipt(copies, { version: 2, copies_max: 6, takes_fold_count: 8 }),
    false,
  );
});
