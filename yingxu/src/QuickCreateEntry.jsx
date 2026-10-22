import React, { useEffect, useState } from 'react';
import InkLoader from './InkLoader.jsx';
import { createCloudClient } from './cloud-client.js';
import { useI18n } from './i18n/I18nProvider.jsx';

const StoryChat = React.lazy(() => import('./StoryChat.jsx'));

// Quick Create's front door (#422): the chat story builder when the server has it switched
// on and ready, otherwise the original direct Quick Create. ?mode=direct keeps the original.
let checked = null;
function storyChatReady(account) {
  if (!account) return Promise.resolve(false);
  if (!checked || checked.account !== account) {
    const client = createCloudClient();
    client.setAccount(account);
    checked = {
      account,
      ready: client
        .request('/v1/story-chats/config')
        .then((config) => !!(config.enabled && config.assistant_ready))
        .catch(() => false),
    };
  }
  return checked.ready;
}

export default function QuickCreateEntry({ account, story, direct }) {
  const { t } = useI18n();
  const [ready, setReady] = useState(null);
  useEffect(() => {
    let live = true;
    storyChatReady(account).then((value) => live && setReady(value));
    return () => {
      live = false;
    };
  }, [account]);
  // An explicit story link always opens the chat (it explains if it is switched off).
  if (!story && ready === null) return <InkLoader label={t('loading.quickCreate')} />;
  if (!story && !ready) return direct;
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
