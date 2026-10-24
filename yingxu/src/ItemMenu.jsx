import React, { useEffect, useRef, useState } from 'react';
import { MoreHorizontal } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
// One open menu owns only presentation. Business operations stay with callers.
export default function ItemMenu({ children, label, disabled = false }) {
  const { t } = useI18n(),
    [open, setOpen] = useState(false),
    [place, setPlace] = useState(null),
    root = useRef(null),
    trigger = useRef(null);
  useEffect(() => {
    if (!open) return;
    const outside = (e) => {
      if (!root.current?.contains(e.target)) setOpen(false);
    };
    const key = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopImmediatePropagation();
        setOpen(false);
        trigger.current?.focus();
      }
    };
    const scroll = () => setOpen(false);
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('keydown', key, true);
    window.addEventListener('scroll', scroll, true);
    return () => {
      document.removeEventListener('pointerdown', outside, true);
      document.removeEventListener('keydown', key, true);
      window.removeEventListener('scroll', scroll, true);
    };
  }, [open]);
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);
  return (
    <span className="item-menu" ref={root} onPointerDown={(e) => e.stopPropagation()}>
      <button
        type="button"
        ref={trigger}
        disabled={disabled}
        aria-label={label || t('trash.menu')}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={(e) => {
          e.stopPropagation();
          // Float above scrolling panels (a narrow sidebar clipped it), aligned to the
          // trigger's right edge but never past the window's left edge.
          const box = e.currentTarget.getBoundingClientRect();
          // Room on the left for the 180px menu: align right edges; else open rightwards.
          setPlace(
            box.right >= 188
              ? { top: box.bottom + 4, right: Math.max(8, window.innerWidth - box.right) }
              : { top: box.bottom + 4, left: Math.max(8, box.left) },
          );
          setOpen((v) => !v);
        }}
      >
        <MoreHorizontal size={16} aria-hidden="true" />
      </button>
      {open && (
        <span
          className="item-menu-popover"
          role="menu"
          style={
            place && {
              position: 'fixed',
              top: place.top,
              right: place.right ?? 'auto',
              left: place.left ?? 'auto',
              maxWidth: `min(280px, calc(100vw - ${(place.right ?? place.left) + 8}px))`,
            }
          }
          onClick={(e) => e.stopPropagation()}
        >
          {typeof children === 'function' ? children(() => setOpen(false)) : children}
        </span>
      )}
    </span>
  );
}
