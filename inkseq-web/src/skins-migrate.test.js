import test from 'node:test';
import assert from 'node:assert/strict';

function storage(values) {
  const map = new Map(Object.entries(values));
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: (key) => map.delete(key),
    map,
  };
}

test('the old light/dark pair moves to coral once; later choices are kept', async () => {
  globalThis.localStorage = storage({ 'inkseq.skin': 'default' });
  const { chosenSkin } = await import('./skins.js?first');
  assert.equal(chosenSkin(), 'coral-dark');
  assert.equal(localStorage.getItem('inkseq.skin'), 'coral-dark');
  // A deliberate pick of the old look after the move stays.
  localStorage.setItem('inkseq.skin', 'default');
  assert.equal(chosenSkin(), 'default');
  globalThis.localStorage = storage({ 'inkseq.skin': 'nordic' });
  assert.equal(chosenSkin(), 'coral');
  globalThis.localStorage = storage({ 'inkseq.skin': 'taisho' });
  assert.equal(chosenSkin(), 'taisho');
  globalThis.localStorage = storage({});
  assert.equal(chosenSkin(), '');
  delete globalThis.localStorage;
});
