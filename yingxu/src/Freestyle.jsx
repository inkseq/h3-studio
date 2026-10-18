import React, { useEffect } from 'react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useAppLocation, usePageActive } from './AppRouteCache.jsx';
import { navigateApp } from './app-navigation.js';
import { freestyleWorkspaceHref } from './workspace-navigation.js';
import { store } from './store.js';
import StudioHeader from './StudioHeader.jsx';

// Old Agent links keep their public destination and the same shared draft.
// The v2 workspace owns editing, preflight, submission and result recovery.
export default function Freestyle() {
  const { t } = useI18n(),
    location = useAppLocation(),
    active = usePageActive();
  const state = store.getState(),
    href = freestyleWorkspaceHref(location.search, state);
  useEffect(() => {
    if (active && href) navigateApp(href, { replace: true, restore: false });
  }, [active, href]);
  return (
    <div className="home">
      <StudioHeader />
      <main className="empty-state">
        <p role={href ? 'status' : 'alert'}>
          {t(href ? 'quickView.openingWorkspace' : 'quickView.invalidWorkspaceLink')}
        </p>
        {!href && <a href="/">{t('quickView.backToWorks')}</a>}
      </main>
    </div>
  );
}
