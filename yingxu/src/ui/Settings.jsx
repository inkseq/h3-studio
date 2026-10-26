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

// A read-only value row uses as="div"; editable rows are labels for their control.
export function SettingRow({ label, help, meta, children, wide = false, as: Tag = 'label' }) {
  return (
    <Tag className={'ui-setting-row' + (wide ? ' wide' : '')}>
      <span className="ui-setting-label">
        <b>{label}</b>
        {help && <small className="muted">{help}</small>}
        {meta}
      </span>
      <span className="ui-setting-control">{children}</span>
    </Tag>
  );
}
