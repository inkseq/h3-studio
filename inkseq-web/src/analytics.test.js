import test from 'node:test';
import assert from 'node:assert/strict';
import {
  anonymousId,
  checkoutAnalytics,
  replayAddress,
  replayAttribute,
  replayRequest,
  resetAnalytics,
  safeProperties,
  track,
  withoutAttribution,
} from './analytics.js';

test('speed events keep their numbers and drop the address they name', () => {
  const event = withoutAttribution({
    event: '$web_vitals',
    properties: {
      $web_vitals_LCP_event: {
        value: 2896,
        attribution: { url: 'https://inkseq.com/v1/artifacts/1/content?expected_account=sam' },
      },
      $current_url: 'https://inkseq.com/',
    },
  });
  assert.deepEqual(event.properties.$web_vitals_LCP_event, { value: 2896 });
  assert.equal(event.properties.$current_url, 'https://inkseq.com/');
  assert.equal(withoutAttribution(null), null);
});

test('replays keep the page shape but not words or media links', () => {
  assert.equal(replayAttribute('title', '林澈'), '**');
  assert.equal(replayAttribute('aria-label', 'Add Sam'), '*******');
  assert.equal(replayAttribute('class', 'story-card on'), 'story-card on');
  assert.equal(replayAttribute('srcset', '/a.png 2x'), '');
  assert.equal(
    replayAttribute('style', 'width:4px;background:url(/v1/a?x=1)'),
    'width:4px;background:none',
  );
  assert.equal(replayAttribute('style', 'width:4px'), 'width:4px');
});

test('replay addresses drop queries and other hosts', () => {
  const origin = 'https://inkseq.com';
  assert.equal(
    replayAddress('https://inkseq.com/v1/artifacts/1/content?expected_account=sam', origin),
    'https://inkseq.com/v1/artifacts/1/content',
  );
  assert.equal(replayAddress('/assets/a.js', origin), 'https://inkseq.com/assets/a.js');
  assert.equal(
    replayAddress('https://media.example.com/owners/sam/a.png?Signature=x', origin),
    'https://media.example.com',
  );
  assert.equal(
    replayAddress('https://fonts.googleapis.com/css2?family=Inter', origin),
    'https://fonts.googleapis.com/css2?family=Inter',
  );
  assert.equal(replayAddress('data:image/png;base64,AAAA', origin), 'data://');
  assert.equal(replayAddress('http://[', origin), '');
  assert.deepEqual(
    replayRequest({ name: 'https://media.example.com/a?Signature=x', duration: 4 }).duration,
    4,
  );
});

test('resetting analytics before it starts does nothing', () => {
  assert.doesNotThrow(() => resetAnalytics());
});

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
