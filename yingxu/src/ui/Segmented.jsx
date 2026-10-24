// Shared segmented control / tabs (h3-studio#514). The look is .seg in ui/controls.css.
import React from 'react';

// options: [{ value, label, title? }]. With tabs, the buttons are role="tab" + aria-selected;
// otherwise a pressed-button group.
export function Segmented({
  options,
  value,
  onChange,
  label,
  tabs = false,
  className = '',
  disabled = false,
}) {
  return (
    <div
      className={('seg ' + className).trim()}
      role={tabs ? 'tablist' : 'group'}
      aria-label={label}
    >
      {options.map((option) => {
        const on = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            className={['keep-label', on && 'on', option.className].filter(Boolean).join(' ')}
            title={option.title}
            disabled={disabled || option.disabled}
            {...(tabs ? { role: 'tab', 'aria-selected': on } : { 'aria-pressed': on })}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
