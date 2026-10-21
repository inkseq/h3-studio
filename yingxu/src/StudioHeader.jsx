import React, { useEffect, useState } from 'react';
import { Cpu, Images, LayoutGrid, MessageSquare, Settings2, Trash2 } from 'lucide-react';
import { LanguageSwitcher, useI18n } from './i18n/I18nProvider.jsx';
import { navigateApp } from './app-navigation.js';
import { accountLabel, readAccess, resetAccess } from './account-access.js';
import { CreditsChip } from './CreditsUI.jsx';
import AccountMenu from './AccountMenu.jsx';
import InkMark from './InkMark.jsx';
import { dailyInkMark } from './ink-mark-daily.js';

// What the signed-in account may see (role, sign-in method, credits); re-read when it changes.
export function useAccess(account) {
  const [access, setAccess] = useState(null);
  useEffect(() => {
    let live = true;
    if (account) resetAccess();
    readAccess().then((value) => live && setAccess(value));
    return () => {
      live = false;
    };
  }, [account]);
  return access;
}

// Name shown for the signed-in account: display name or email, never an internal user id.
export function useAccountLabel(account) {
  const { t } = useI18n();
  const [access, setAccess] = useState(null);
  useEffect(() => {
    let live = true;
    if (!account) {
      setAccess(null);
      return undefined;
    }
    resetAccess();
    readAccess().then((value) => live && setAccess(value));
    return () => {
      live = false;
    };
  }, [account]);
  const label = account ? accountLabel(account, access, t('account.myAccount')) : null;
  return { label, initial: label ? label.trim().slice(0, 1).toLocaleUpperCase() : null };
}

export default function StudioHeader({
  account,
  active = 'works',
  accountMenu,
  menuItems,
  onSignIn,
}) {
  const { t } = useI18n();
  // GPUs only for accounts the backend lets view capacity (#304); regular users never run machines.
  const [access, setAccess] = useState(null);
  useEffect(() => {
    let live = true;
    readAccess().then((value) => live && setAccess(value));
    return () => {
      live = false;
    };
  }, []);
  const links = [
    // Quick Create (chat) is the main way in; Works holds the finished stories (owner, 2026-10-22).
    ['quick', '/quick-chat', 'nav.quickCreate', MessageSquare],
    ['works', '/', 'v2.works', LayoutGrid],
    ['library', '/library', 'library.title', Images],
    ...(access?.gpus || active === 'operator' ? [['operator', '/operator', 'v2.gpus', Cpu]] : []),
    ['settings', '/settings', 'settings.title', Settings2],
  ];
  // Port of prototype pages.jsx/AppHeader. Only navigation and account data differ.
  return (
    <header className="hbar">
      <a className="logo" href={account ? '/quick-chat' : '/'}>
        <InkMark size={26} variant={dailyInkMark()} />
        <span>{t('app.name')}</span>
      </a>
      <nav aria-label={t('nav.mobile')}>
        {links.map(([id, href, label, Icon]) => (
          <button
            key={id}
            type="button"
            className={active === id ? 'on' : ''}
            aria-current={active === id ? 'page' : undefined}
            onClick={() => navigateApp(href)}
          >
            <Icon className="nav-ico" size={20} aria-hidden="true" />
            <span>{t(label)}</span>
          </button>
        ))}
      </nav>
      <div className="spacer" />
      <CreditsChip account={account} />
      <LanguageSwitcher />
      <button
        type="button"
        className={'ghost hbar-trash' + (active === 'trash' ? ' on' : '')}
        onClick={() => navigateApp('/trash')}
        aria-label={t('trash.title')}
        title={t('trash.title')}
      >
        <Trash2 size={15} />
      </button>
      {accountMenu || <AccountMenu account={account} items={menuItems} onSignIn={onSignIn} />}
    </header>
  );
}
