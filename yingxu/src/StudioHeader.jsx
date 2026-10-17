import React from 'react';
import {Trash2} from 'lucide-react';
import {LanguageSwitcher,useI18n} from './i18n/I18nProvider.jsx';

export default function StudioHeader({account,active='works',accountMenu}){
  const {t}=useI18n();
  const links=[['works','/','v2.works'],['quick','/quick-chat','nav.quickCreate'],['library','/library','library.title'],['operator','/operator','v2.gpus'],['settings','/settings','settings.title']];
  return <header className="v2-header">
    <a className="v2-brand" href="/"><span aria-hidden="true"/>{t('app.name')}</a>
    <nav className="v2-global-nav" aria-label={t('nav.mobile')}>{links.map(([id,href,label])=><a key={id} href={href} aria-current={active===id?'page':undefined}>{t(label)}</a>)}</nav>
    <div className="v2-header-actions"><LanguageSwitcher/><a className="v2-trash-link" href="/trash" aria-label={t('trash.title')} title={t('trash.title')}><Trash2 size={16}/></a>{accountMenu||<a className="v2-avatar" href="/settings" aria-label={account||t('settings.title')} title={account||t('settings.title')}>{account?account.slice(0,1).toLocaleUpperCase():'·'}</a>}</div>
  </header>;
}
