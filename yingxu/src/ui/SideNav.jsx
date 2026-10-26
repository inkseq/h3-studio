// Vertical section navigation beside a page (h3-studio#514): settings sections, library
// filters, admin areas. On phones it becomes a horizontal row of pills. The look lives in
// ui/side-nav.css; pages only place the list.
import React from 'react';
import { Button } from './Button.jsx';
import './side-nav.css';

export function SideNav({ as: Tag = 'nav', className = '', children, ...props }) {
  return (
    <Tag className={('side-nav ' + className).trim()} {...props}>
      {children}
    </Tag>
  );
}

// One row: icon, label, optional count. `alert` colours the count (e.g. items needing a look).
export function SideNavItem({
  active = false,
  icon: Icon,
  count,
  countTitle,
  alert = false,
  className = '',
  children,
  ...props
}) {
  return (
    <Button
      data-ui-surface="nav"
      className={['side-nav-item', active && 'on', alert && 'alert', className]
        .filter(Boolean)
        .join(' ')}
      {...props}
    >
      {Icon && <Icon size={16} aria-hidden="true" />}
      <span>{children}</span>
      {count !== undefined && <small title={countTitle}>{count}</small>}
    </Button>
  );
}
