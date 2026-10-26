import { useI18n } from './i18n/I18nProvider.jsx';
import { Button } from './ui/Button.jsx';
import React from 'react';
import './generation-availability.css';
import { modeAvailability } from './generation-availability.js';

export default function GenerationAvailability({
  snapshot,
  capabilities,
  settings,
  operator = false,
  compact = false,
  onRefresh,
  checking = false,
}) {
  const { t, locale } = useI18n();
  const current = modeAvailability(snapshot, capabilities, settings, undefined, Date.now(), locale);
  return (
    <section
      className={'qc-availability ' + (compact ? 'qc-availability-compact' : '')}
      aria-label={t('availability.title')}
      role="status"
    >
      <div className="qc-availability-modes">
        {(compact ? [current.mode] : ['fl', 'ref']).filter(Boolean).map((mode) => {
          const item = modeAvailability(snapshot, capabilities, settings, mode, Date.now(), locale);
          return (
            <span
              key={mode}
              className={'qc-availability-mode ' + (item.available ? 'available' : 'unavailable')}
              aria-current={mode === current.mode ? 'true' : undefined}
            >
              <b>{t('availability.' + mode)}</b>
              <span>{item.label}</span>
            </span>
          );
        })}
      </div>
      <details>
        <summary>{t('quickView.inputDetails')}</summary>
        <p>{current.message}</p>
      </details>
      <div className="qc-availability-actions">
        {onRefresh && (
          <Button disabled={checking} onClick={onRefresh}>
            {checking ? t('availability.checking') : t('availability.refresh')}
          </Button>
        )}
        {operator && !current.available && <a href="/operator">{t('availability.console')}</a>}
      </div>
    </section>
  );
}
