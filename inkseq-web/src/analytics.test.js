import test from 'node:test';
import assert from 'node:assert/strict';
import { anonymousId, checkoutAnalytics, safeProperties, track } from './analytics.js';

test('event properties never carry free text', () => {
  assert.deepEqual(
    safeProperties({
      pack: 'creator',
      turn: 3,
      live: true,
      prompt: 'a fox at dusk',
      title: 'My film!',
    }),
    { pack: 'creator', turn: 3, live: true },
  );
});

test('tracking is a no-op until analytics is installed (local, tests)', () => {
  assert.doesNotThrow(() => track('story_chat_message', { turn: 1 }));
});

test('checkout analytics is absent until analytics runs, and the id is only an anonymous UUID', () => {
  assert.equal(checkoutAnalytics(), undefined);
  assert.equal(checkoutAnalytics('1'), undefined);
  assert.ok(anonymousId('019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b'));
  for (const bad of ['user@example.com', 'qa-admin', 'owner-1', '', null, 42])
    assert.equal(anonymousId(bad), false);
});
