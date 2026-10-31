import React, { useEffect } from 'react';
import InkLoader from './InkLoader.jsx';
import { navigateApp } from './app-navigation.js';
import { useCloud } from './CloudStudio.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';

const StoryChat = React.lazy(() => import('./StoryChat.jsx'));

// Quick Create's front door (#422): the chat story builder. Signed-out visitors sign in on
// the home page; when the server has the chat switched off, the chat itself says so. The
// original direct Quick Create was removed (owner, 2026-10-24).
export default function QuickCreateEntry({ account, story }) {
  const { t } = useI18n(),
    { sessionReady } = useCloud();
  // Only once the sign-in check has finished; a signed-in person must not be bounced.
  useEffect(() => {
    if (sessionReady && !account) navigateApp('/', { replace: true });
  }, [sessionReady, account]);
  if (!account) return <InkLoader label={t('loading.quickCreate')} />;
  return (
    <React.Suspense fallback={<InkLoader label={t('loading.quickCreate')} />}>
      {/* One stable instance: the address gains the story id after the first message and
          must not remount the chat mid-reply. */}
      <StoryChat
        key="live-story-chat"
        account={account}
        mode="live"
        projectId={story && story !== 'new' ? story : null}
      />
    </React.Suspense>
  );
}
