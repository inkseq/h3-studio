import React, { useEffect, useState } from 'react';
import { Wrench } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { MAINTENANCE_CHANGED, formatEnd, noticeState } from './maintenance-notice.js';
import './maintenance.css';

const POLL_MS = 60_000;

// One banner on every page while a release is in progress (#453). It polls the anonymous auth
// config quietly, re-checks when a request is refused for maintenance, and clears by itself.
export default function MaintenanceBanner() {
  const { t, locale } = useI18n();
  const [config, setConfig] = useState(null);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    let live = true,
      timer = null;
    const check = async () => {
      clearTimeout(timer);
      if (document.visibilityState !== 'hidden') {
        try {
          const response = await fetch('/api/auth/config', {
            headers: { Accept: 'application/json' },
            credentials: 'same-origin',
          });
          if (response.ok && live) setConfig(await response.json());
        } catch {
          // Offline or between deploys: keep what we last knew.
        }
        if (live) setNow(Date.now());
      }
      if (live) timer = setTimeout(check, POLL_MS);
    };
    void check();
    const again = () => void check();
    window.addEventListener(MAINTENANCE_CHANGED, again);
    document.addEventListener('visibilitychange', again);
    return () => {
      live = false;
      clearTimeout(timer);
      window.removeEventListener(MAINTENANCE_CHANGED, again);
      document.removeEventListener('visibilitychange', again);
    };
  }, []);
  const notice = noticeState(config, now);
  if (!notice) return null;
  const time = formatEnd(notice.endsAt, locale, now);
  return (
    <div className={'maintenance-bar ' + notice.severity} role="status" aria-live="polite">
      <Wrench size={15} aria-hidden="true" />
      <span>
        <strong>{t('maintenance.title')}</strong>{' '}
        {notice.overdue
          ? t('maintenance.overdue')
          : time
            ? t('maintenance.until', { time })
            : t('maintenance.soon')}{' '}
        {t('maintenance.safe')}
        {notice.message && <em> {notice.message}</em>}
      </span>
    </div>
  );
}
