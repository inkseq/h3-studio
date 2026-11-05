import { setPreviewAsUser } from './preview-as-user.js';
import { Button } from './ui/Button.jsx';
// One account menu for every page (#358): who you are on top, page actions, then
// Settings / Trash / Sign out. Pages add their own actions through `items` (ui/Menu rows).
// The menu itself is the shared Radix menu (#514 phase 2): portal, outside click, Escape and
// keyboard handling come from one place.
import React, { useEffect, useState } from 'react';
import { Eye, LogIn, LogOut, Settings2, Trash2, UserRound } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { navigateApp } from './app-navigation.js';
import { resetAccess } from './account-access.js';
import { cloudController as cloud } from './cloud-controller.js';
import { useAccess, useAccountLabel } from './StudioHeader.jsx';
import { Menu, MenuGroup, MenuItem, MenuLabel, MenuSeparator } from './ui/Menu.jsx';
import './account-menu.css';

export { MenuItem };

// A page's header is replaced by the real one when the page's code arrives. A menu opened on the
// waiting header stays open: the new menu renders while the old one is still up and reads this.
// `carryOpen` is off for a menu in a hidden part of the page.
let menuOpen = false;

export default function AccountMenu({ account, items, onSignIn, carryOpen = true }) {
  const { t } = useI18n(),
    who = useAccountLabel(account),
    access = useAccess(account),
    [open, setOpen] = useState(() => carryOpen && menuOpen);
  useEffect(() => {
    if (!open) return undefined;
    menuOpen = true;
    return () => {
      menuOpen = false;
    };
  }, [open]);
  // Set here as well as in the effect: an item that changes page closes the menu in the same
  // event, and the next page's header must not read it as still open.
  const changeOpen = (next) => {
    menuOpen = next;
    setOpen(next);
  };
  const email = access?.email && access.email !== who.label ? access.email : null;
  const signIn = () => (onSignIn ? onSignIn() : navigateApp('/'));
  return (
    <Menu
      className="account-menu-panel"
      label={who.label || t('story.copy14')}
      open={open}
      onOpenChange={changeOpen}
      trigger={
        <Button className="avatar account-menu-trigger" title={who.label || t('story.copy14')}>
          {account ? who.initial : <UserRound size={17} aria-hidden="true" />}
        </Button>
      }
    >
      {account ? (
        <MenuLabel className="account-menu-who">
          <span className="avatar" aria-hidden="true">
            {who.initial}
          </span>
          <div>
            <b>{who.label}</b>
            {email && <small>{email}</small>}
          </div>
        </MenuLabel>
      ) : (
        <>
          <MenuLabel className="account-menu-who signed-out">
            <small>{t('menu.signedOut')}</small>
          </MenuLabel>
          <MenuItem icon={LogIn} onSelect={signIn}>
            {t('story.copy14')}
          </MenuItem>
        </>
      )}
      {items && (
        <>
          <MenuSeparator />
          <MenuGroup>{items}</MenuGroup>
        </>
      )}
      <MenuSeparator />
      <MenuGroup>
        <MenuItem icon={Settings2} onSelect={() => navigateApp('/settings')}>
          {t('settings.title')}
        </MenuItem>
        <MenuItem icon={Trash2} onSelect={() => navigateApp('/trash')}>
          {t('trash.title')}
        </MenuItem>
        {access?.canPreview && (
          <MenuItem icon={Eye} onSelect={() => setPreviewAsUser(!access.previewing)}>
            {t(access.previewing ? 'menu.previewExit' : 'menu.previewAsUser')}
          </MenuItem>
        )}
      </MenuGroup>
      {account && (
        <>
          <MenuSeparator />
          <MenuItem
            icon={LogOut}
            onSelect={() => {
              resetAccess();
              void cloud.logout().catch(() => {});
            }}
          >
            {t('account.copy81')}
          </MenuItem>
        </>
      )}
    </Menu>
  );
}
