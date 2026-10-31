import test from 'node:test';
import assert from 'node:assert/strict';
import { QueryClient } from '@tanstack/react-query';
import { accountKey, installAccountScope } from './data/query.js';

test('cached reads are dropped when the signed-in account changes', () => {
  const client = new QueryClient();
  let state = { account: 'alice' };
  const listeners = new Set();
  const controller = {
    getState: () => state,
    subscribe: (fn) => (listeners.add(fn), () => listeners.delete(fn)),
  };
  const stop = installAccountScope(controller, client);
  client.setQueryData(accountKey('alice', 'prefs'), { copies_max: 4 });
  state = { account: 'alice', busy: true };
  listeners.forEach((fn) => fn());
  assert.deepEqual(client.getQueryData(accountKey('alice', 'prefs')), { copies_max: 4 });
  state = { account: 'bob' };
  listeners.forEach((fn) => fn());
  assert.equal(client.getQueryData(accountKey('alice', 'prefs')), undefined);
  assert.notDeepEqual(accountKey('alice', 'x'), accountKey('bob', 'x'));
  stop();
});
