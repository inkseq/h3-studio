// Shared tooltip (h3-studio#514 phase 2): Radix Tooltip positions it outside clipped panels,
// opens on hover and keyboard focus, and closes on Escape. Inside a modal <dialog> it renders
// within that dialog so it stays above it.
import React, { useRef, useState } from 'react';
import * as Primitive from '@radix-ui/react-tooltip';
import './menu.css';

export function Tooltip({ content, side = 'top', children }) {
  const triggerRef = useRef(null),
    [container, setContainer] = useState(undefined);
  if (!content) return children;
  return (
    <Primitive.Provider delayDuration={150}>
      <Primitive.Root
        onOpenChange={(open) => {
          if (open) setContainer(triggerRef.current?.closest('dialog') || undefined);
        }}
      >
        <Primitive.Trigger ref={triggerRef} asChild>
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
