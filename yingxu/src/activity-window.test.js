import test from 'node:test';
import assert from 'node:assert/strict';
import { emptyActivityWindow, updateActivityWindow } from './activity-window.js';

const items = (top, bottom) =>
  Array.from({ length: top - bottom + 1 }, (_, index) => ({
    id: `event-${top - index}`,
    project_version: top - index,
  }));
const page = (top, bottom, cursor = bottom) => ({
  items: items(top, bottom),
  next_before_version: cursor,
});

test('a newer overlapping window retains an explicitly loaded older-page cursor', () => {
  let state = updateActivityWindow(emptyActivityWindow(), page(100, 51));
  state = updateActivityWindow(state, page(50, 1, null), { older: true });
  state = updateActivityWindow(state, page(110, 61));
  assert.equal(state.cursor, null);
  assert.equal(state.events.length, 110);
  assert.equal(state.events.at(-1).project_version, 1);
  assert.equal(state.windowReset, false);
});

test('more than one page of new events resets both displayed window and pagination so the gap can be loaded', () => {
  let state = updateActivityWindow(emptyActivityWindow(), page(100, 51));
  state = updateActivityWindow(state, page(170, 121));
  assert.equal(state.windowReset, true);
  assert.equal(state.cursor, 121);
  assert.equal(state.events.length, 50);
  assert.equal(state.events.at(-1).project_version, 121);
  state = updateActivityWindow(state, page(120, 71), { older: true });
  assert.equal(state.cursor, 71);
  assert.equal(state.events.length, 100);
  assert.equal(state.windowReset, false);
  assert.ok(state.events.some((event) => event.project_version === 120));
  assert.ok(state.events.some((event) => event.project_version === 101));
  state = updateActivityWindow(state, page(70, 21), { older: true });
  assert.equal(state.events.length, 150);
  assert.equal(new Set(state.events.map((event) => event.id)).size, 150);
});

test('an empty history discovers new records and a complete refreshed page does not invent pagination', () => {
  let state = updateActivityWindow(emptyActivityWindow(), { items: [], next_before_version: null });
  state = updateActivityWindow(state, page(3, 1, null));
  assert.equal(state.events.length, 3);
  assert.equal(state.cursor, null);
  assert.equal(state.windowReset, false);
});

test('malformed cursor does not discard retained activity or silently claim the full history is loaded', () => {
  const state = updateActivityWindow(emptyActivityWindow(), page(100, 51)),
    saved = structuredClone(state);
  assert.throws(
    () => updateActivityWindow(state, { items: items(170, 121), next_before_version: 'bad' }),
    /Invalid/,
  );
  assert.deepEqual(state, saved);
});
