// Shared form field (h3-studio#514): a label above its control, optional help and error.
// Controls themselves (input, select, textarea) are styled globally in ui/controls.css.
import React from 'react';

export function Field({ label, help, error, children, className = '' }) {
  return (
    <label className={('ui-field ' + className).trim()}>
      {label && <span className="ui-field-label">{label}</span>}
      {children}
      {help && !error && <small className="ui-field-help">{help}</small>}
      {error && (
        <small className="ui-field-error" role="alert">
          {error}
        </small>
      )}
    </label>
  );
}

export function Checkbox({ label, className = '', ...props }) {
  return (
    <label className={('ui-check ' + className).trim()}>
      <input type="checkbox" {...props} />
      <span>{label}</span>
    </label>
  );
}

export function Spinner({ size = 14, label }) {
  return (
    <span
      className="ui-spinner"
      style={{ width: size, height: size }}
      role={label ? 'status' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : 'true'}
    />
  );
}
