import { SettingRow, SettingsSection } from './ui/Settings.jsx';
import { Button } from './ui/Button.jsx';
import React, { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Clock, RotateCcw, ShieldCheck } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { formatMoneyMicros } from './money.js';
import {
  CAPACITY_GROUPS,
  FIELD_UNITS,
  baseline,
  buildChanges,
  confirmBody,
  draftFrom,
  fleetShortfall,
  leaf,
  offDefault,
  pendingControl,
  toText,
} from './capacity-settings-model.js';
import './capacity-settings.css';

const PATH = '/v1/operator/admin/spending-settings';
const KNOWN_ERRORS = [
  'spending_settings_changed',
  'spending_confirmation_changed',
  'spending_confirmation_required',
  'spending_settings_invalid',
];
const HELP = new Set([
  'tenant_daily_limit_microusd',
  'tenant_daily_warning_fraction',
  'fleet_hourly_limit_microusd',
  'max_hourly_price_microusd',
  'idle_active_seconds',
]);

function DailyStatus({ daily }) {
  const { t, locale } = useI18n();
  if (!daily) return null;
  const money = formatMoneyMicros;
  const spent = daily.projected_microusd ?? daily.confirmed_exposure_microusd;
  const resets = Number.isFinite(daily.resets_at)
    ? new Intl.DateTimeFormat(locale === 'zh' ? 'zh-CN' : 'en-AU', {
        timeZone: daily.timezone || 'Australia/Sydney',
        weekday: 'short',
        hour: 'numeric',
        minute: '2-digit',
      }).format(new Date(daily.resets_at * 1000))
    : null;
  // The server can flag "warning" for unconfirmed supplier bills alone; only call it near the
  // cap when spend really is near it (h3-studio#537), the bills note below covers the rest.
  const near =
    Number.isSafeInteger(daily.limit_microusd) && daily.limit_microusd > 0
      ? (spent || 0) >= daily.limit_microusd * (daily.warning_fraction ?? 0.8)
      : true;
  const status = daily.status === 'warning' && !near ? 'within_limit' : daily.status;
  const tone = ['limit_reached', 'usage_unconfirmed'].includes(status)
    ? 'bad'
    : status === 'warning'
      ? 'warn'
      : 'ok';
  return (
    <section className={'cap-today ' + tone} aria-label={t('capacity.today')}>
      <div className="cap-today-head">
        <strong>{t('capacity.today.' + status)}</strong>
        {resets && (
          <small>
            <Clock size={13} aria-hidden="true" /> {t('capacity.resets', { time: resets })}
          </small>
        )}
      </div>
      <p className="cap-today-amount">
        {Number.isSafeInteger(daily.limit_microusd)
          ? t('capacity.todaySpent', { spent: money(spent), limit: money(daily.limit_microusd) })
          : t('capacity.todaySpentNoLimit', { spent: money(spent) })}
      </p>
      {Number.isSafeInteger(daily.limit_microusd) && Number.isSafeInteger(spent) && (
        <span
          className="cap-meter"
          style={{ '--used': Math.min(1, spent / daily.limit_microusd) }}
          aria-hidden="true"
        />
      )}
      <small className="muted">
        {t('capacity.todayParts', {
          settled: money(daily.settled_microusd),
          committed: money(daily.committed_microusd),
          pending: money(daily.pending_microusd),
        })}
      </small>
      {daily.unresolved_count > 0 && (
        <details className="cap-unresolved">
          <summary>
            <AlertTriangle size={14} aria-hidden="true" />
            {t('capacity.unresolved', { count: daily.unresolved_count })}
          </summary>
          <p className="muted">{t('capacity.unresolvedHelp')}</p>
          <ul>
            {(daily.details || []).map((item, index) => (
              <li key={index}>
                <code>{item.code}</code> · {item.reference_type} <code>{item.reference_id}</code>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

export function Consequences({ preview }) {
  const { t } = useI18n();
  const money = formatMoneyMicros;
  const pending =
    preview.consequences?.find((item) => item.code === 'control_pending_implementation')?.fields ||
    [];
  const fieldList = (fields) =>
    (fields || [])
      .filter((field) => field !== 'capacity_policy_version')
      .map((field) => t('capacity.field.' + field))
      .join('、');
  return (
    <ul className="cap-consequences">
      {(preview.consequences || []).map((item, index) => {
        const short = fleetShortfall(item);
        let text;
        switch (item.code) {
          case 'tenant_daily_supplier_limit':
            text = t('capacity.c.' + item.code, {
              limit: money(item.limit_microusd),
              percent: Math.round(item.warning_fraction * 100),
            });
            break;
          case 'admitted_work_may_exceed_daily_limit':
            text = t('capacity.c.' + item.code, { limit: money(item.limit_microusd) });
            break;
          case 'fleet_allows_combination':
          case 'fleet_limits_combination':
            text = t(short ? 'capacity.c.fleetShort' : 'capacity.c.' + item.code, {
              machines: item.machines,
              allowed: short?.allowed,
              required: money(item.required_hourly_microusd ?? item.hourly_microusd),
              fleet: money(item.fleet_hourly_limit_microusd),
            });
            break;
          case 'mode_price_ceiling':
            text = t('capacity.c.' + item.code, {
              mode: t('capacity.mode.' + item.mode),
              price: money(item.hourly_microusd),
            });
            break;
          case 'mode_ceiling_above_machine_ceiling':
            text = t('capacity.c.' + item.code, {
              mode: t('capacity.mode.' + item.mode),
              modePrice: money(item.mode_hourly_microusd),
              machinePrice: money(item.machine_hourly_microusd),
            });
            break;
          case 'setting_next_admission_only':
          case 'control_pending_implementation': {
            // Pending controls get their own line; do not list them twice.
            const fields = fieldList(
              item.code === 'control_pending_implementation'
                ? item.fields
                : (item.fields || []).filter((field) => !pending.includes(field)),
            );
            if (!fields) return null;
            text = t('capacity.c.' + item.code, { fields });
            break;
          }
          default:
            text = t('capacity.c.unknown', { code: item.code });
        }
        const warn = short || item.code === 'mode_ceiling_above_machine_ceiling';
        return (
          <li key={index} className={warn ? 'bad' : ''}>
            {warn && <AlertTriangle size={14} aria-hidden="true" />}
            {text}
          </li>
        );
      })}
    </ul>
  );
}

export default function CapacitySettings({ settings, api, onSaved }) {
  const { t, locale } = useI18n();
  const base = useMemo(() => baseline(settings), [settings]);
  const defaults = settings.capacity_policy?.defaults || {};
  const [draft, setDraft] = useState(() => draftFrom(base));
  const [review, setReview] = useState(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  useEffect(() => {
    setDraft(draftFrom(base));
    setReview(null);
  }, [base]);

  const { changes, errors, changed } = buildChanges(settings, draft);
  const adopted = !!settings.capacity_policy?.adopted;
  const recommendedIds = offDefault(settings);
  const latest = settings.capacity_policy?.latest_changes || {};
  const edit = (id, value) => {
    setReview(null);
    setNotice('');
    setDraft((current) => ({ ...current, [id]: value }));
  };
  const fail = (failure) => {
    const detail = failure?.detail?.detail || failure?.detail || {};
    if (detail.code === 'spending_settings_changed') onSaved?.();
    setError(
      KNOWN_ERRORS.includes(detail.code)
        ? t('capacity.err.' + detail.code, {
            detail: [].concat(detail.fields || detail.field || detail.message || []).join(', '),
          })
        : failure.message,
    );
  };
  const check = async () => {
    setBusy(true);
    setError('');
    try {
      const preview = await api.request(PATH + '/preview', {
        method: 'POST',
        body: { expected_version: settings.version, changes },
      });
      setReview({ preview, changes });
    } catch (failure) {
      fail(failure);
    } finally {
      setBusy(false);
    }
  };
  const confirm = async () => {
    setBusy(true);
    setError('');
    try {
      await api.request(PATH, {
        method: 'PUT',
        body: confirmBody(review.preview, review.changes, reason),
      });
      setReview(null);
      setReason('');
      setNotice(t('capacity.saved'));
      onSaved?.();
    } catch (failure) {
      setReview(null);
      fail(failure);
    } finally {
      setBusy(false);
    }
  };

  const unit = (id) => t('capacity.unit.' + FIELD_UNITS[id]) || null;
  const input = (id, narrow = false) => (
    <input
      className={narrow ? 'narrow' : ''}
      inputMode="decimal"
      aria-label={t('capacity.field.' + id)}
      aria-invalid={!!errors[id]}
      value={draft[id] ?? ''}
      disabled={busy}
      onChange={(event) => edit(id, event.target.value)}
    />
  );
  const problem = (id) =>
    errors[id] ? (
      <small className="ui-setting-error" role="alert">
        {t('capacity.error.' + errors[id].code, errors[id])}
      </small>
    ) : null;
  const lastChange = (key) => {
    const change = latest[key];
    if (!change) return null;
    const time = new Intl.DateTimeFormat(locale === 'zh' ? 'zh-CN' : 'en-AU', {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(new Date(change.observed_at * 1000));
    return (
      <span title={change.reason || undefined}>
        {t('capacity.lastChange', {
          actor: String(change.actor_id || '').replace(/^browser:/, ''),
          time,
        })}
        {change.reason ? ` · ${change.reason}` : ''}
      </span>
    );
  };
  const recommend = (ids) =>
    setDraft((current) => ({
      ...current,
      ...Object.fromEntries(ids.map((id) => [id, toText(id, leaf(defaults, id))])),
    }));

  const row = (id) => {
    const key = id.split('.')[0];
    const pending = pendingControl(settings, id);
    const suggested = recommendedIds.includes(id) && draft[id] !== toText(id, leaf(defaults, id));
    const helpKey = HELP.has(id) ? 'capacity.help.' + id : null;
    return (
      <SettingRow
        key={id}
        as="div"
        label={t('capacity.field.' + id)}
        badge={pending && t('capacity.pending')}
        help={helpKey && t(helpKey)}
        meta={lastChange(key)}
        unit={unit(id)}
        changed={changed.includes(id)}
        extra={
          <>
            {suggested && (
              <Button className="linkish" onClick={() => recommend([id])}>
                {t('capacity.recommended', { value: toText(id, leaf(defaults, id)) })}
              </Button>
            )}
            {problem(id)}
          </>
        }
      >
        {input(id)}
      </SettingRow>
    );
  };
  const shareRow = () => {
    const ids = [
      'batch_guaranteed_share_slots.numerator',
      'batch_guaranteed_share_slots.denominator',
    ];
    return (
      <SettingRow
        key="share"
        as="div"
        label={t('capacity.field.batch_guaranteed_share_slots')}
        badge={pendingControl(settings, ids[0]) && t('capacity.pending')}
        help={t('capacity.batchShare', {
          numerator: draft[ids[0]] || '?',
          denominator: draft[ids[1]] || '?',
        })}
        changed={ids.some((id) => changed.includes(id))}
        extra={
          <>
            {problem(ids[0])}
            {problem(ids[1])}
          </>
        }
      >
        {input(ids[0], true)}
        <span className="ui-setting-unit narrow">/</span>
        {input(ids[1], true)}
      </SettingRow>
    );
  };

  const count = changed.length;
  const invalid = Object.keys(errors).length > 0;
  return (
    <div className="cap">
      <header className="cap-head">
        <div>
          <h3>{t('capacity.title')}</h3>
          <p className="muted">{t('capacity.intro')}</p>
        </div>
        {adopted && (
          <span className="cap-version">
            <ShieldCheck size={14} aria-hidden="true" />
            {t('capacity.adopted', { version: settings.capacity_policy.capacity_policy_version })}
          </span>
        )}
      </header>
      <DailyStatus daily={settings.tenant_daily_supplier_spending} />
      {!adopted && (
        <div className="cap-banner" role="note">
          <p>{t('capacity.notAdopted')}</p>
          {recommendedIds.length > 0 && (
            <Button variant="ghost" size="sm" onClick={() => recommend(recommendedIds)}>
              {t('capacity.useRecommended')}
            </Button>
          )}
        </div>
      )}
      {CAPACITY_GROUPS.map((group) => {
        const fields = group.fields.map(([id]) =>
          id === 'batch_guaranteed_share_slots.denominator'
            ? null
            : id === 'batch_guaranteed_share_slots.numerator'
              ? shareRow()
              : row(id),
        );
        // Queue rules are recorded but not enforced yet (#456 B/C/D), so they start folded.
        return group.id === 'queue' ? (
          <details key={group.id} className="ui-settings cap-fold">
            <summary className="ui-settings-head">
              <h3 id={'cap-' + group.id}>{t('capacity.group.' + group.id)}</h3>
            </summary>
            <p className="muted small ui-settings-note">{t('capacity.group.queueHelp')}</p>
            {fields}
          </details>
        ) : (
          <SettingsSection key={group.id} title={t('capacity.group.' + group.id)}>
            {fields}
          </SettingsSection>
        );
      })}
      {error && (
        <p className="cap-error-bar" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="cap-notice" role="status">
          {notice}
        </p>
      )}
      {review ? (
        <section className="cap-review" aria-label={t('capacity.reviewTitle')}>
          <h4>{t('capacity.reviewTitle')}</h4>
          <ul className="cap-diff">
            {changed.map((id) => (
              <li key={id}>
                <span>{t('capacity.field.' + id)}</span>
                <s>{toText(id, leaf(base, id))}</s>
                <b>{draft[id]}</b>
                <small>{t('capacity.unit.' + FIELD_UNITS[id])}</small>
              </li>
            ))}
          </ul>
          {review.preview.adopts_policy && <p className="cap-adopts">{t('capacity.adopts')}</p>}
          <Consequences preview={review.preview} />
          <label className="cap-reason">
            {t('capacity.reason')}
            <textarea
              rows={2}
              maxLength={500}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
            <small className="muted">{t('capacity.reasonHelp')}</small>
          </label>
          <div className="cap-actions">
            <Button variant="ghost" disabled={busy} onClick={() => setReview(null)}>
              {t('capacity.back')}
            </Button>
            <Button variant="primary" disabled={busy || !reason.trim()} onClick={confirm}>
              {t('capacity.confirm')}
            </Button>
          </div>
        </section>
      ) : (
        <div className="cap-bar">
          <span>{count > 0 ? t('capacity.changes', { count }) : ''}</span>
          {count > 0 && (
            <Button
              variant="ghost"
              size="sm"
              className="keep-label"
              disabled={busy}
              onClick={() => {
                setDraft(draftFrom(base));
                setError('');
              }}
            >
              <RotateCcw size={14} aria-hidden="true" />
              {t('capacity.undo')}
            </Button>
          )}
          <Button variant="primary" disabled={busy || count === 0 || invalid} onClick={check}>
            {t('capacity.review')}
          </Button>
        </div>
      )}
    </div>
  );
}
