// Pill chip (h3-studio#514): a reply suggestion (tone="accent") or a toggle such as "use this
// reference" (pressed). `media` puts a small thumbnail first. The look lives in ui/chip.css.
import React from 'react';
import { Button } from './Button.jsx';
import './chip.css';

export function Chip({ tone = 'neutral', pressed, media, className = '', children, ...props }) {
  return (
    <Button
      data-ui-surface="chip"
      className={['chip', tone === 'accent' && 'accent', media && 'with-media', className]
        .filter(Boolean)
        .join(' ')}
      aria-pressed={pressed}
      {...props}
    >
      {media}
      {children}
    </Button>
  );
}
