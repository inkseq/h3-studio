import React from 'react';
import {Trash2} from 'lucide-react';
import {LanguageSwitcher,useI18n} from './i18n/I18nProvider.jsx';
import {navigateApp} from './app-navigation.js';

export default function StudioHeader({account,active='works',accountMenu}){
  const {t}=useI18n();
  const links=[['works','/','v2.works'],['quick','/quick-chat','nav.quickCreate'],['library','/library','library.title'],['operator','/operator','v2.gpus'],['settings','/settings','settings.title']];
  // Port of prototype pages.jsx/AppHeader. Only navigation and account data differ.
  return <header className="hbar">
    <a className="logo" href="/">{t('app.name')}</a>
    <nav aria-label={t('nav.mobile')}>{links.map(([id,href,label])=><button key={id} type="button" className={active===id?'on':''} aria-current={active===id?'page':undefined} onClick={()=>navigateApp(href)}>{t(label)}</button>)}</nav>
    <div className="spacer"/><LanguageSwitcher/>
    <button type="button" className={'ghost hbar-trash'+(active==='trash'?' on':'')} onClick={()=>navigateApp('/trash')} aria-label={t('trash.title')} title={t('trash.title')}><Trash2 size={15}/></button>
    {accountMenu||<button type="button" className="avatar" onClick={()=>navigateApp('/settings')} aria-label={account||t('settings.title')} title={account||t('settings.title')}>{account?account.slice(0,1).toLocaleUpperCase():'·'}</button>}
  </header>;
}
