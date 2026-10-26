// Shared tooltip (h3-studio#514 phase 2): Radix Tooltip positions it outside clipped panels,
// opens on hover and keyboard focus, and closes on Escape. Inside a modal <dialog> it renders
// within that dialog so it stays above it.
import React, { useRef, useState } from 'react';
import * as Primitive from '@radix-ui/react-tooltip';
import './menu.css';

// With `tap`, a tap or click also toggles it, so help icons work on phones (no hover there).
export function Tooltip({ content, side = 'top', tap = false, children }) {
  const triggerRef = useRef(null),
    [container, setContainer] = useState(undefined),
    [open, setOpen] = useState(false);
  if (!content) return children;
  const change = (next) => {
    if (next) setContainer(triggerRef.current?.closest('dialog') || undefined);
    setOpen(next);
  };
  return (
    <Primitive.Provider delayDuration={150}>
      <Primitive.Root open={open} onOpenChange={change}>
        <Primitive.Trigger
          ref={triggerRef}
          asChild
          onClick={tap ? () => change(!open) : undefined}
          onPointerDown={tap ? (event) => event.preventDefault() : undefined}
        >
          {children}
        </Primitive.Trigger>
        <Primitive.Portal container={container}>
          <Primitive.Content className="ui-tooltip" side={side} sideOffset={6} collisionPadding={8}>
            {content}
          </Primitive.Content>
        </Primitive.Portal>
      </Primitive.Root>
    </Primitive.Provider>
  );
}
