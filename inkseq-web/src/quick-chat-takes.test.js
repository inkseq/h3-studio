import { digest } from './cloud-model.js';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createQuickChatClient } from './quick-chat-client.js';
import { createQuickChatController } from './quick-chat-controller.js';
import { preferenceBody, checkedPreferences } from './quick-chat-takes.js';
const memory = () => {
  const values = new Map();
  return {
    getItem: (k) => values.get(k) || null,
    setItem: (k, v) => values.set(k, v),
    removeItem: (k) => values.delete(k),
  };
};
const fake = (extra = {}) => ({
  setAccount() {},
  reset() {},
  schema: async () => ({}),
  capabilities: async () => ({}),
  sessions: async () => ({ sessions: [] }),
  session: async (id) => ({ id, version: 1 }),
  materials: async () => ({ materials: [] }),
  timeline: async () => ({ events: [] }),
  ...extra,
});
async function ready(client, storage = memory()) {
  const c = createQuickChatController({ client, storage });
  await c.setAccount('owner');
  await c.open('session');
  return c;
}

test('editable preference CAS has no hidden 4/8 maximum and rejects noninteger/lossy values', () => {
  const old = { version: 7, copies_max: 4, takes_fold_count: 8 };
  assert.deepEqual(preferenceBody(old, '64', '200'), {
    expected_version: 7,
    copies_max: 64,
    takes_fold_count: 200,
  });
  for (const n of [0, -1, 1.1, Infinity, 2147483648])
    assert.throws(() => checkedPreferences({ ...old, copies_max: n }));
  assert.equal(old.copies_max, 4);
});

test('take writes use same-origin owner fence and explicit CAS/key without submit', async () => {
  const calls = [],
    client = createQuickChatClient({
      fetcher: async (path, options) => {
        calls.push({ path, options });
        return new Response('{}');
      },
    });
  client.setAccount('owner');
  await client.starTake('s', 'i', { expected_version: 2, starred: true }, 'star-key');
  await client.again(
    's',
    'c',
    { expected_card_version: 3, source_revision_id: 'r', item_id: 'i', copies: 1 },
    'again-key',
  );
  await client.takeToStory(
    's',
    'i',
    { target_project_id: 'p', expected_project_version: 4, parent_id: 'scene' },
    'transfer-key',
  );
  assert.equal(calls[0].options.method, 'PATCH');
  assert.ok(calls.every((x) => x.options.headers['X-Expected-Account'] === 'owner'));
  assert.deepEqual(
    calls.map((x) => x.options.headers['Idempotency-Key']),
    ['star-key', 'again-key', 'transfer-key'],
  );
  assert.ok(calls.every((x) => !x.path.includes('submissions') && !x.path.includes('preflights')));
});
test('unknown Again retains exact body/key and explicit recovery does not submit', async () => {
  const calls = [],
    client = fake({
      again: async (...args) => {
        calls.push(args);
        if (calls.length === 1) throw Error('lost');
        return { revision: { id: 'new' } };
      },
    }),
    c = await ready(client);
  await assert.rejects(c.again({ id: 'card', version: 2 }, { id: 'take', revision_id: 'rev' }));
  assert.equal(c.getState().pending.method, 'again');
  await c.recover();
  assert.deepEqual(calls[0], calls[1]);
  assert.equal(c.getState().pending, null);
});
test('pending transfer recovery GET preserves one original shot operation', async () => {
  let posts = 0,
    gets = 0;
  const client = fake({
      takeToStory: async () => {
        posts++;
        return {
          source_session_id: 'session',
          item_id: 'take',
          expected_project_version: 4,
          parent_id: 'scene',
          id: 'transfer',
          status: 'unknown',
          project_id: 'p',
          seed_locked: true,
        };
      },
      storyTransfer: async (s, id) => {
        gets++;
        assert.equal(s, 'session');
        assert.equal(id, 'transfer');
        return {
          source_session_id: 'session',
          item_id: 'take',
          expected_project_version: 4,
          parent_id: 'scene',
          id,
          status: 'completed',
          project_id: 'p',
          project_version: 5,
          seed_locked: true,
          shot_id: 'shot',
          web_url: '/?project=p&entity=shot',
        };
      },
    }),
    c = await ready(client);
  await c.takeToStory(
    { id: 'take' },
    { target_project_id: 'p', expected_project_version: 4, parent_id: 'scene' },
  );
  assert.equal(c.getState().pending.transferId, 'transfer');
  await c.recover();
  assert.equal(posts, 1);
  assert.equal(gets, 1);
  assert.equal(c.getState().pending, null);
});
test('transfer receipt mismatch stays pending; explicit resume retains original key/body', async () => {
  const calls = [];
  let valid = false;
  const client = fake({
      takeToStory: async (...args) => {
        calls.push(args);
        return {
          source_session_id: 'session',
          item_id: 'take',
          expected_project_version: 4,
          parent_id: 'scene',
          id: 'transfer',
          status: valid ? 'completed' : 'pending',
          project_id: 'p',
          seed_locked: true,
          project_version: 5,
          shot_id: 'shot',
          web_url: 'https://foreign.invalid/?project=p&entity=shot',
        };
      },
    }),
    c = await ready(client);
  await c.takeToStory(
    { id: 'take' },
    { target_project_id: 'p', expected_project_version: 4, parent_id: 'scene' },
  );
  valid = true;
  await assert.rejects(c.resumeStoryTransfer());
  assert.deepEqual(calls[0], calls[1]);
  assert.equal(c.getState().pending.transferId, 'transfer');
});
test('old-account late write cannot clear original pending or affect new account', async () => {
  let finish;
  const storage = memory(),
    c = await ready(fake({ starTake: () => new Promise((r) => (finish = r)) }), storage);
  const request = c.starTake({ id: 'take', annotation: { version: 0 } }, true);
  await c.setAccount('other');
  finish({ take_id: 'take', version: 1, starred: true });
  await assert.rejects(request);
  assert.ok(storage.getItem('yingxu:quick-chat:operation:owner'));
  assert.equal(c.getState().account, 'other');
  assert.equal(c.getState().pending, null);
});
test('partial transfer followed by 401/403/404/409 retains original command across restored auth', async () => {
  for (const status of [401, 403, 404, 409]) {
    let first = true;
    const calls = [],
      storage = memory(),
      client = fake({
        takeToStory: async (...args) => {
          calls.push(args);
          if (first) {
            first = false;
            throw Object.assign(Error('after partial asset copy'), { status });
          }
          return {
            source_session_id: 'session',
            item_id: 'take',
            expected_project_version: 4,
            parent_id: 'scene',
            id: 'original',
            status: 'completed',
            project_id: 'p',
            project_version: 5,
            seed_locked: true,
            shot_id: 'shot',
            web_url: '/?project=p&entity=shot',
          };
        },
      }),
      c = await ready(client, storage);
    await assert.rejects(
      c.takeToStory(
        { id: 'take' },
        { target_project_id: 'p', expected_project_version: 4, parent_id: 'scene' },
      ),
    );
    assert.equal(c.getState().pending.method, 'takeToStory');
    const restored = await ready(client, storage);
    await restored.recover();
    assert.deepEqual(calls[0], calls[1]);
    assert.equal(restored.getState().pending, null);
  }
});

test('closed no-shot proof requires explicit fresh acknowledgement; never retargets automatically', async () => {
  const calls = [],
    receipt = {
      source_session_id: 'session',
      item_id: 'take',
      expected_project_version: 4,
      parent_id: 'scene',
      id: 'transfer',
      status: 'blocked',
      no_shot_committed: true,
      error_code: 'target_version_conflict',
      project_id: 'p',
      shot_id: 'reserved-shot',
      seed: '7',
      seed_locked: true,
      web_url: null,
    };
  let response = receipt;
  const client = fake({
      takeToStory: async (...args) => {
        calls.push(args);
        return receipt;
      },
      storyTransfer: async () => response,
    }),
    storage = memory(),
    c = await ready(client, storage);
  await c.takeToStory(
    { id: 'take' },
    { target_project_id: 'p', expected_project_version: 4, parent_id: 'scene' },
  );
  assert.equal(c.getState().pending.closedReceipt.no_shot_committed, true);
  const original = c.getState().pending;
  for (const patch of [
    { source_session_id: 'other' },
    { item_id: 'other' },
    { parent_id: 'other' },
    { expected_project_version: 5 },
    { no_shot_committed: false },
    { status: 'unknown' },
    { error_code: 'other' },
    { project_id: 'other' },
    { id: 'other' },
    { web_url: '/?project=p&entity=reserved-shot' },
  ]) {
    response = { ...receipt, ...patch };
    await assert.rejects(c.acknowledgeBlockedTransfer());
    assert.equal(c.getState().pending.key, original.key);
  }
  response = receipt;
  await c.acknowledgeBlockedTransfer();
  assert.equal(c.getState().pending, null);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0][2], original.body);
});

test('completed transfer mismatched original receipt identity never clears pending', async () => {
  const receipt = {
    source_session_id: 'session',
    item_id: 'take',
    expected_project_version: 4,
    parent_id: 'scene',
    id: 'transfer',
    status: 'completed',
    project_id: 'p',
    project_version: 5,
    shot_id: 'shot',
    seed_locked: true,
    web_url: '/?project=p&entity=shot',
  };
  for (const patch of [
    { source_session_id: 'other' },
    { item_id: 'other' },
    { parent_id: 'other' },
    { expected_project_version: 5 },
    { id: 'other' },
  ]) {
    const c = await ready(
      fake({
        takeToStory: async () => ({ ...receipt, status: 'pending' }),
        storyTransfer: async () => ({ ...receipt, ...patch }),
      }),
    );
    await c.takeToStory(
      { id: 'take' },
      { target_project_id: 'p', expected_project_version: 4, parent_id: 'scene' },
    );
    const original = c.getState().pending;
    await assert.rejects(c.recover());
    assert.equal(c.getState().pending.key, original.key);
    assert.equal(c.getState().pending.transferId, 'transfer');
  }
});

test('take display rename preserves omitted star and original body/key across lost reply and validates exact annotation receipt', async () => {
  const calls = [];
  let mode = 'lost';
  const c = await ready(
    fake({
      starTake: async (...args) => {
        calls.push(structuredClone(args));
        if (mode === 'lost') throw Error('lost');
        return {
          take_id: mode === 'foreign' ? 'other' : 'take',
          version: 3,
          title: 'New name',
          starred: true,
        };
      },
    }),
  );
  await assert.rejects(
    c.renameTake({ id: 'take', annotation: { version: 2, starred: true } }, 'New name'),
  );
  assert.deepEqual(calls[0][2], { expected_version: 2, title: 'New name' });
  mode = 'foreign';
  await assert.rejects(c.recover());
  assert.ok(c.getState().pending);
  mode = 'valid';
  await c.recover();
  assert.equal(c.getState().pending, null);
  assert.deepEqual(calls[0], calls[2]);
});
test('annotation bare409 and foreign closed proof retain pending; exact no-write proof permits fresh explicit edit', async () => {
  let mode = 'bare';
  const c = await ready(
    fake({
      starTake: async (s, id, body) => {
        throw {
          status: 409,
          detail:
            mode === 'bare'
              ? {}
              : {
                  code: 'version_conflict',
                  resource: 'quick_chat_take',
                  session_id: s,
                  take_id: mode === 'foreign' ? 'other' : id,
                  no_mutation_committed: true,
                  request_hash: await digest(body),
                },
        };
      },
    }),
  );
  await assert.rejects(c.renameTake({ id: 'take', annotation: { version: 2 } }, 'Name'));
  assert.ok(c.getState().pending);
  mode = 'foreign';
  await assert.rejects(c.recover());
  assert.ok(c.getState().pending);
  mode = 'exact';
  await assert.rejects(c.recover());
  assert.equal(c.getState().pending, null);
});
