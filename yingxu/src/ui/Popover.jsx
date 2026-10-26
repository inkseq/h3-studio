// Shared popover (h3-studio#514 phase 2): a floating panel with mixed content (status,
// buttons, lists). Radix Popover handles portal, stacking, outside click, Escape and focus.
// Inside a modal <dialog> it renders within it. Plain action lists use ui/Menu instead.
import React, { useRef, useState } from 'react';
import * as Primitive from '@radix-ui/react-popover';
import './menu.css';

export function Popover({ trigger, align = 'end', className = '', children }) {
  const triggerRef = useRef(null),
    [container, setContainer] = useState(undefined),
    [open, setOpen] = useState(false);
  return (
    <Primitive.Root
      open={open}
      onOpenChange={(next) => {
        if (next) setContainer(triggerRef.current?.closest('dialog') || undefined);
        setOpen(next);
      }}
    >
      <Primitive.Trigger ref={triggerRef} asChild>
        {trigger}
      </Primitive.Trigger>
      <Primitive.Portal container={container}>
        <Primitive.Content
          className={('ui-popover ' + className).trim()}
          align={align}
          sideOffset={6}
          collisionPadding={8}
          // Any button inside closes the panel after its action, like a menu.
          onClick={(event) => {
            if (event.target.closest('button:not(:disabled), a[href]')) setOpen(false);
          }}
        >
          {children}
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
}
