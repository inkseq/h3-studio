// One account menu for every page (#358): who you are on top, page actions, then
// Settings / Trash / Sign out. Pages add their own actions through `items`.
import React, { useEffect, useRef } from 'react';
import { LogIn, LogOut, Settings2, Trash2, UserRound } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { navigateApp } from './app-navigation.js';
import { resetAccess } from './account-access.js';
import { cloudController as cloud } from './cloud-controller.js';
import { useAccess, useAccountLabel } from './StudioHeader.jsx';
import './account-menu.css';

export function MenuItem({ icon: Icon, children, ...props }) {
  return (
    <button
      type="button"
      role="menuitem"
      className="account-menu-item"
      data-ui-surface="menu"
      {...props}
    >
      {Icon ? <Icon size={15} aria-hidden="true" /> : <i aria-hidden="true" />}
      <span>{children}</span>
    </button>
  );
}

export default function AccountMenu({ account, items, onSignIn }) {
  const { t } = useI18n(),
    who = useAccountLabel(account),
    access = useAccess(account),
    ref = useRef(null);
  // A <details> menu stays open on its own; close it on outside click, Escape or after an action.
  useEffect(() => {
    const close = () => ref.current?.removeAttribute('open');
    const outside = (event) => ref.current && !ref.current.contains(event.target) && close();
    const key = (event) => {
      if (event.key !== 'Escape' || !ref.current?.open) return;
      close();
      ref.current.querySelector('summary')?.focus();
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', key);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', key);
    };
  }, []);
  const closeAfter = (event) => {
    if (event.target.closest('button:not(:disabled), a[href]'))
      ref.current?.removeAttribute('open');
  };
  const email = access?.email && access.email !== who.label ? access.email : null;
  const signIn = () => (onSignIn ? onSignIn() : navigateApp('/quick-chat'));
  return (
    <details className="account-menu" ref={ref}>
      <summary
        className="avatar"
        aria-label={who.label || t('story.copy14')}
        title={who.label || t('story.copy14')}
      >
        {account ? who.initial : <UserRound size={17} aria-hidden="true" />}
      </summary>
      <div className="account-menu-panel" role="menu" onClick={closeAfter}>
        {account ? (
          <div className="account-menu-who">
            <span className="avatar" aria-hidden="true">
              {who.initial}
            </span>
            <div>
              <b>{who.label}</b>
              {email && <small>{email}</small>}
            </div>
          </div>
        ) : (
          <div className="account-menu-who signed-out">
            <small>{t('menu.signedOut')}</small>
            <button
              type="button"
              className="primary account-menu-signin"
              data-ui-surface="menu"
              onClick={signIn}
            >
              <LogIn size={15} aria-hidden="true" />
              {t('story.copy14')}
            </button>
          </div>
        )}
        {items && <div className="account-menu-group">{items}</div>}
        <div className="account-menu-group">
          <MenuItem icon={Settings2} onClick={() => navigateApp('/settings')}>
            {t('settings.title')}
          </MenuItem>
          <MenuItem icon={Trash2} onClick={() => navigateApp('/trash')}>
            {t('trash.title')}
          </MenuItem>
        </div>
        {account && (
          <div className="account-menu-group">
            <MenuItem
              icon={LogOut}
              onClick={() => {
                resetAccess();
                void cloud.logout().catch(() => {});
              }}
            >
              {t('account.copy81')}
            </MenuItem>
          </div>
        )}
      </div>
    </details>
  );
}
