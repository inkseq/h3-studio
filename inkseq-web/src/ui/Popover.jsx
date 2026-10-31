// Shared popover (h3-studio#514 phase 2): a floating panel with mixed content (status,
// buttons, lists). Radix Popover handles portal, stacking, outside click, Escape and focus.
// Inside a modal <dialog> it renders within it. Plain action lists use ui/Menu instead.
import React, { useRef, useState } from 'react';
import * as Primitive from '@radix-ui/react-popover';
import './menu.css';

// Content can close its popover after work that is not a button click (a file was chosen) by
// dispatching this bubbling event from any node inside it.
export const POPOVER_CLOSE = 'ui-popover-close';
export const closePopover = (node) =>
  node?.dispatchEvent(new CustomEvent(POPOVER_CLOSE, { bubbles: true }));

export function Popover({ trigger, align = 'end', className = '', onOpenChange, children }) {
  const triggerRef = useRef(null),
    [container, setContainer] = useState(undefined),
    [open, setOpen] = useState(false);
  const change = (next) => {
    if (next) setContainer(triggerRef.current?.closest('dialog') || undefined);
    setOpen(next);
    onOpenChange?.(next);
  };
  const latest = useRef(change);
  latest.current = change;
  return (
    <Primitive.Root open={open} onOpenChange={change}>
      <Primitive.Trigger ref={triggerRef} asChild>
        {trigger}
      </Primitive.Trigger>
      <Primitive.Portal container={container}>
        <Primitive.Content
          className={('ui-popover ' + className).trim()}
          align={align}
          sideOffset={6}
          collisionPadding={8}
          // Any button inside closes the panel after its action, like a menu, unless it only
          // changes what the panel shows (data-keep-open).
          onClick={(event) => {
            const hit = event.target.closest('button:not(:disabled), a[href]');
            if (hit && !hit.closest('[data-keep-open]')) change(false);
          }}
          ref={(node) => {
            if (!node || node.dataset.closeListener) return;
            node.dataset.closeListener = '1';
            node.addEventListener(POPOVER_CLOSE, () => latest.current(false));
          }}
        >
          {children}
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
}
