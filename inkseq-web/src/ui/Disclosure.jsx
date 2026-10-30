// Inline show/hide section (h3-studio#514): a native <details> with its <summary>. Floating
// panels use Popover and menus use Menu instead; pages style a disclosure by its className.
import React from 'react';

export function Disclosure({ summary, summaryClassName, children, ...props }) {
  return (
    <details {...props}>
      <summary className={summaryClassName}>{summary}</summary>
      {children}
    </details>
  );
}
