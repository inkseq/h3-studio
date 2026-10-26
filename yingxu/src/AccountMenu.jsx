import { setPreviewAsUser } from './preview-as-user.js';
// One account menu for every page (#358): who you are on top, page actions, then
// Settings / Trash / Sign out. Pages add their own actions through `items` (ui/Menu rows).
// The menu itself is the shared Radix menu (#514 phase 2): portal, outside click, Escape and
// keyboard handling come from one place.
import React from 'react';
import { Eye, LogIn, LogOut, Settings2, Trash2, UserRound } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { navigateApp } from './app-navigation.js';
import { resetAccess } from './account-access.js';
import { cloudController as cloud } from './cloud-controller.js';
import { useAccess, useAccountLabel } from './StudioHeader.jsx';
import { Menu, MenuGroup, MenuItem, MenuLabel, MenuSeparator } from './ui/Menu.jsx';
import './account-menu.css';

export { MenuItem };

export default function AccountMenu({ account, items, onSignIn }) {
  const { t } = useI18n(),
    who = useAccountLabel(account),
    access = useAccess(account);
  const email = access?.email && access.email !== who.label ? access.email : null;
  const signIn = () => (onSignIn ? onSignIn() : navigateApp('/'));
  return (
    <Menu
      className="account-menu-panel"
      label={who.label || t('story.copy14')}
      trigger={
        <button
          type="button"
          className="avatar account-menu-trigger"
          title={who.label || t('story.copy14')}
        >
          {account ? who.initial : <UserRound size={17} aria-hidden="true" />}
        </button>
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
