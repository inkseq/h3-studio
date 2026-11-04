import React, { useEffect } from 'react';
import StudioHeader from './StudioHeader.jsx';
import { navigateApp } from './app-navigation.js';
import { useCloud } from './CloudStudio.jsx';

const StoryChat = React.lazy(() => import('./StoryChat.jsx'));

// Quick Create's front door (#422): the chat story builder. Signed-out visitors sign in on
// the home page; when the server has the chat switched off, the chat itself says so. The
// original direct Quick Create was removed (owner, 2026-10-24).
export default function QuickCreateEntry({ account, story }) {
  const { sessionReady } = useCloud();
  // Only once the sign-in check has finished; a signed-in person must not be bounced.
  useEffect(() => {
    if (sessionReady && !account) navigateApp('/', { replace: true });
  }, [sessionReady, account]);
  const frame = (
    <div className="home sc-shell">
      <StudioHeader account={account} active="quick" />
    </div>
  );
  if (!account) return frame;
  return (
    <React.Suspense fallback={frame}>
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
