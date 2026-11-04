import test from 'node:test';
import assert from 'node:assert/strict';
import { isChunkError, readComposerDraft, reloadOnce, writeComposerDraft } from './chunk-reload.js';

const memory = () => {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
};

test('a missing code file reloads once, not in a loop', () => {
  const storage = memory();
  let reloads = 0;
  const reload = () => reloads++;
  assert.equal(reloadOnce({ storage, reload, now: 100000 }), true);
  assert.equal(reloadOnce({ storage, reload, now: 110000 }), false);
  assert.equal(reloads, 1);
  assert.equal(reloadOnce({ storage, reload, now: 200000 }), true);
  assert.equal(reloads, 2);
});

test('only missing code files count', () => {
  assert.ok(isChunkError(Error('Failed to fetch dynamically imported module: /assets/a.js')));
  assert.ok(isChunkError(Error('Importing a module script failed.')));
  assert.ok(!isChunkError(Error('Cannot read properties of undefined')));
});

test('a typed draft is kept per account and story, and cleared when empty', () => {
  const storage = memory();
  writeComposerDraft('a', null, 'hello', storage);
  assert.equal(readComposerDraft('a', null, storage), 'hello');
  assert.equal(readComposerDraft('b', null, storage), '');
  assert.equal(readComposerDraft('a', 'story', storage), '');
  writeComposerDraft('a', null, '', storage);
  assert.equal(readComposerDraft('a', null, storage), '');
});
