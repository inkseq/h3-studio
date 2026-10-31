// A selectable row in a list (h3-studio#514): a title and an optional second line, the current
// one tinted. For lists such as "my stories"; section navigation uses SideNav instead.
import React from 'react';
import { Button } from './Button.jsx';
import './list-button.css';

export function ListButton({ current = false, label, meta, className = '', ...props }) {
  return (
    <Button
      data-ui-surface="row"
      className={('list-button ' + className).trim()}
      aria-current={current ? 'page' : undefined}
      {...props}
    >
      <span>{label}</span>
      {meta && <small>{meta}</small>}
    </Button>
  );
}
