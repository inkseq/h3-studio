// What the signed-in account may see in the shared header (h3-studio#304 / #328).
// The backend still enforces every route; this only hides navigation the account cannot use.
// One cached /api/auth/me read per page load; a failed or anonymous read shows nothing privileged.

let pending = null;

export function readAccess(fetcher = globalThis.fetch) {
  if (!pending) {
    pending = fetcher('/api/auth/me', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    })
      .then((response) => (response.ok ? response.json() : null))
      .then(accessFrom)
      .catch(() => accessFrom(null));
  }
  return pending;
}

export function accessFrom(me) {
  const operator = me?.operator_capacity || {};
  return {
    signedIn: Boolean(me && (me.username || me.account)),
    displayName: displayName(me),
    email: typeof me?.email === 'string' ? me.email : null,
    authentication: typeof me?.authentication === 'string' ? me.authentication : null,
    creditsEnabled: me?.credits?.enabled === true,
    gpus: !me?.machine && operator.view === true,
    admin: !me?.machine && operator.admin === true,
  };
}

// Tests and sign-out reset the cache.
export function resetAccess() {
  pending = null;
}

// WorkOS AuthKit sign-in (#305), enabled by /api/auth/config.
export function hostedSignInEnabled(auth) {
  return auth?.hosted_sign_in?.enabled === true;
}

// Same-origin relative path only; the backend checks it again.
// `hint` opens the hosted page on sign-up or sign-in (#395); omitted, WorkOS decides.
export function hostedSignInUrl(returnTo = '/', hint) {
  const path = typeof returnTo === 'string' && /^\/(?!\/)/.test(returnTo) ? returnTo : '/';
  const screen = hint === 'sign-up' || hint === 'sign-in' ? '&screen_hint=' + hint : '';
  return '/api/auth/workos/start?return_to=' + encodeURIComponent(path) + screen;
}

// After sign-out the server may return the hosted provider's logout page, which ends the
// WorkOS session too, so the next sign-in asks for an account again (#395). Only https.
export function providerLogoutUrl(value) {
  try {
    const url = new URL(value?.logout_url);
    return url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

// WorkOS users have an internal owner id (u_<hex>) as username; never show that to people.
const INTERNAL_ID = /^u_[0-9a-f]{16,}$/i;
export function displayName(me) {
  if (!me) return null;
  const name = me.display_name || me.name || me.email;
  if (typeof name === 'string' && name.trim()) return name.trim();
  const id = me.username || me.account;
  return typeof id === 'string' && id && !INTERNAL_ID.test(id) ? id : null;
}
export function accountLabel(account, access, fallback) {
  if (access?.displayName) return access.displayName;
  return account && !INTERNAL_ID.test(account) ? account : fallback;
}
