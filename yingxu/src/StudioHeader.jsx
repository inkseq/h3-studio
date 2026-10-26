import { setPreviewAsUser } from './preview-as-user.js';
import { Button } from './ui/Button.jsx';
import React, { useEffect, useState } from 'react';
import {
  Cpu,
  Images,
  LayoutGrid,
  MessageSquare,
  Settings2,
  Trash2,
  ShieldCheck,
} from 'lucide-react';
import ThemeToggle from './ThemeToggle.jsx';
import { LanguageSwitcher, useI18n } from './i18n/I18nProvider.jsx';
import { navigateApp } from './app-navigation.js';
import { currentSupportView, endSupportView } from './support-view.js';
import { accountLabel, readAccess, readAccessFor } from './account-access.js';
import { CreditsChip } from './CreditsUI.jsx';
import AccountMenu from './AccountMenu.jsx';
import InkMark from './InkMark.jsx';
import { dailyInkMark } from './ink-mark-daily.js';

// Reads the account's access once; if a signed-in read came back empty (busy server), asks once
// more shortly after instead of leaving "My account" in place of the name.
function loadAccess(account, apply) {
  readAccessFor(account).then((value) => {
    apply(value);
    if (account && !value.signedIn) setTimeout(() => readAccessFor(account).then(apply), 1500);
  });
}

// What the signed-in account may see (role, sign-in method, credits); re-read when it changes.
export function useAccess(account) {
  const [access, setAccess] = useState(null);
  useEffect(() => {
    let live = true;
    loadAccess(account, (value) => live && setAccess(value));
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
    loadAccess(account, (value) => live && setAccess(value));
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
    // One admin console (h3-studio#537), only for admins.
    ...((access?.admin || active === 'admin') && !currentSupportView()
      ? [['admin', '/admin', 'adminHome.title', ShieldCheck]]
      : []),
  ];
  // Port of prototype pages.jsx/AppHeader. Only navigation and account data differ.
  const supportView = currentSupportView();
  return (
    <>
      {supportView && (
        <div className="preview-as-user-bar support-view-bar" role="status">
          {t('menu.supportView', {
            name: supportView.display_name || supportView.owner,
            minutes: Math.max(1, Math.round((supportView.expires_at - Date.now() / 1000) / 60)),
          })}
          <Button size="sm" onClick={() => void endSupportView()}>
            {t('menu.supportViewEnd')}
          </Button>
        </div>
      )}
      {access?.previewing && (
        <div className="preview-as-user-bar" role="status">
          {t('menu.previewing')}
          <Button size="sm" onClick={() => setPreviewAsUser(false)}>
            {t('menu.previewExit')}
          </Button>
        </div>
      )}
      <header className="hbar">
        <a className="logo" href={account ? '/quick-chat' : '/'}>
          <InkMark size={26} variant={dailyInkMark()} />
          <span>{t('app.name')}</span>
        </a>
        <nav aria-label={t('nav.mobile')}>
          {links.map(([id, href, label, Icon]) => (
            <Button
              key={id}
              className={active === id ? 'on' : ''}
              aria-current={active === id ? 'page' : undefined}
              onClick={() => navigateApp(href)}
            >
              <Icon className="nav-ico" size={20} aria-hidden="true" />
              <span>{t(label)}</span>
            </Button>
          ))}
        </nav>
        <div className="spacer" />
        <CreditsChip account={account} />
        <ThemeToggle />
        <LanguageSwitcher />
        <Button
          className={'ghost hbar-trash' + (active === 'trash' ? ' on' : '')}
          onClick={() => navigateApp('/trash')}
          aria-label={t('trash.title')}
          title={t('trash.title')}
        >
          <Trash2 size={15} />
        </Button>
        {accountMenu || <AccountMenu account={account} items={menuItems} onSignIn={onSignIn} />}
      </header>
    </>
  );
}
