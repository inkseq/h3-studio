// A category filter chip (h3-studio#514): icon, label and count, the open one tinted with the
// accent. `iconOnly` is the narrow rail form: a plain icon with a count badge when not zero.
// `alert` paints the count as a warning pill. The look lives in ui/filter-chip.css.
import React from 'react';
import { Button } from './Button.jsx';
import './filter-chip.css';

export function FilterChip({
  active = false,
  alert = false,
  iconOnly = false,
  icon: Icon,
  label,
  count,
  className = '',
  ...props
}) {
  return (
    <Button
      data-ui-surface="chip"
      className={[
        'filter-chip',
        active && 'on',
        alert && 'alert',
        iconOnly && 'icon-only',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      aria-pressed={active}
      {...props}
    >
      {Icon && <Icon size={13} aria-hidden="true" />}
      <span>{label}</span>
      {count !== undefined && <small className={count ? undefined : 'zero'}>{count}</small>}
    </Button>
  );
}
