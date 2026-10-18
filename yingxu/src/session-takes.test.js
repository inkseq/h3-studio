import test from 'node:test';
import assert from 'node:assert/strict';
import {
  checkedSessionTakes,
  sessionTakeRows,
  takeStatusGroup,
  readSessionTakes,
} from './session-takes.js';
const row = (n) => ({
  id: 'take_' + n,
  card_id: 'card_' + n,
  revision_id: 'revision_' + n,
  status: 'succeeded',
  annotation: { starred: n % 2 === 0 },
  trash_state: 'active',
  artifacts: [],
});
const page = (n, cursor = null) => ({
  session_id: 'session1',
  projection: 'original_takes_v1',
  includes_trashed: true,
  total_count: 66,
  takes: [row(n)],
  before_cursor: cursor,
});
test('a session grid reads original pages independently of loaded conversation cards', async () => {
  const calls = [],
    rows = [];
  const client = {
    sessionTakes: async (id, options) => {
      calls.push([id, options.before_cursor]);
      return calls.length === 1 ? page(66, 'older') : page(1);
    },
  };
  assert.equal(
    await readSessionTakes(client, 'session1', {
      isCurrent: () => true,
      onPage: (value) => rows.push(...value.takes),
    }),
    true,
  );
  assert.deepEqual(calls, [
    ['session1', null],
    ['session1', 'older'],
  ]);
  assert.deepEqual(
    rows.map((value) => value.id),
    ['take_66', 'take_1'],
  );
});
test('late old-account or old-session pages cannot render or start another read', async () => {
  let finish,
    current = true;
  const values = [],
    client = { sessionTakes: () => new Promise((resolve) => (finish = resolve)) };
  const reading = readSessionTakes(client, 'session1', {
    isCurrent: () => current,
    onPage: (value) => values.push(value),
  });
  current = false;
  finish(page(1, 'older'));
  assert.equal(await reading, false);
  assert.deepEqual(values, []);
});
test('closing the grid cancels a late read and cyclic or foreign cursors stop before rendering', async () => {
  const abort = new AbortController(),
    values = [];
  let finish;
  const reading = readSessionTakes(
    { sessionTakes: () => new Promise((resolve) => (finish = resolve)) },
    'session1',
    { signal: abort.signal, isCurrent: () => true, onPage: (value) => values.push(value) },
  );
  abort.abort();
  finish(page(1));
  assert.equal(await reading, false);
  assert.deepEqual(values, []);
  let calls = 0;
  await assert.rejects(
    readSessionTakes(
      { sessionTakes: async () => page(++calls, calls === 1 ? 'A' : calls === 2 ? 'B' : 'A') },
      'session1',
      { isCurrent: () => true, onPage: (value) => values.push(value) },
    ),
    /sessionTakes.invalid/,
  );
  assert.equal(calls, 3);
  assert.equal(values.length, 2);
  assert.throws(() => checkedSessionTakes({ ...page(1), session_id: 'other' }, 'session1'));
  assert.throws(() => checkedSessionTakes({ ...page(1), takes: [row(1), row(1)] }, 'session1'));
});
test('filters cover loaded original metadata and exclude recoverable Trash without editing it', () => {
  const rows = [
    row(1),
    { ...row(2), status: 'collecting' },
    { ...row(3), status: 'recovery_hold' },
    { ...row(4), trash_state: 'trashed' },
  ];
  const before = structuredClone(rows);
  assert.deepEqual(
    sessionTakeRows(rows, { starred: true }).map((value) => value.id),
    ['take_2'],
  );
  assert.deepEqual(
    sessionTakeRows(rows, { status: 'running' }).map((value) => value.id),
    ['take_2'],
  );
  assert.deepEqual(
    sessionTakeRows(rows, { status: 'unknown' }).map((value) => value.id),
    ['take_3'],
  );
  assert.equal(takeStatusGroup('pending_admission'), 'queued');
  assert.deepEqual(rows, before);
});
