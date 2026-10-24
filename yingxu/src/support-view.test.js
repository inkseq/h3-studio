import test from 'node:test';
import assert from 'node:assert/strict';

const store = new Map();
globalThis.sessionStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
const { currentSupportView, rememberSupportView, forgetSupportView, endSupportView } =
  await import('./support-view.js');

test('a view is kept per tab until it expires', () => {
  rememberSupportView({ id: 'a'.repeat(32), owner: 'supervan', expires_at: 2000, extra: 'x' });
  assert.equal(currentSupportView(1000).owner, 'supervan');
  assert.equal(currentSupportView(1000).extra, undefined);
  assert.equal(currentSupportView(2001), null);
  assert.equal(currentSupportView(1000), null);
});

test('malformed ids are ignored', () => {
  rememberSupportView({ id: 'not-hex', owner: 'x', expires_at: 9e9 });
  assert.equal(currentSupportView(), null);
  forgetSupportView();
});

test('ending posts without the view header and forgets it', async () => {
  rememberSupportView({ id: 'b'.repeat(32), owner: 'supervan', expires_at: 9e9 });
  const calls = [];
  globalThis.location = { assign: (url) => calls.push(['go', url]) };
  await endSupportView(async (url, options) => calls.push([url, options.headers]));
  assert.deepEqual(calls[0], ['/v1/admin/support-views/' + 'b'.repeat(32) + '/end', undefined]);
  assert.deepEqual(calls[1], ['go', '/admin?section=users']);
  assert.equal(currentSupportView(), null);
});
