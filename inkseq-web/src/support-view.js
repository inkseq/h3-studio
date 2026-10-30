// Read-only admin support view (h3-studio#552): one browser tab shows one user's account.
// The id lives in sessionStorage, so it never leaks into other tabs; the server binds it to
// the admin who started it, refuses every write and ends it after 30 minutes.
const KEY = 'inkseq.supportView.v1';

const storage = () => {
  try {
    return globalThis.sessionStorage || null;
  } catch {
    return null;
  }
};

export function currentSupportView(now = Date.now() / 1000) {
  try {
    const value = JSON.parse(storage()?.getItem(KEY) || 'null');
    if (!value || !/^[0-9a-f]{32}$/.test(value.id) || !(value.expires_at > now)) {
      if (value) storage()?.removeItem(KEY);
      return null;
    }
    return value;
  } catch {
    return null;
  }
}

export function rememberSupportView(view) {
  storage()?.setItem(
    KEY,
    JSON.stringify({
      id: view.id,
      owner: view.owner,
      display_name: view.display_name,
      expires_at: view.expires_at,
    }),
  );
}

export function forgetSupportView() {
  storage()?.removeItem(KEY);
}

/** End the view as the admin (no view header), then return to the users page. */
export async function endSupportView(fetcher = (url, options) => fetch(url, options)) {
  const view = currentSupportView();
  forgetSupportView();
  if (view)
    await fetcher(`/v1/admin/support-views/${view.id}/end`, {
      method: 'POST',
      credentials: 'same-origin',
    }).catch(() => {});
  globalThis.location?.assign('/admin?section=users');
}
