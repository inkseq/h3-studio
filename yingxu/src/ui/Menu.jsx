// Shared floating menu (h3-studio#514 phase 2): Radix DropdownMenu does the hard parts once —
// portal above every panel, outside click and Escape, keyboard navigation, focus return —
// so pages never hand-roll a <details> menu or their own positioning again.
import React, { useRef, useState } from 'react';
import * as Dropdown from '@radix-ui/react-dropdown-menu';
import './menu.css';

export function Menu({
  trigger,
  label,
  disabled = false,
  align = 'end',
  open,
  onOpenChange,
  className = '',
  children,
}) {
  // A modal <dialog> makes the rest of the page inert, so a menu opened inside one renders
  // inside that dialog instead of at the end of <body>.
  const triggerRef = useRef(null),
    [container, setContainer] = useState(undefined);
  return (
    <Dropdown.Root
      modal={false}
      open={open}
      onOpenChange={(next) => {
        if (next) setContainer(triggerRef.current?.closest('dialog') || undefined);
        onOpenChange?.(next);
      }}
    >
      <Dropdown.Trigger ref={triggerRef} asChild disabled={disabled} aria-label={label}>
        {trigger}
      </Dropdown.Trigger>
      <Dropdown.Portal container={container}>
        <Dropdown.Content
          className={('ui-menu ' + className).trim()}
          align={align}
          sideOffset={6}
          collisionPadding={8}
          onClick={(event) => event.stopPropagation()}
          onPointerDown={(event) => event.stopPropagation()}
        >
          {children}
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}

// One row. `onSelect` runs the action; the menu closes by itself.
export function MenuItem({
  icon: Icon,
  onSelect,
  danger = false,
  disabled = false,
  title,
  children,
}) {
  return (
    <Dropdown.Item
      className={'ui-menu-item' + (danger ? ' danger' : '')}
      disabled={disabled}
      title={title}
      onSelect={onSelect}
    >
      {Icon ? <Icon size={15} aria-hidden="true" /> : null}
      <span>{children}</span>
    </Dropdown.Item>
  );
}

// A toggle row that stays open, so the choice applies to the next item picked.
export function MenuCheckboxItem({ checked, onCheckedChange, children }) {
  return (
    <Dropdown.CheckboxItem
      className="ui-menu-item ui-menu-check"
      checked={checked}
      onCheckedChange={onCheckedChange}
      onSelect={(event) => event.preventDefault()}
    >
      <input type="checkbox" checked={checked} readOnly tabIndex={-1} aria-hidden="true" />
      <span>{children}</span>
    </Dropdown.CheckboxItem>
  );
}

export function MenuGroup({ children }) {
  return <Dropdown.Group className="ui-menu-group">{children}</Dropdown.Group>;
}

export function MenuSeparator() {
  return <Dropdown.Separator className="ui-menu-separator" />;
}

// Non-interactive content at the top of a menu (who is signed in, a short note).
export function MenuLabel({ children, className = '' }) {
  return (
    <Dropdown.Label className={('ui-menu-label ' + className).trim()}>{children}</Dropdown.Label>
  );
}
