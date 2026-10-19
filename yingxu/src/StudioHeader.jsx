import React, { useEffect, useState } from 'react';
import { Cpu, Images, LayoutGrid, MessageSquare, Settings2, Trash2, UserRound } from 'lucide-react';
import { LanguageSwitcher, useI18n } from './i18n/I18nProvider.jsx';
import { navigateApp } from './app-navigation.js';
import { readAccess } from './account-access.js';
import { CreditsChip } from './CreditsUI.jsx';

export default function StudioHeader({ account, active = 'works', accountMenu }) {
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
    ['works', '/', 'v2.works', LayoutGrid],
    ['quick', '/quick-chat', 'nav.quickCreate', MessageSquare],
    ['library', '/library', 'library.title', Images],
    ...(access?.gpus || active === 'operator' ? [['operator', '/operator', 'v2.gpus', Cpu]] : []),
    ['settings', '/settings', 'settings.title', Settings2],
  ];
  // Port of prototype pages.jsx/AppHeader. Only navigation and account data differ.
  return (
    <header className="hbar">
      <a className="logo" href="/">
        {t('app.name')}
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
      {accountMenu || (
        <button
          type="button"
          className="avatar"
          onClick={() => navigateApp(account ? '/settings' : '/quick-chat')}
          aria-label={account || t('story.copy14')}
          title={account || t('story.copy14')}
        >
          {account ? (
            account.slice(0, 1).toLocaleUpperCase()
          ) : (
            <UserRound size={17} aria-hidden="true" />
          )}
        </button>
      )}
    </header>
  );
}
