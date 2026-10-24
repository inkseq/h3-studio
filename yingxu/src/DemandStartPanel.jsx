import { Note } from './ui/Note.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { FloatingMessage } from './floating-notice.js';
import React, { useState } from 'react';
import { validDemandStart } from './demand-start-model.js';

// CR43 automatic GPU start: human-admin summary and audited runtime switch.
// Renders only when the admin workspace carries the additive `demand_start` field.
export default function DemandStartPanel({ controller, state, busy }) {
  const { t } = useI18n();
  const value = state.adminSnapshot?.demand_start;
  const [confirm, setConfirm] = useState(null),
    [reason, setReason] = useState(''),
    [saving, setSaving] = useState(false),
    [error, setError] = useState('');
  if (value === undefined) return null;
  if (!validDemandStart(value))
    return (
      <section className="p4-panel p4-demand-start">
        <h3>{t('demand.title')}</h3>
        <p role="alert" className="p4-error">
          {t('demand.invalid')}
        </p>
      </section>
    );
  const occupied = value.running_machines + value.starting_machines + value.uncertain_machines;
  async function save() {
    setSaving(true);
    setError('');
    try {
      await controller.client.request('/v1/operator/dstack/admin/demand-start', {
        method: 'PUT',
        body: {
          expected_revision: value.revision,
          enabled: confirm.enabled,
          reason: reason.trim(),
        },
        key: confirm.key,
      });
      setConfirm(null);
      setReason('');
      await controller.refresh(true);
    } catch (e) {
      setError(e.status === 409 ? t('demand.conflict') : e.message);
    } finally {
      setSaving(false);
    }
  }
  return (
    <section className="p4-panel p4-demand-start" aria-label={t('demand.title')}>
      <header className="p4-between">
        <div>
          <h3>{t('demand.title')}</h3>
          <p className="p4-meta">{t('demand.help', { max: value.hard_max_machines })}</p>
        </div>
        <span className={'p4-demand-state ' + (value.enabled ? 'on' : 'off')}>
          {t(value.enabled ? 'demand.on' : 'demand.off')}
        </span>
      </header>
      <dl className="p4-demand-grid">
        <div>
          <dt>{t('demand.machines')}</dt>
          <dd>
            {occupied} / {value.hard_max_machines}
          </dd>
          <small>
            {t('demand.breakdown', {
              running: value.running_machines,
              starting: value.starting_machines,
              uncertain: value.uncertain_machines,
            })}
          </small>
        </div>
        <div>
          <dt>{t('demand.queued')}</dt>
          <dd>{value.queued_generation_jobs}</dd>
          <small>
            {t('v2.firstLast')} {value.queued_by_mode.fl} · {t('v2.reference')}{' '}
            {value.queued_by_mode.ref}
          </small>
        </div>
        <div>
          <dt>{t('demand.status')}</dt>
          <dd className="p4-demand-reason">
            {value.blocked_reason ? t('demand.reason.' + value.blocked_reason) : t('demand.ok')}
          </dd>
        </div>
      </dl>
      {confirm === null ? (
        <button
          className={value.enabled ? '' : 'p4-primary'}
          disabled={busy || saving}
          onClick={() => {
            setError('');
            // One key per confirmation, so a retried click replays instead of mutating twice.
            setConfirm({ enabled: !value.enabled, key: 'demand-' + crypto.randomUUID() });
          }}
        >
          {t(value.enabled ? 'demand.turnOff' : 'demand.turnOn')}
        </button>
      ) : null}
      {confirm !== null && (
        <form
          className="p4-demand-confirm"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <label>
            {t('demand.reasonLabel')}
            <input
              value={reason}
              maxLength={500}
              required
              autoFocus
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          <Note>{t(confirm.enabled ? 'demand.onNote' : 'demand.offNote')}</Note>
          <div className="p4-between">
            <button type="button" disabled={saving} onClick={() => setConfirm(null)}>
              {t('takes.cancel')}
            </button>
            <button className="p4-primary" disabled={saving || !reason.trim()}>
              {t(confirm.enabled ? 'demand.turnOn' : 'demand.turnOff')}
            </button>
          </div>
        </form>
      )}
      <FloatingMessage tone="error" text={error ? error : null} />
    </section>
  );
}
