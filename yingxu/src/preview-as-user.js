// "View as a regular user" for admins (owner, 2026-10-22). Only the interface changes: admin
// menus, GPU pages and dollar settings are hidden in this tab. The server still treats the
// account as an admin, so this is a preview, not a permission change.
const KEY = 'inkseq.previewAsUser';

export function previewingAsUser() {
  try {
    return sessionStorage.getItem(KEY) === '1';
  } catch {
    return false;
  }
}

export function setPreviewAsUser(on) {
  try {
    if (on) sessionStorage.setItem(KEY, '1');
    else sessionStorage.removeItem(KEY);
  } catch {}
  window.location.reload();
}

/** The /api/auth/me record as a regular user would get it, while previewing. */
export function asPreviewed(me) {
  if (!me || !previewingAsUser()) return me;
  return {
    ...me,
    operator_capacity: { ...(me.operator_capacity || {}), view: false, admin: false },
  };
}
