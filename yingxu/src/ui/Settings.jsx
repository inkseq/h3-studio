// Shared settings layout (owner, 2026-10-26; h3-studio#514): one card per group, one row per
// setting with the label and help on the left and a compact control on the right. Settings,
// assistants and admin pages use this instead of their own row styles.
import React from 'react';
import './settings.css';

export function SettingsSection({ title, help, actions, children, className = '' }) {
  return (
    <section className={('ui-settings ' + className).trim()}>
      {(title || help) && (
        <header className="ui-settings-head">
          {title && <h3>{title}</h3>}
          {help && <p className="muted">{help}</p>}
        </header>
      )}
      {children}
      {actions && <footer className="ui-settings-actions">{actions}</footer>}
    </section>
  );
}

// One setting. Editable rows are labels for their control; rows whose inputs carry their own
// labels (several inputs, admin forms) or show a value only use as="div".
// - badge: a short state next to the name ("pending"); meta: a line under the help (last change)
// - unit: text after the control; extra: links or errors stacked under the control
// - changed: highlights an edited, unsaved row
export function SettingRow({
  label,
  help,
  meta,
  badge,
  unit,
  extra,
  changed = false,
  wide = false,
  as: Tag = 'label',
  children,
}) {
  return (
    <Tag className={'ui-setting-row' + (wide ? ' wide' : '') + (changed ? ' changed' : '')}>
      <span className="ui-setting-label">
        <span className="ui-setting-name">
          <b>{label}</b>
          {badge && <span className="ui-setting-badge">{badge}</span>}
        </span>
        {help && <small className="muted">{help}</small>}
        {meta && <small className="ui-setting-meta">{meta}</small>}
      </span>
      <span className="ui-setting-value">
        <span className="ui-setting-control">
          {children}
          {unit && <span className="ui-setting-unit">{unit}</span>}
        </span>
        {extra}
      </span>
    </Tag>
  );
}
