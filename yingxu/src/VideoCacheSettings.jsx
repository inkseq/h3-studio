import React, { useEffect, useState } from 'react';
import { HardDrive, Trash2 } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { videoCacheStatus, clearVideoCache } from './video-cache-client.js';
export default function VideoCacheSettings({ account }) {
  const { t } = useI18n(),
    [state, setState] = useState(null),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    setState(null);
    videoCacheStatus(account)
      .then((value) => {
        if (active) setState(value);
      })
      .catch(() => {
        if (active) setState(false);
      });
    return () => {
      active = false;
    };
  }, [account]);
  const clear = async () => {
    setBusy(true);
    try {
      await clearVideoCache();
      setState(await videoCacheStatus(account));
    } catch {
      setState(false);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="card-box video-cache-settings">
      <div className="row tight">
        <HardDrive size={17} />
        <b>{t('settings.videoCache')}</b>
        <div className="spacer" />
        <button
          className="small ghost"
          disabled={busy || state === false || state === null}
          onClick={clear}
        >
          <Trash2 size={14} />
          {t('settings.videoCacheClear')}
        </button>
      </div>
      <small className="muted" role="status">
        {state === false
          ? t('settings.videoCacheUnavailable')
          : state
            ? t(state.count === 1 ? 'settings.videoCacheUsageOne' : 'settings.videoCacheUsage', {
                amount: (state.bytes / 1048576).toFixed(1),
                count: state.count,
              })
            : t('settings.videoCacheChecking')}
      </small>
      <small className="muted">{t('settings.videoCacheHelp')}</small>
    </section>
  );
}
