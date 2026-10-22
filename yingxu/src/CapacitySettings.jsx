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
  const tone = ['limit_reached', 'usage_unconfirmed'].includes(daily.status)
    ? 'bad'
    : daily.status === 'warning'
      ? 'warn'
      : 'ok';
  return (
    <section className={'cap-today ' + tone} aria-label={t('capacity.today')}>
      <div className="cap-today-head">
        <strong>{t('capacity.today.' + daily.status)}</strong>
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

function Consequences({ preview }) {
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
        return (
          <li key={index} className={short ? 'bad' : ''}>
            {short && <AlertTriangle size={14} aria-hidden="true" />}
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

  const unit = (id) => {
    const label = t('capacity.unit.' + FIELD_UNITS[id]);
    return label ? <span className="cap-unit">{label}</span> : null;
  };
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
      <small className="cap-error" role="alert">
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
      <small className="cap-last" title={change.reason || undefined}>
        {t('capacity.lastChange', {
          actor: String(change.actor_id || '').replace(/^browser:/, ''),
          time,
        })}
        {change.reason ? ` · ${change.reason}` : ''}
      </small>
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
      <div key={id} className={'cap-row' + (changed.includes(id) ? ' changed' : '')}>
        <div className="cap-label">
          <b>{t('capacity.field.' + id)}</b>
          {pending && <span className="cap-badge">{t('capacity.pending')}</span>}
          {helpKey && <small className="muted">{t(helpKey)}</small>}
          {lastChange(key)}
        </div>
        <div className="cap-value">
          <span className="cap-input">
            {input(id)}
            {unit(id)}
          </span>
          {suggested && (
            <button type="button" className="linkish" onClick={() => recommend([id])}>
              {t('capacity.recommended', { value: toText(id, leaf(defaults, id)) })}
            </button>
          )}
          {problem(id)}
        </div>
      </div>
    );
  };
  const shareRow = () => {
    const ids = [
      'batch_guaranteed_share_slots.numerator',
      'batch_guaranteed_share_slots.denominator',
    ];
    return (
      <div
        key="share"
        className={'cap-row' + (ids.some((id) => changed.includes(id)) ? ' changed' : '')}
      >
        <div className="cap-label">
          <b>{t('capacity.field.batch_guaranteed_share_slots')}</b>
          {pendingControl(settings, ids[0]) && (
            <span className="cap-badge">{t('capacity.pending')}</span>
          )}
          <small className="muted">
            {t('capacity.batchShare', {
              numerator: draft[ids[0]] || '?',
              denominator: draft[ids[1]] || '?',
            })}
          </small>
        </div>
        <div className="cap-value">
          <span className="cap-input">
            {input(ids[0], true)}
            <span className="cap-unit">/</span>
            {input(ids[1], true)}
          </span>
          {problem(ids[0])}
          {problem(ids[1])}
        </div>
      </div>
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
            <button type="button" className="ghost small" onClick={() => recommend(recommendedIds)}>
              {t('capacity.useRecommended')}
            </button>
          )}
        </div>
      )}
      {CAPACITY_GROUPS.map((group) => (
        <section key={group.id} className="cap-group" aria-labelledby={'cap-' + group.id}>
          <h4 id={'cap-' + group.id}>{t('capacity.group.' + group.id)}</h4>
          {group.id === 'queue' && <p className="muted small">{t('capacity.group.queueHelp')}</p>}
          {group.fields.map(([id]) =>
            id === 'batch_guaranteed_share_slots.denominator'
              ? null
              : id === 'batch_guaranteed_share_slots.numerator'
                ? shareRow()
                : row(id),
          )}
        </section>
      ))}
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
            <button type="button" className="ghost" disabled={busy} onClick={() => setReview(null)}>
              {t('capacity.back')}
            </button>
            <button
              type="button"
              className="primary"
              disabled={busy || !reason.trim()}
              onClick={confirm}
            >
              {t('capacity.confirm')}
            </button>
          </div>
        </section>
      ) : (
        <div className="cap-bar">
          <span>{count > 0 ? t('capacity.changes', { count }) : ''}</span>
          {count > 0 && (
            <button
              type="button"
              className="ghost small keep-label"
              disabled={busy}
              onClick={() => {
                setDraft(draftFrom(base));
                setError('');
              }}
            >
              <RotateCcw size={14} aria-hidden="true" />
              {t('capacity.undo')}
            </button>
          )}
          <button
            type="button"
            className="primary"
            disabled={busy || count === 0 || invalid}
            onClick={check}
          >
            {t('capacity.review')}
          </button>
        </div>
      )}
    </div>
  );
}
