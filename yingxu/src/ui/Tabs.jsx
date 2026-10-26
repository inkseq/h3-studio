// Underlined tabs (h3-studio#514) for switching panels, e.g. Chat | Cards on a phone. Options
// are [{ value, label }]; `fill` gives every tab an equal share of the row. Look: ui/tabs.css.
import React from 'react';
import { Button } from './Button.jsx';
import './tabs.css';

export function Tabs({ options, value, onChange, label, fill = false, className = '' }) {
  return (
    <div
      className={['tabs-underline', fill && 'fill', className].filter(Boolean).join(' ')}
      role="tablist"
      aria-label={label}
    >
      {options.map((option) => (
        <Button
          key={option.value}
          data-ui-surface="tab"
          className="tab"
          role="tab"
          aria-selected={option.value === value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </Button>
      ))}
    </div>
  );
}
