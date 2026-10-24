// Shared buttons (h3-studio#514). The look lives in ui/controls.css; pages only place them.
import React from 'react';
const VARIANTS = {
  primary: 'primary',
  secondary: '',
  ghost: 'ghost',
  danger: 'danger',
  'ghost-danger': 'ghost danger',
};

export function buttonClass({ variant = 'secondary', size = 'md', className = '' } = {}) {
  return [VARIANTS[variant] ?? '', size === 'sm' ? 'small' : '', className]
    .filter(Boolean)
    .join(' ');
}

export function Button({ variant, size, className, type = 'button', ...props }) {
  return (
    <button
      type={type}
      className={buttonClass({ variant, size, className }) || undefined}
      {...props}
    />
  );
}

// Icon-only: the label is required and becomes both the accessible name and the tooltip.
export function IconButton({ label, variant = 'ghost', className = '', ...props }) {
  return (
    <Button
      variant={variant}
      className={('icon-button ' + className).trim()}
      aria-label={label}
      title={label}
      {...props}
    />
  );
}
