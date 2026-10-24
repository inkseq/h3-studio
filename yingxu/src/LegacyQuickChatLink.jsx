import React, { useEffect, useState } from 'react';
import InkLoader from './InkLoader.jsx';
import StudioHeader from './StudioHeader.jsx';
import { createCloudClient } from './cloud-client.js';
import { navigateApp } from './app-navigation.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useCloud } from './CloudStudio.jsx';
import './story-open.css';

// The original Quick Create is gone (owner, 2026-10-24). Its old links (?session=, card,
// revision, submission) open the story the conversation was converted into (#481);
// anything else explains itself and offers a way on.
export default function LegacyQuickChatLink({ account, sessionId }) {
  const { t } = useI18n();
  const [failed, setFailed] = useState(false),
    { sessionReady } = useCloud();
  useEffect(() => {
    let live = true;
    setFailed(false);
    if (!sessionReady && !account) return undefined;
    if (!account || !sessionId) {
      navigateApp(account ? '/quick-chat' : '/', { replace: true });
      return undefined;
    }
    const client = createCloudClient();
    client.setAccount(account);
    client
      .request('/v1/quick-chat/sessions/' + encodeURIComponent(sessionId))
      .then((value) => {
        const story = value?.session?.migrated_project_id;
        if (!live) return;
        if (typeof story === 'string' && story)
          navigateApp('/quick-chat?story=' + encodeURIComponent(story), { replace: true });
        else setFailed(true);
      })
      .catch(() => live && setFailed(true));
    return () => {
      live = false;
    };
  }, [sessionReady, account, sessionId]);
  if (!failed) return <InkLoader label={t('legacyChat.opening')} />;
  return (
    <div className="home story-open-destination">
      <StudioHeader account={account} active="quick" />
      <main>
        <section className="story-open-notice" role="status">
          <h2>{t('legacyChat.title')}</h2>
          <p>{t('legacyChat.body')}</p>
          <div className="story-open-actions">
            <button type="button" onClick={() => navigateApp('/')}>
              {t('legacyChat.works')}
            </button>
            <button type="button" onClick={() => navigateApp('/quick-chat')}>
              {t('legacyChat.quickCreate')}
            </button>
          </div>
        </section>
      </main>
    </div>
  );
}
