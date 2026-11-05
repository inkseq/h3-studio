import './app-styles.js';
import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import { QueryClientProvider } from '@tanstack/react-query';
import { installAccountScope, queryClient } from './data/query.js';
import { cloudController } from './cloud-controller.js';
import videoCacheWorkerURL from './video-cache-worker.js?worker&url';
import { installVideoCache } from './video-cache-client.js';
import { startSkin } from './skins.js';
import { installMonitoring, reactErrorHandler } from './monitoring.js';
import { installAnalytics } from './analytics.js';
import { installVersionCheck } from './version-check.js';
import { showNotice } from './floating-notice.js';
import { installChunkReload } from './chunk-reload.js';

const monitored = installMonitoring();
installAnalytics();
startSkin();
installAccountScope(cloudController);
installChunkReload();
createRoot(
  document.getElementById('root'),
  // React 19 reports render crashes here; send them to Sentry on the deployed sites.
  monitored
    ? { onUncaughtError: reactErrorHandler(), onCaughtError: reactErrorHandler() }
    : undefined,
).render(
  <QueryClientProvider client={queryClient}>
    <App />
  </QueryClientProvider>,
);

if (import.meta.env.PROD) {
  installVideoCache(videoCacheWorkerURL);
  installVersionCheck({ notify: showNotice });
}
