// Shared notes and empty states (h3-studio#514). The look lives in ui/feedback.css.
import React from 'react';

// A short line of supporting text. Tones other than muted read as a soft callout.
export function Note({ tone = 'muted', as: Tag = 'p', className = '', ...props }) {
  const classes = ['ui-note', tone !== 'muted' && 'ui-note-' + tone, className].filter(Boolean);
  return <Tag className={classes.join(' ')} {...props} />;
}

// Nothing to show yet: an optional icon, a title, a line of help and an optional action.
export function EmptyState({
  icon = null,
  title,
  children,
  action = null,
  compact = false,
  className = '',
  ...props
}) {
  const classes = ['ui-empty', compact && 'ui-empty-compact', className].filter(Boolean);
  return (
    <div className={classes.join(' ')} {...props}>
      {icon && <span className="ui-empty-icon">{icon}</span>}
      {title && <strong>{title}</strong>}
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

// Small status label: tone is one of cloud, local (or any .badge modifier).
export function Badge({ tone = '', className = '', ...props }) {
  return <span className={['badge', tone, className].filter(Boolean).join(' ')} {...props} />;
}

// Tag for a card kind or keyword: kind is character, location or prop.
export function Chip({ kind = '', className = '', ...props }) {
  return <span className={['chip', kind, className].filter(Boolean).join(' ')} {...props} />;
}
