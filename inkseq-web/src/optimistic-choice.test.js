import test from 'node:test';
import assert from 'node:assert/strict';
import { latestOnly } from './optimistic-choice.js';

test('choices made while a write is out send only the newest, one at a time', async () => {
  const sent = [],
    gates = [];
  const write = latestOnly((value) => {
    sent.push(value);
    return new Promise((resolve) => gates.push(resolve));
  });
  const done = write('a');
  write('b');
  write('c');
  await Promise.resolve();
  assert.deepEqual(sent, ['a']);
  gates.shift()();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(sent, ['a', 'c']);
  gates.shift()();
  await done;
  write('d');
  await Promise.resolve();
  assert.deepEqual(sent, ['a', 'c', 'd']);
  gates.shift()();
});

test('a refused write does not stop the next choice or reject the press', async () => {
  const sent = [];
  const write = latestOnly(async (value) => {
    sent.push(value);
    if (value === 'a') throw Error('refused');
  });
  const first = write('a');
  write('b');
  await first;
  assert.deepEqual(sent, ['a', 'b']);
});
