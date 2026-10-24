import { useEffect, useRef } from 'react';
import { usePageActive } from './AppRouteCache.jsx';

// One floating line per message, over the page, so feedback never shifts the layout
// (owner, 2026-10-24). Messages mirror whatever state produced them: a notice stays
// while its source still holds it, and the person can dismiss it.
let notices = [],
  next = 0;
const listeners = new Set();
const emit = () => {
  notices = [...notices];
  for (const listener of listeners) listener();
};
export const subscribeNotices = (listener) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

/** Show a message; returns a function that removes it again. */
export function showNotice(text, { tone = 'info', action = null, onDismiss = null } = {}) {
  const id = ++next;
  notices.push({ id, text: String(text), tone, action, onDismiss });
  emit();
  return () => {
    const before = notices.length;
    notices = notices.filter((item) => item.id !== id);
    if (notices.length !== before) emit();
  };
}

/** Renders nothing in place; shows `text` floating while it is non-empty and the page is active. */
export function FloatingMessage({ text, tone = 'error', action = null, onDismiss = null }) {
  const active = usePageActive();
  const value = text === null || text === undefined || text === false ? '' : String(text);
  // Callbacks change every render; read the latest without re-showing the notice.
  const latest = useRef({ action, onDismiss });
  latest.current = { action, onDismiss };
  const actionLabel = action?.label || '',
    actionDisabled = !!action?.disabled;
  useEffect(() => {
    if (!active || !value) return undefined;
    return showNotice(value, {
      tone,
      action: actionLabel
        ? {
            label: actionLabel,
            disabled: actionDisabled,
            onClick: () => latest.current.action?.onClick?.(),
          }
        : null,
      onDismiss: () => latest.current.onDismiss?.(),
    });
  }, [active, value, tone, actionLabel, actionDisabled]);
  return null;
}

export const currentNotices = () => notices;
/** Remove every notice with this text and run their dismiss callbacks. */
export function dismissNotice(text) {
  const gone = notices.filter((other) => other.text === text);
  notices = notices.filter((other) => other.text !== text);
  emit();
  for (const other of gone) other.onDismiss?.();
}
