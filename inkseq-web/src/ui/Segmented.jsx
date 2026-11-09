// Shared segmented control / tabs (h3-studio#514). The look is .seg in ui/controls.css.
import React, { useRef } from 'react';

// The option a tab key moves to (WAI-ARIA APG tabs, automatic activation): ←/→ wrap around,
// Home/End go to the ends, disabled options are skipped. Returns -1 for other keys.
export function tabKeyTarget(options, from, key, disabled = false) {
  const n = options.length,
    open = (i) => !disabled && !options[i]?.disabled;
  if (!n || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(key)) return -1;
  const step = key === 'ArrowLeft' || key === 'End' ? -1 : 1;
  let at = key === 'Home' ? 0 : key === 'End' ? n - 1 : from + step;
  for (let tries = 0; tries < n; tries++, at += step) {
    const i = (at + n) % n;
    if (open(i)) return i;
  }
  return -1;
}

// options: [{ value, label, title? }]. With tabs, the buttons are role="tab" + aria-selected,
// one Tab stop with the arrows moving and selecting (roving tabindex); otherwise a
// pressed-button group.
export function Segmented({
  options,
  value,
  onChange,
  label,
  tabs = false,
  className = '',
  disabled = false,
}) {
  const box = useRef(null);
  const selected = options.findIndex((option) => option.value === value);
  // The tab in the Tab order: the selected one, else the first that can be chosen.
  const stop = selected >= 0 ? selected : tabKeyTarget(options, -1, 'ArrowRight', disabled);
  const keys = (event) => {
    if (!tabs || event.altKey || event.ctrlKey || event.metaKey) return;
    const from = Number(event.target.dataset?.index);
    const to = tabKeyTarget(options, Number.isInteger(from) ? from : stop, event.key, disabled);
    if (to < 0) return;
    event.preventDefault();
    box.current?.querySelectorAll('[role="tab"]')[to]?.focus();
    if (options[to].value !== value) onChange(options[to].value);
  };
  return (
    <div
      ref={box}
      className={('seg ' + className).trim()}
      role={tabs ? 'tablist' : 'group'}
      aria-label={label}
      onKeyDown={tabs ? keys : undefined}
    >
      {options.map((option, index) => {
        const on = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            data-index={index}
            className={['keep-label', on && 'on', option.className].filter(Boolean).join(' ')}
            title={option.title}
            disabled={disabled || option.disabled}
            {...(tabs
              ? { role: 'tab', 'aria-selected': on, tabIndex: index === stop ? 0 : -1 }
              : { 'aria-pressed': on })}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
