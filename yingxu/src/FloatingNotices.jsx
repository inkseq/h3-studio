import React, { useSyncExternalStore } from 'react';
import { Button } from './ui/Button.jsx';
import { X } from 'lucide-react';
import { currentNotices, dismissNotice, subscribeNotices } from './floating-notice.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import './floating-notices.css';

export default function FloatingNotices() {
  const { t } = useI18n();
  const list = useSyncExternalStore(subscribeNotices, currentNotices, currentNotices);
  // The same text from several places shows once; newest last.
  const shown = [];
  for (const item of list)
    if (!shown.some((other) => other.text === item.text && other.tone === item.tone))
      shown.push(item);
  if (!shown.length) return null;
  return (
    <div className="floating-notices" aria-live="polite">
      {shown.slice(-3).map((item) => (
        <div
          key={item.id}
          className={'floating-notice ' + item.tone}
          role={item.tone === 'error' ? 'alert' : 'status'}
        >
          <span title={item.text}>{item.text}</span>
          {item.action && (
            <Button
              className="floating-notice-action"
              disabled={item.action.disabled}
              onClick={item.action.onClick}
            >
              {item.action.label}
            </Button>
          )}
          <Button aria-label={t('notice.dismiss')} onClick={() => dismissNotice(item.text)}>
            <X size={14} />
          </Button>
        </div>
      ))}
    </div>
  );
}
