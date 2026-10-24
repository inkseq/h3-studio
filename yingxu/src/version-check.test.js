import test from 'node:test';
import assert from 'node:assert/strict';
import { entryOf, installVersionCheck, isNewer } from './version-check.js';

const page = (entry) => `<script type="module" crossorigin src="${entry}"></script>`;

test('reads the hashed entry script', () => {
  assert.equal(entryOf(page('/assets/index-Dg0PIQOm.js')), '/assets/index-Dg0PIQOm.js');
  assert.equal(entryOf('<script src="/src/main.jsx"></script>'), null);
  assert.equal(isNewer('/assets/index-a.js', '/assets/index-b.js'), true);
  assert.equal(isNewer('/assets/index-a.js', '/assets/index-a.js'), false);
  assert.equal(isNewer('/assets/index-a.js', null), false);
});

function fakeDocument(entry) {
  const listeners = {};
  return {
    visibilityState: 'visible',
    querySelectorAll: () => (entry ? [{ getAttribute: () => entry }] : []),
    addEventListener: (name, fn) => (listeners[name] = fn),
    removeEventListener: (name) => delete listeners[name],
    fire: (name) => listeners[name]?.(),
  };
}

test('offers a reload once when the site serves a different build', async () => {
  const doc = fakeDocument('/assets/index-old.js');
  const shown = [];
  let served = '/assets/index-old.js';
  const stop = installVersionCheck({
    doc,
    every: 1e9,
    fetcher: async () => ({ ok: true, text: async () => page(served) }),
    notify: (text, options) => shown.push([text, options.action.label]),
  });
  doc.fire('visibilitychange');
  await new Promise((done) => setTimeout(done, 0));
  assert.equal(shown.length, 0);
  served = '/assets/index-new.js';
  doc.fire('visibilitychange');
  await new Promise((done) => setTimeout(done, 0));
  doc.fire('visibilitychange');
  await new Promise((done) => setTimeout(done, 0));
  assert.equal(shown.length, 1);
  stop();
});

test('does nothing on the dev server', () => {
  let fetched = false;
  const stop = installVersionCheck({
    doc: fakeDocument(null),
    fetcher: async () => (fetched = true),
  });
  stop();
  assert.equal(fetched, false);
});
