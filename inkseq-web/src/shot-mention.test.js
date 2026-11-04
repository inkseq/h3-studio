import assert from 'node:assert/strict';
import test from 'node:test';
import {
  mentionAt,
  mentionOptions,
  readCardSize,
  rememberCardSize,
  withMention,
} from './shot-mention.js';

test('a mention starts after a space or at the start, never inside a word', () => {
  assert.deepEqual(mentionAt('@', 1), { start: 0, end: 1, query: '' });
  assert.deepEqual(mentionAt('走进 @小', 5), { start: 3, end: 5, query: '小' });
  assert.equal(mentionAt('mail a@b', 8), null);
  assert.equal(mentionAt('@小明 跑', 5), null);
  assert.equal(mentionAt(undefined, 0), null);
});

test('options match any part of a name, up to six', () => {
  const profiles = ['林夏', '林中木屋', '老周', 'a', 'b', 'c', 'd', 'e'].map((name, id) => ({
    id,
    name,
  }));
  assert.deepEqual(
    mentionOptions(profiles, '林').map((p) => p.name),
    ['林夏', '林中木屋'],
  );
  assert.equal(mentionOptions(profiles, '').length, 6);
  assert.deepEqual(mentionOptions(null, ''), []);
});

test('choosing replaces only what was typed and leaves the caret after it', () => {
  const text = '她看向 @林 然后转身',
    mention = mentionAt(text, 6);
  const next = withMention(text, mention, '林夏');
  assert.equal(next.text, '她看向 林夏  然后转身');
  assert.equal(next.caret, 7);
});

test('card size is remembered, and bad or blocked storage falls back', () => {
  const data = new Map(),
    storage = {
      getItem: (key) => data.get(key) ?? null,
      setItem: (key, value) => data.set(key, value),
    };
  assert.equal(readCardSize(storage), 'm');
  rememberCardSize(storage, 'l');
  assert.equal(readCardSize(storage), 'l');
  rememberCardSize(storage, 'huge');
  assert.equal(readCardSize(storage), 'm');
  const blocked = {
    getItem() {
      throw Error('blocked');
    },
    setItem() {
      throw Error('blocked');
    },
  };
  assert.equal(readCardSize(blocked), 'm');
  assert.doesNotThrow(() => rememberCardSize(blocked, 'l'));
});
