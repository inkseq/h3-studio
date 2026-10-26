import { useI18n } from './i18n/I18nProvider.jsx';
import React, { useEffect, useRef, useState } from 'react';

export function useStoryboardPanelSize(key, initial, min, max) {
  const storageKey = 'sixnine-storyboard-panel:' + key,
    cleanup = useRef(() => {});
  const clamp = (value) => Math.max(min, Math.min(max, value));
  // Older builds wrote the default on mount. Treat that unchanged default as
  // automatic; retain a genuinely resized value in the existing storage key.
  const [saved, setSaved] = useState(() => {
    try {
      const raw = JSON.parse(localStorage.getItem(storageKey)),
        explicit = raw && typeof raw === 'object',
        value = explicit ? raw.size : Number(raw);
      return Number.isFinite(value) &&
        value >= min &&
        value <= max &&
        (explicit || value !== initial)
        ? value
        : null;
    } catch {
      return null;
    }
  });
  const size = saved ?? initial,
    setSize = (value) =>
      setSaved((old) => clamp(typeof value === 'function' ? value(old ?? initial) : value));
  useEffect(() => {
    try {
      saved === null
        ? localStorage.removeItem(storageKey)
        : localStorage.setItem(storageKey, JSON.stringify({ size: saved }));
    } catch {
      // Browser storage is optional (private mode, blocked site data).
    }
  }, [storageKey, saved]);
  useEffect(() => () => cleanup.current(), []);
  return {
    size,
    min,
    max,
    custom: saved !== null,
    set: setSize,
    reset: () => setSaved(null),
    adjust: (delta, from = size) => setSize(from + delta),
    start: (event, axis, sign, from = size) => {
      event.preventDefault();
      cleanup.current();
      const origin = axis === 'x' ? event.clientX : event.clientY,
        base = from;
      const move = (e) =>
        setSize(clamp(base + sign * ((axis === 'x' ? e.clientX : e.clientY) - origin)));
      const done = () => {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', done);
        window.removeEventListener('pointercancel', done);
        document.body.classList.remove('sb-resizing', 'sb-resizing-y');
        cleanup.current = () => {};
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', done);
      window.addEventListener('pointercancel', done);
      document.body.classList.add(axis === 'x' ? 'sb-resizing' : 'sb-resizing-y');
      cleanup.current = done;
    },
  };
}
export function StoryboardSplitter({
  panel,
  axis = 'x',
  sign = 1,
  className = '',
  label = null,
  current = panel.size,
  onDouble,
  title,
}) {
  const { t } = useI18n();
  return (
    <div
      className={'splitter axis-' + axis + ' ' + className}
      tabIndex={0}
      role="separator"
      aria-label={label || t('storyboard.resize')}
      aria-orientation={axis === 'x' ? 'vertical' : 'horizontal'}
      aria-valuemin={panel.min}
      aria-valuemax={panel.max}
      aria-valuenow={Math.round(current)}
      title={title || t('storyboard.copy13')}
      onPointerDown={(event) => panel.start(event, axis, sign, current)}
      onDoubleClick={onDouble || panel.reset}
      onKeyDown={(event) => {
        if (event.key === 'Home') {
          event.preventDefault();
          panel.reset();
        }
        if (
          (axis === 'x' ? ['ArrowLeft', 'ArrowRight'] : ['ArrowUp', 'ArrowDown']).includes(
            event.key,
          )
        ) {
          event.preventDefault();
          panel.adjust(
            (['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : -1) *
              sign *
              (event.shiftKey ? 40 : 10),
            current,
          );
        }
      }}
    >
      <i />
    </div>
  );
}
