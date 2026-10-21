import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { AlertTriangle, Check, Loader2 } from 'lucide-react';
import { store } from './store.js';
import { cloudController as cloud } from './cloud-controller.js';
import { useCloud } from './CloudStudio.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';

const DELAY_MS = 2000,
  RETRY_MS = 15000;
let status = { state: 'idle', error: '' };
const listeners = new Set();
function setStatus(next) {
  status = { ...status, ...next };
  listeners.forEach((fn) => fn());
}
const subscribe = (fn) => (listeners.add(fn), () => listeners.delete(fn));
const getStatus = () => status;

// Cloud stories save themselves a moment after the last edit (owner, 2026-10-21). The
// server's version check still refuses a stale save, so a conflict stops autosave until
// the person chooses which version to keep; it never overwrites the other edit.
export function useCloudAutosave() {
  const workspace = useSyncExternalStore(store.subscribe, () => store.getState().workspace),
    c = useCloud(),
    timer = useRef(null);
  const cloudStory = workspace.mode === 'cloud',
    blocked = !!c.conflict;
  useEffect(() => {
    clearTimeout(timer.current);
    if (!cloudStory || !workspace.dirty || blocked) return;
    const wait = status.state === 'error' ? RETRY_MS : DELAY_MS;
    timer.current = setTimeout(async () => {
      if (!store.getState().workspace.dirty || cloud.getState().busy) return;
      setStatus({ state: 'saving', error: '' });
      try {
        await cloud.save();
        setStatus({ state: 'saved', error: '' });
      } catch (error) {
        setStatus({ state: 'error', error: error?.message || '' });
      }
    }, wait);
    return () => clearTimeout(timer.current);
  }, [cloudStory, workspace.dirty, workspace.serverVersion, blocked, c.busy]);
  useEffect(() => {
    if (!cloudStory) return;
    const flush = () => {
      if (store.getState().workspace.dirty && !cloud.getState().busy && !cloud.getState().conflict)
        void cloud.save().catch(() => {});
    };
    const hidden = () => document.visibilityState === 'hidden' && flush();
    document.addEventListener('visibilitychange', hidden);
    window.addEventListener('pagehide', flush);
    return () => {
      document.removeEventListener('visibilitychange', hidden);
      window.removeEventListener('pagehide', flush);
    };
  }, [cloudStory]);
}

/** "Saving… / Saved / Couldn't save · Retry / Changed elsewhere" for a cloud story. */
export function CloudSaveStatus({ className = '', plain = false }) {
  const { t } = useI18n();
  const workspace = useSyncExternalStore(store.subscribe, () => store.getState().workspace),
    c = useCloud(),
    s = useSyncExternalStore(subscribe, getStatus, getStatus),
    [, force] = useState(0);
  if (workspace.mode !== 'cloud') return null;
  const retry = async () => {
    setStatus({ state: 'saving', error: '' });
    try {
      await cloud.save();
      setStatus({ state: 'saved', error: '' });
    } catch (error) {
      setStatus({ state: 'error', error: error?.message || '' });
    }
    force((n) => n + 1);
  };
  let icon = <Check size={13} />,
    label = t('autosave.saved'),
    tone = 'ok',
    action = null;
  if (c.conflict) {
    icon = <AlertTriangle size={13} />;
    label = t('autosave.conflict');
    tone = 'warn';
  } else if (s.state === 'error' && workspace.dirty) {
    icon = <AlertTriangle size={13} />;
    label = t('autosave.failed');
    tone = 'warn';
    action = retry;
  } else if (workspace.dirty || s.state === 'saving') {
    icon = <Loader2 size={13} className="spin" />;
    label = t('autosave.saving');
    tone = 'busy';
  }
  return action && !plain ? (
    <button
      type="button"
      className={'autosave-status ' + tone + ' ' + className}
      title={s.error || undefined}
      onClick={action}
    >
      {icon}
      {label}
    </button>
  ) : (
    <span className={'autosave-status ' + tone + ' ' + className} role="status">
      {icon}
      {label}
    </span>
  );
}
