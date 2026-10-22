import test from 'node:test';
import assert from 'node:assert/strict';
import {
  accessFrom,
  hostedSignInEnabled,
  hostedSignInUrl,
  providerLogoutUrl,
  readAccessFor,
  readAccess,
  resetAccess,
} from './account-access.js';

test('anonymous or failed reads expose nothing privileged', () => {
  assert.deepEqual(accessFrom(null), {
    canPreview: false,
    previewing: false,
    signedIn: false,
    displayName: null,
    email: null,
    authentication: null,
    creditsEnabled: false,
    gpus: false,
    admin: false,
  });
  assert.deepEqual(accessFrom({}), {
    canPreview: false,
    previewing: false,
    signedIn: false,
    displayName: null,
    email: null,
    authentication: null,
    creditsEnabled: false,
    gpus: false,
    admin: false,
  });
});

test('GPUs follow operator_capacity.view and admin follows operator_capacity.admin', () => {
  assert.deepEqual(
    accessFrom({ username: 'supervan', operator_capacity: { view: true, admin: false } }),
    {
      canPreview: false,
      previewing: false,
      signedIn: true,
      displayName: 'supervan',
      email: null,
      authentication: null,
      creditsEnabled: false,
      gpus: true,
      admin: false,
    },
  );
  assert.deepEqual(accessFrom({ username: 'mia', operator_capacity: { view: false } }), {
    canPreview: false,
    previewing: false,
    signedIn: true,
    displayName: 'mia',
    email: null,
    authentication: null,
    creditsEnabled: false,
    gpus: false,
    admin: false,
  });
});

test('machine principals (agent keys) never get GPU or admin navigation', () => {
  assert.deepEqual(
    accessFrom({
      username: 'superdan',
      machine: true,
      operator_capacity: { view: true, admin: true },
    }),
    {
      canPreview: false,
      previewing: false,
      signedIn: true,
      displayName: 'superdan',
      email: null,
      authentication: null,
      creditsEnabled: false,
      gpus: false,
      admin: false,
    },
  );
});

test('one cached read per page load; errors fall back to no access and are retried', async () => {
  resetAccess();
  let calls = 0;
  const failing = () => {
    calls++;
    return Promise.reject(new Error('offline'));
  };
  assert.deepEqual(await readAccess(failing), {
    canPreview: false,
    previewing: false,
    signedIn: false,
    displayName: null,
    email: null,
    authentication: null,
    creditsEnabled: false,
    gpus: false,
    admin: false,
  });
  // A failed read (offline, or 429 when the page asks several times) is not cached.
  await readAccess(failing);
  assert.equal(calls, 2);
  resetAccess();
  const ok = () =>
    Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ username: 'a', operator_capacity: { view: true } }),
    });
  assert.equal((await readAccess(ok)).gpus, true);
  // A successful read is shared by every later caller for the same account.
  let okCalls = 0;
  const counted = () => (okCalls++, ok());
  resetAccess();
  await readAccessFor('a', counted);
  await readAccessFor('a', counted);
  assert.equal(okCalls, 1);
  await readAccessFor('b', counted);
  assert.equal(okCalls, 2);
  resetAccess();
});

test('hosted sign-in needs an explicit flag and only returns to same-origin paths', () => {
  assert.equal(hostedSignInEnabled({}), false);
  assert.equal(hostedSignInEnabled({ hosted_sign_in: { enabled: true } }), true);
  assert.equal(
    hostedSignInUrl('/settings?tab=credits'),
    '/api/auth/workos/start?return_to=%2Fsettings%3Ftab%3Dcredits',
  );
  for (const bad of ['//evil.example', 'https://evil.example', 'javascript:x', null])
    assert.equal(hostedSignInUrl(bad), '/api/auth/workos/start?return_to=%2F');
});

test('people see a name or email, never an internal WorkOS user id', async () => {
  const { displayName, accountLabel } = await import('./account-access.js');
  assert.equal(displayName({ username: 'u_fb8fef7287e9420db65962e201d5ff54' }), null);
  assert.equal(
    displayName({ username: 'u_fb8fef7287e9420db65962e201d5ff54', email: 'mia@example.com' }),
    'mia@example.com',
  );
  assert.equal(displayName({ username: 'superdan' }), 'superdan');
  assert.equal(
    accountLabel('u_fb8fef7287e9420db65962e201d5ff54', null, 'My account'),
    'My account',
  );
  assert.equal(accountLabel('superdan', null, 'My account'), 'superdan');
});

test('hosted sign-in carries the sign-up/sign-in screen hint, and provider logout is https only (#395)', () => {
  assert.equal(
    hostedSignInUrl('/quick-chat', 'sign-up'),
    '/api/auth/workos/start?return_to=%2Fquick-chat&screen_hint=sign-up',
  );
  assert.equal(
    hostedSignInUrl('/', 'sign-in'),
    '/api/auth/workos/start?return_to=%2F&screen_hint=sign-in',
  );
  assert.equal(hostedSignInUrl('//evil', 'admin'), '/api/auth/workos/start?return_to=%2F');
  assert.equal(
    providerLogoutUrl({
      ok: true,
      logout_url: 'https://api.workos.com/user_management/sessions/logout?session_id=s',
    }),
    'https://api.workos.com/user_management/sessions/logout?session_id=s',
  );
  assert.equal(providerLogoutUrl({ ok: true }), null);
  assert.equal(providerLogoutUrl({ logout_url: 'javascript:alert(1)' }), null);
  assert.equal(providerLogoutUrl({ logout_url: 'http://example.com' }), null);
});

test('an admin can preview as a regular user only in the interface (owner, 2026-10-22)', () => {
  const store = new Map();
  globalThis.sessionStorage = {
    getItem: (k) => store.get(k) ?? null,
    setItem: (k, v) => store.set(k, v),
    removeItem: (k) => store.delete(k),
  };
  try {
    const me = { username: 'superdan', operator_capacity: { view: true, admin: true } };
    assert.equal(accessFrom(me).admin, true);
    assert.equal(accessFrom(me).canPreview, true);
    store.set('inkseq.previewAsUser', '1');
    const preview = accessFrom(me);
    assert.equal(preview.admin, false);
    assert.equal(preview.gpus, false);
    assert.equal(preview.previewing, true);
    assert.equal(preview.canPreview, true); // still able to switch back
    // A regular user with the flag set sees nothing different.
    assert.equal(accessFrom({ username: 'mia' }).previewing, false);
  } finally {
    delete globalThis.sessionStorage;
  }
});
