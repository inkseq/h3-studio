// The app's main navigation (h3-studio#514): a segmented bar in the desktop header and a
// bottom tab bar on phones. The look lives in ui/app-nav.css; the header only places it.
import React from 'react';
import { Button } from './Button.jsx';
import './app-nav.css';

export function AppNav({ label, items, active, onNavigate }) {
  return (
    <nav className="app-nav" aria-label={label}>
      {items.map(({ id, href, label: itemLabel, icon: Icon }) => (
        <Button
          key={id}
          className={'app-nav-item' + (active === id ? ' on' : '')}
          aria-current={active === id ? 'page' : undefined}
          onClick={() => onNavigate(href)}
        >
          <Icon className="app-nav-icon" size={20} aria-hidden="true" />
          <span>{itemLabel}</span>
        </Button>
      ))}
    </nav>
  );
}
