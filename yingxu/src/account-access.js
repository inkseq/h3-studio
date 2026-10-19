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
export function hostedSignInUrl(returnTo = '/') {
  const path = typeof returnTo === 'string' && /^\/(?!\/)/.test(returnTo) ? returnTo : '/';
  return '/api/auth/workos/start?return_to=' + encodeURIComponent(path);
}
