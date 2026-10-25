// Shared modal dialog (h3-studio#514). One behaviour for every dialog: native modal, Escape
// closes, focus moves in and returns to the opener, and a cached page that is hidden closes
// its dialog. The look is dialogs.css (.qc-dialog); admin consoles reuse useModal only.
import React, { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { usePageActive } from '../AppRouteCache.jsx';
import { useI18n } from '../i18n/I18nProvider.jsx';

// A click on the backdrop targets the <dialog> itself, but so does a click on its own
// padding; only a point outside the box counts as "outside".
export function clickedOutside(event) {
  const box = event.currentTarget.getBoundingClientRect();
  return (
    event.target === event.currentTarget &&
    (event.clientX < box.left ||
      event.clientX > box.right ||
      event.clientY < box.top ||
      event.clientY > box.bottom)
  );
}

export function useModal({ focusFirst = false } = {}) {
  const active = usePageActive(),
    ref = useRef(),
    opener = useRef(globalThis.document?.activeElement);
  useEffect(() => {
    const dialog = ref.current;
    if (!active) {
      dialog.close();
      return;
    }
    dialog.showModal();
    // Start typing straight away: the first field, else the first action (not the close button).
    if (focusFirst)
      (
        dialog.querySelector('[autofocus],input,textarea,select') ||
        dialog.querySelector('button:not(.qc-dialog-head button)')
      )?.focus();
    return () => {
      dialog.close();
      queueMicrotask(() => {
        if (opener.current?.isConnected && !opener.current.closest('[inert]'))
          opener.current.focus();
      });
    };
  }, [active, focusFirst]);
  return ref;
}

export function Dialog({
  title,
  onClose,
  children,
  eyebrow = '',
  subtitle = '',
  wide = false,
  size = wide ? 'wide' : 'md',
  busy = false,
  // Owner review #232 (#35): every dialog closes on an outside click, like Escape.
  closeOnBackdrop = true,
  portal = false,
  className = '',
}) {
  const { t } = useI18n();
  const ref = useModal({ focusFirst: true });
  const close = () => {
    if (!busy) onClose();
  };
  // Trash moves can close the dialog that started them (UniversalTrash).
  useEffect(() => {
    const dialog = ref.current;
    dialog.addEventListener('inkseq-close-after-trash', close);
    return () => dialog.removeEventListener('inkseq-close-after-trash', close);
  });
  const node = (
    <dialog
      ref={ref}
      className={['qc-dialog', size !== 'md' && 'qc-dialog-' + size, className]
        .filter(Boolean)
        .join(' ')}
      aria-label={title}
      onCancel={(event) => {
        event.preventDefault();
        // Escape closes an open item menu first, not the dialog around it.
        if (!ref.current?.querySelector('.item-menu [aria-expanded="true"]')) close();
      }}
      onClick={(event) => {
        if (closeOnBackdrop && clickedOutside(event)) close();
      }}
    >
      <div className="qc-dialog-head">
        <div>
          {eyebrow && <span className="qc-eyebrow">{eyebrow}</span>}
          <h2>{title}</h2>
          {subtitle && <p className="qc-dialog-subtitle">{subtitle}</p>}
        </div>
        <button type="button" aria-label={t('workspace.copy88')} disabled={busy} onClick={close}>
          <X size={18} />
        </button>
      </div>
      {children}
    </dialog>
  );
  return portal ? createPortal(node, document.body) : node;
}

// Footer row: secondary actions first, the main action last on the right.
export function DialogActions({ children }) {
  return <div className="dialog-actions">{children}</div>;
}
