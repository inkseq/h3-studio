import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import videoCacheWorkerURL from './video-cache-worker.js?worker&url';
import { installVideoCache } from './video-cache-client.js';
import './demo-v2.css';
import './v2/page-bridge.css';
import './dialogs.css';
import './skins.css';
import { applySkin, readSkin } from './skins.js';
import { installPopoverDismiss } from './popover-dismiss.js';

applySkin(readSkin());
installPopoverDismiss();
createRoot(document.getElementById('root')).render(<App />);

if (import.meta.env.PROD) installVideoCache(videoCacheWorkerURL);
