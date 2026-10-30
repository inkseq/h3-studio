import test from 'node:test';
import assert from 'node:assert/strict';
import {
  isActiveTrashItem,
  createTrashController,
  actionFromPreview,
  checkedTrashPreview,
} from './trash-commands.js';
import { digest } from './cloud-model.js';
const target = { kind: 'entity', project_id: 'story', id: 'shot' };
const storage = () => {
  const m = new Map();
  return {
    getItem: (k) => m.get(k) ?? null,
    setItem: (k, v) => m.set(k, v),
    removeItem: (k) => m.delete(k),
  };
};
async function preview(action = 'trash') {
  const request = { action, items: [target] };
  return {
    eligible: true,
    action,
    request_hash: await digest(request),
    items: [{ target, expected_version: 3, title: 'Shot', kind: 'shot' }],
    affected_project_versions: { story: 3 },
    permanent_confirmation_required: action === 'purge',
    auto_purge: false,
  };
}
async function receipt(op, state = 'completed') {
  return {
    operation_id: 'original',
    action: op.body.action,
    request_hash: await digest(op.body),
    state,
    items: [
      {
        target,
        version: 1,
        state:
          state === 'pending'
            ? 'purging'
            : op.body.action === 'restore'
              ? 'active'
              : op.body.action === 'purge'
                ? 'purged'
                : 'trashed',
        auto_purge: false,
      },
    ],
    ledger_retained: true,
    auto_purge: false,
    media_deleted: false,
  };
}
test('lost response retains exact original body and key; known pending observes without another write', async () => {
  const requests = [],
    s = storage();
  let first = true;
  const api = {
    request: async (path, options) => {
      requests.push(structuredClone({ path, options }));
      if (first) {
        first = false;
        throw Error('lost');
      }
      return receipt(
        { body: JSON.parse(s.getItem('inkseq-trash-command-v1:owner')).body },
        requests.length === 2 ? 'pending' : 'completed',
      );
    },
  };
  const c = createTrashController({
    api,
    account: 'owner',
    getAccount: () => 'owner',
    storage: s,
    keyFactory: () => 'one',
  });
  await assert.rejects(c.start(await preview('purge')));
  const original = c.pending();
  await c.recover();
  assert.deepEqual(requests[0], requests[1]);
  assert.equal(c.pending().operationId, 'original');
  await c.recover();
  assert.equal(requests[2].path, '/v1/trash/actions/original');
  assert.deepEqual(requests[2].options, {});
  assert.equal(c.pending(), null);
  assert.equal(original.body.confirmed, true);
});
test('wrong receipt identity or hash never clears original; bare401 does not permit a new key', async () => {
  const s = storage(),
    api = {
      request: async () => {
        throw { status: 401 };
      },
    },
    c = createTrashController({ api, account: 'owner', getAccount: () => 'owner', storage: s });
  await assert.rejects(c.start(await preview()));
  const op = c.pending();
  api.request = async () => ({ ...(await receipt(op)), request_hash: 'wrong' });
  await assert.rejects(c.recover());
  assert.deepEqual(c.pending(), op);
  api.request = async () => ({
    ...(await receipt(op)),
    items: [
      { target: { ...target, id: 'other' }, version: 1, state: 'trashed', auto_purge: false },
    ],
  });
  await assert.rejects(c.recover());
  assert.deepEqual(c.pending(), op);
});
test('account switch during response leaves original operation for its original account', async () => {
  let account = 'owner',
    resolve;
  const s = storage(),
    api = { request: () => new Promise((r) => (resolve = r)) },
    c = createTrashController({ api, account: 'owner', getAccount: () => account, storage: s });
  const run = c.start(await preview());
  const op = c.pending();
  account = 'other';
  resolve(await receipt(op));
  await assert.rejects(run);
  assert.deepEqual(c.pending(), op);
});
test('only matching positive no-write proof clears; preview cannot replace selected targets or versions', async () => {
  const value = await preview();
  await checkedTrashPreview(value, { action: 'trash', items: [target] });
  await assert.rejects(
    checkedTrashPreview(
      { ...value, items: [{ ...value.items[0], target: { ...target, id: 'other' } }] },
      { action: 'trash', items: [target] },
    ),
  );
  assert.equal(actionFromPreview(value).confirmed, undefined);
  const s = storage();
  let matching = false;
  const api = {
      request: async (path, o) => {
        throw {
          detail: {
            detail: {
              code: 'trash_version_conflict',
              resource: 'trash',
              no_mutation_committed: true,
              request_hash: matching ? await digest(o.body) : 'wrong',
            },
          },
        };
      },
    },
    c = createTrashController({ api, account: 'owner', getAccount: () => 'owner', storage: s });
  await assert.rejects(c.start(value));
  assert.ok(c.pending());
  matching = true;
  await assert.rejects(c.recover(), /trash.changed/);
  assert.equal(c.pending(), null);
});

test('unrecognised no-write claims and foreign known operation IDs cannot release a pending purge', async () => {
  const s = storage();
  let response;
  const api = {
      request: async () => {
        if (response instanceof Error) throw response;
        return response;
      },
    },
    c = createTrashController({ api, account: 'owner', getAccount: () => 'owner', storage: s });
  response = Error('network');
  await assert.rejects(c.start(await preview('purge')));
  const original = c.pending();
  const unknown = Error('unknown');
  unknown.detail = {
    resource: 'trash',
    code: 'unreviewed_new_code',
    no_mutation_committed: true,
    request_hash: await digest(original.body),
  };
  response = unknown;
  await assert.rejects(c.recover());
  assert.deepEqual(c.pending(), original);
  response = await receipt(original, 'pending');
  await c.recover();
  const known = c.pending();
  response = { ...(await receipt(known)), operation_id: 'foreign' };
  await assert.rejects(c.recover());
  assert.deepEqual(c.pending(), known);
});

test('active workbench projections hide retained trash audit rows without removing history', () => {
  const rows = [
    { id: 'legacy' },
    { id: 'active', trash_state: 'active' },
    { id: 'trash', trash_state: 'trashed' },
    { id: 'purging', trash_state: 'purging' },
    { id: 'purged', trash_state: 'purged' },
    { id: 'unknown', trash_state: 'unknown' },
  ];
  assert.deepEqual(
    rows.filter(isActiveTrashItem).map((r) => r.id),
    ['legacy', 'active'],
  );
  assert.equal(rows.length, 6);
});
