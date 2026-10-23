import test from 'node:test';
import assert from 'node:assert/strict';
import { safeProperties, track } from './analytics.js';

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
