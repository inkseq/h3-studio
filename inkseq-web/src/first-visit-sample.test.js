import test from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from './store.js';

const memory = () => {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
};

test('the first-visit example is marked until someone edits it', () => {
  const storage = memory();
  const store = createStore({ storage });
  assert.equal(store.getLocalProjectSummary().sample, true);
  const again = createStore({ storage });
  assert.equal(again.getLocalProjectSummary().sample, true);
  again.updateProject({ title: 'Mine now' });
  assert.equal(again.getLocalProjectSummary().sample, undefined);
  assert.equal(createStore({ storage }).getLocalProjectSummary().sample, undefined);
});
