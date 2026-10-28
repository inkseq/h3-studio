import { useI18n } from './i18n/I18nProvider.jsx';
import { Disclosure } from './ui/Disclosure.jsx';
import { Button } from './ui/Button.jsx';
import { Dialog, DialogActions } from './ui/Dialog.jsx';
import { FloatingMessage } from './floating-notice.js';
import React, { useEffect, useRef, useState, useEffectEvent } from 'react';
import {
  failureLabel,
  hostPolicyOptions,
  hostText,
  knownNumber,
  secondsLabel,
  validHostSummary,
} from './operator-host-model.js';
import { money, time } from './p4-operator-model.js';
import './v2/ops-adapter.css';
const localizedPolicy = (value, t) =>
  t('operator.policy.' + (['default', 'prefer', 'deny'].includes(value) ? value : 'unknown'));

function SampleGroups({ groups = [] }) {
  const { t, locale } = useI18n();
  return groups.length ? (
    <div className="p4-host-samples">
      <table>
        <thead>
          <tr>
            <th>{t('operator.copy148')}</th>
            <th>{t('operator.copy149')}</th>
            <th>{t('operator.copy150')}</th>
            <th>{t('operator.copy151')}</th>
            <th>{t('operator.copy152')}</th>
          </tr>
        </thead>
        <tbody>
          {groups.map((group, index) => (
            <tr key={index}>
              <td>
                {hostText(group.profile_id, locale)}
                <small>
                  {hostText(group.mode, locale)} · {hostText(group.resolution, locale)} ·{' '}
                  {t('operatorPanel.steps', { count: group.steps ?? '—' })} ·{' '}
                  {hostText(group.warmth, locale)}
                  <br />
                  {t('operatorPanel.mediaTiming', {
                    seconds: knownNumber(group.duration_s) ?? t('operator.status72'),
                    frames: knownNumber(group.frames) ?? t('operator.status72'),
                    fps: knownNumber(group.fps) ?? t('operator.status72'),
                  })}
                  <br />
                  {t('operatorPanel.references', {
                    images: knownNumber(group.image_refs) ?? t('operator.status72'),
                    videos: knownNumber(group.video_refs) ?? t('operator.status72'),
                    audio: knownNumber(group.audio_refs) ?? t('operator.status72'),
                  })}
                  <br />
                  {t('operatorPanel.samples', { count: group.samples })}
                </small>
              </td>
              <td>
                {secondsLabel(group.median_cold_start_s, locale)}
                <small>
                  {t('operatorPanel.observedCount', { count: group.cold_start_samples })}
                  <br />
                  {group.cold_start_basis === 'bootstrap_to_native_worker_ready'
                    ? t('operator.status73')
                    : t('operator.status74')}
                </small>
              </td>
              <td>
                {secondsLabel(group.median_generation_s, locale)}
                <small>
                  {t('operatorPanel.observedCount', { count: group.generation_samples })}
                  <br />
                  {t('operator.copy153')}
                </small>
              </td>
              <td>
                {money(knownNumber(group.cost_per_video_microusd))}
                <small>{t('operator.copy154')}</small>
              </td>
              <td>
                {knownNumber(group.download_mb_s) === null
                  ? t('operator.status72')
                  : group.download_mb_s.toFixed(1) + ' MB/s'}
                {group.download_basis === 'network_transfer_unobserved' && (
                  <small>{t('operator.copy155')}</small>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  ) : (
    <p className="p4-meta">{t('operator.copy156')}</p>
  );
}
function LastFailure({ failure }) {
  const { locale } = useI18n();
  return (
    <div className="p4-host-failure">
      <span>{failureLabel(failure, locale)}</span>
      {failure && (
        <small>
          {time(failure.at, locale)}
          {typeof failure.reason_code === 'string' &&
            /^[a-z][a-z0-9_]{0,99}$/.test(failure.reason_code) && (
              <>
                {' · '}
                <code>{failure.reason_code}</code>
              </>
            )}
        </small>
      )}
    </div>
  );
}
function LastDiagnostic({ diagnostic }) {
  const { t, locale } = useI18n();
  if (!diagnostic) return null;
  const outcome =
    diagnostic.outcome === 'failure'
      ? t('operator.status75')
      : diagnostic.outcome === 'unknown'
        ? t('operator.status76')
        : diagnostic.outcome === 'cancelled'
          ? t('operator.status77')
          : t('operator.status78');
  return (
    <div className="p4-host-failure">
      <span>{t('operatorPanel.diagnostic', { outcome })}</span>
      <small>
        {time(diagnostic.at, locale)} · {hostText(diagnostic.stage, locale)}
        {typeof diagnostic.reason_code === 'string' &&
          /^[a-z][a-z0-9_]{0,99}$/.test(diagnostic.reason_code) && (
            <>
              {' · '}
              <code>{diagnostic.reason_code}</code>
            </>
          )}
      </small>
    </div>
  );
}
export function HostRecord({ host, children }) {
  const { t, locale } = useI18n();
  const observed = host.latest_observation;
  return (
    <section
      className="p4-panel p4-host-record"
      aria-label={t('operatorPanel.host', { id: host.machine_id })}
    >
      <header className="p4-between">
        <div>
          <h3>{t('operatorPanel.hostTarget', { id: host.machine_id })}</h3>
          <small>
            {host.identity_confirmed ? t('operator.status79') : t('operator.status80')} ·{' '}
            {t('operatorPanel.lastObserved', { time: time(host.last_observed_at, locale) })}
          </small>
        </div>
        <span
          className={
            'p4-pill ' +
            (host.policy.effective === 'deny'
              ? 'warm'
              : host.policy.effective === 'prefer'
                ? 'good'
                : 'muted')
          }
        >
          {localizedPolicy(host.policy.effective, t)}
        </span>
      </header>
      <div className="p4-host-summary">
        <span>
          <b>{host.runs}</b>
          {t('operator.copy157')}
        </span>
        <span>
          <b>{host.failure_count}</b>
          {t('operator.copy158')}
        </span>
        <span>
          {t('operatorPanel.failureRate', {
            value:
              knownNumber(host.failure_rate) === null
                ? t('operatorPanel.unknown')
                : (host.failure_rate * 100).toFixed(0) + '%',
          })}
        </span>
        {Number.isSafeInteger(host.qualified_start_failure_count) &&
          host.qualified_start_failure_count >= 0 && (
            <span>
              {t('operatorPanel.qualifiedFailures', { count: host.qualified_start_failure_count })}
            </span>
          )}
        {Number.isSafeInteger(host.execution_failure_count) &&
          host.execution_failure_count >= 0 && (
            <span>
              {t('operatorPanel.executionFailures', { count: host.execution_failure_count })}
            </span>
          )}
      </div>
      {observed ? (
        <div className="p4-kv">
          <div>
            <small>{t('operator.copy159')}</small>
            {hostText(observed.gpu, locale)} × {observed.gpu_count ?? '—'}
          </div>
          <div>
            <small>{t('operator.copy160')}</small>
            {knownNumber(observed.allocated_ram_mib) === null
              ? '—'
              : Math.round(observed.allocated_ram_mib / 1024)}{' '}
            /{' '}
            {knownNumber(observed.allocated_vram_mib) === null
              ? '—'
              : Math.round(observed.allocated_vram_mib / 1024)}{' '}
            GiB
          </div>
          <div>
            <small>{t('operator.copy161')}</small>
            {observed.cpu ?? '—'}
          </div>
          <div>
            <small>{t('operator.copy162')}</small>
            {t('operatorPanel.perHour', {
              amount: money(knownNumber(observed.quoted_hourly_microusd)),
            })}
          </div>
          <div>
            <small>{t('operator.copy163')}</small>
            {hostText(observed.region, locale)} ·{' '}
            {knownNumber(observed.reliability) === null
              ? t('operator.status72')
              : (observed.reliability * 100).toFixed(1) + '%'}
          </div>
          <div>
            <small>{t('operator.copy164')}</small>
            {time(observed.observed_at, locale)}
          </div>
        </div>
      ) : (
        <p className="p4-meta">{t('operator.copy165')}</p>
      )}
      {!host.last_failure && host.failure_count > 0 ? (
        <p className="p4-meta">{t('operator.copy166')}</p>
      ) : (
        <LastFailure failure={host.last_failure} />
      )}
      <LastDiagnostic diagnostic={host.last_diagnostic} />{' '}
      {host.policy.automatic_deny_until && (
        <p className="p4-warning">
          {t('operatorPanel.denyUntil', { time: time(host.policy.automatic_deny_until, locale) })}
        </p>
      )}
      <Disclosure summary={t('operator.copy167')}>
        <SampleGroups groups={host.groups} />
      </Disclosure>
      <Disclosure summary={t('operator.copy168')}>
        {host.rentals.length ? (
          <div className="p4-host-rentals">
            {host.rentals.map((row, index) => (
              <section key={index}>
                <b>{t('operatorPanel.node', { id: hostText(row.node_id, locale) })}</b>
                <small>
                  dstack run {hostText(row.dstack_run_id, locale)}
                  <br />
                  Offer {hostText(String(row.offer_id ?? ''), locale)} ·{' '}
                  {t('operatorPanel.instance', {
                    id: hostText(String(row.provider_instance_id ?? ''), locale),
                  })}
                  <br />
                  {hostText(row.state, locale)} · {hostText(row.stage, locale)} ·{' '}
                  {time(row.observed_at, locale)}
                  <br />
                  {hostText(row.profile_id, locale)} · {hostText(row.mode, locale)} ·{' '}
                  {hostText(row.region, locale)} ·{' '}
                  {t('operatorPanel.quote', {
                    amount: money(knownNumber(row.quoted_hourly_microusd)),
                  })}
                </small>
              </section>
            ))}
          </div>
        ) : (
          <p>{t('operator.copy169')}</p>
        )}
      </Disclosure>
      {children}
    </section>
  );
}
export function OfferHostHistory({ members = [], profileId, now = Date.now() }) {
  const { t, locale } = useI18n();
  const histories = members.flatMap((offer) =>
    Array.isArray(offer.host_history)
      ? offer.host_history.filter((row) => row?.capacity_profile_id === profileId)
      : [],
  );
  const known = histories.filter(validHostSummary);
  return (
    <Disclosure
      className="p4-offer-hosts"
      summary={
        <>
          {t('operatorPanel.history', {
            detail: known.length
              ? t('operatorPanel.mappings', { count: known.length })
              : t('operator.status81'),
          })}
        </>
      }
    >
      {known.length ? (
        known.map((host, index) => {
          const fresh =
            typeof host.offer_observed_at === 'number' &&
            host.offer_observed_at * 1000 <= now &&
            typeof host.selection_expires_at === 'number' &&
            host.selection_expires_at * 1000 > now;
          return (
            <section key={String(host.offer_id) + ':' + index}>
              <b>{t('operatorPanel.hostNumber', { id: host.machine_id })}</b>
              <small>
                Offer #{host.offer_id} ·{' '}
                {fresh ? t('operatorPanel.mappingValid') : t('operator.status82')}
                <br />
                {t('operatorPanel.observed', { time: time(host.offer_observed_at, locale) })} ·{' '}
                {localizedPolicy(host.policy.effective, t)}
              </small>
              <span>
                {t('operatorPanel.runsSamples', {
                  runs: host.runs ?? '—',
                  samples:
                    host.groups?.reduce(
                      (sum, group) =>
                        sum + (Number.isSafeInteger(group.samples) ? group.samples : 0),
                      0,
                    ) ?? '—',
                })}
              </span>
              <LastFailure failure={host.last_failure} />
              <SampleGroups groups={host.groups || []} />
            </section>
          );
        })
      ) : (
        <p>{t('operator.copy170')}</p>
      )}
      <small>{t('operator.copy171')}</small>
    </Disclosure>
  );
}
function PolicyReview({ host, selection, current, busy, onClose, onConfirm }) {
  const { t } = useI18n();
  return (
    <Dialog
      title={t('operatorPanel.confirmPolicy', { id: host.machine_id })}
      subtitle={localizedPolicy(host.policy.manual, t) + ' → ' + localizedPolicy(selection, t)}
      busy={busy}
      onClose={onClose}
    >
      <p>{t('operator.copy172')}</p>
      {selection === 'prefer' && <p className="p4-meta">{t('operator.copy173')}</p>}
      {!current && <p role="alert">{t('operator.copy174')}</p>}
      <DialogActions>
        <Button disabled={busy} onClick={onClose}>
          {t('operator.copy175')}
        </Button>
        <Button variant="primary" disabled={busy || !current} onClick={onConfirm}>
          {busy ? t('operator.status83') : t('operator.status84')}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
function HostEditor({ host, busy, onSave }) {
  const { t, locale } = useI18n();
  const [selection, setSelection] = useState(host.policy.manual),
    [review, setReview] = useState(null),
    [error, setError] = useState(''),
    [notice, setNotice] = useState('');
  const current =
    review?.host.policy.snapshot === host.policy.snapshot &&
    review?.host.policy.revision === host.policy.revision;
  async function save() {
    if (!review || busy || !current) return;
    setError('');
    try {
      const value = await onSave(review.host, review.selection);
      setNotice(
        value.changed
          ? t('operatorPanel.policySaved', {
              command: value.command_id,
              audit: value.audit_event_id,
            })
          : t('operatorPanel.policyUnchanged', { command: value.command_id }),
      );
      setReview(null);
    } catch (error) {
      setError(error.message);
      setReview(null);
    }
  }
  return (
    <>
      <form
        className="p4-host-policy"
        onSubmit={(event) => {
          event.preventDefault();
          setError('');
          setNotice('');
          setReview({ host: structuredClone(host), selection });
        }}
      >
        <label>
          {t('operator.copy176')}
          <select
            aria-label={t('operatorPanel.policyAria', { id: host.machine_id })}
            value={selection}
            disabled={busy}
            onChange={(event) => {
              setSelection(event.target.value);
              setReview(null);
              setNotice('');
            }}
          >
            {hostPolicyOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {t('operator.policy.' + option.value)}
              </option>
            ))}
          </select>
        </label>
        <Button type="submit" disabled={busy || selection === host.policy.manual}>
          {t('operator.copy177')}
        </Button>
        <small>
          {t('operatorPanel.revision', {
            revision: host.policy.revision,
            reason: hostText(host.policy.reason_code, locale),
          })}
        </small>
      </form>
      <FloatingMessage tone="info" text={notice ? notice : null} />
      <FloatingMessage tone="error" text={error ? error : null} />
      {review && (
        <PolicyReview
          host={review.host}
          selection={review.selection}
          current={current}
          busy={busy}
          onClose={() => setReview(null)}
          onConfirm={() => void save()}
        />
      )}
    </>
  );
}
function HostLookup({ controller, target, busy }) {
  const { t } = useI18n();
  const [input, setInput] = useState(''),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(''),
    generation = useRef(0);
  useEffect(
    () => () => {
      generation.current++;
    },
    [],
  );
  const machine = input.trim(),
    valid = /^[1-9][0-9]{0,17}$/.test(machine),
    current = target && String(target.machine_id) === machine;
  async function lookup() {
    const request = ++generation.current;
    setLoading(true);
    setError('');
    try {
      await controller.readHostTarget(machine);
    } catch (error) {
      if (generation.current === request) setError(error.message);
    } finally {
      if (generation.current === request) setLoading(false);
    }
  }
  return (
    <section className="p4-panel p4-host-lookup" aria-label={t('operator.status126')}>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (valid && !busy && !loading) void lookup();
        }}
      >
        <label>
          {t('operator.copy178')}
          <input
            aria-label={t('operator.status127')}
            inputMode="numeric"
            maxLength="18"
            value={input}
            disabled={busy || loading}
            onChange={(event) => {
              setInput(event.target.value);
              setError('');
            }}
          />
        </label>
        <Button type="submit" disabled={busy || loading || !valid}>
          {loading ? t('operator.status85') : t('operator.status86')}
        </Button>
      </form>
      <p className="p4-meta">{t('operator.copy179')}</p>
      <FloatingMessage tone="error" text={error ? error : null} />
      {current && (
        <HostRecord key={machine} host={target}>
          <HostEditor
            host={target}
            busy={busy || loading}
            onSave={(host, selection) => controller.updateHostPolicy(host, selection)}
          />
        </HostRecord>
      )}
    </section>
  );
}
export default function OperatorHostsPanel({ controller, state, admin = false, busy = false }) {
  const { t, locale } = useI18n();
  const value = admin ? state.adminHosts : state.hosts,
    [loading, setLoading] = useState(false),
    [error, setError] = useState(''),
    generation = useRef(0);
  const readEvent = useEffectEvent((...args) => read(...args));
  useEffect(() => {
    generation.current++;
    setError('');
    void readEvent();
    const generationRef = generation;
    return () => {
      generationRef.current++;
    };
  }, [controller, state.account, admin]);
  async function read(offset = 0) {
    const current = generation.current;
    setLoading(true);
    setError('');
    try {
      await controller.readHosts({ admin, offset });
      if (admin) await controller.readHostAudit();
    } catch (error) {
      if (generation.current === current) setError(error.message);
    } finally {
      if (generation.current === current) setLoading(false);
    }
  }
  const blocked = busy || loading;
  return (
    <section
      className="p4-hosts"
      aria-label={admin ? t('operator.status87') : t('operator.status88')}
    >
      <header className="p4-between">
        <div>
          <h2>{admin ? t('operator.status89') : t('operator.status88')}</h2>
          <p className="p4-meta">
            {admin ? t('operator.status90') : t('operator.status91')} {t('operatorPanel.grouping')}
          </p>
        </div>
        <Button disabled={blocked} onClick={() => void read()}>
          {loading ? t('operator.status70') : t('operator.status92')}
        </Button>
      </header>
      {error && (
        <p className="p4-error" role="alert">
          {error} {t('operatorPanel.retained')}
        </p>
      )}
      {admin && (
        <HostLookup
          key={state.account}
          controller={controller}
          target={state.hostTarget}
          busy={blocked}
        />
      )}{' '}
      {value ? (
        <>
          <small>{t('operatorPanel.readHosts', { time: time(value.as_of, locale) })}</small>
          {value.hosts.length ? (
            value.hosts.map((host) => (
              <HostRecord key={host.machine_id} host={host}>
                {admin && (
                  <HostEditor
                    host={host}
                    busy={blocked}
                    onSave={(host, selection) => controller.updateHostPolicy(host, selection)}
                  />
                )}
              </HostRecord>
            ))
          ) : (
            <p className="ui-empty ui-empty-compact">{t('operator.copy180')}</p>
          )}
          {value.has_more && (
            <Button disabled={blocked} onClick={() => void read(value.offset + value.limit)}>
              {t('operator.copy181')}
            </Button>
          )}
          <p className="p4-meta">{t('operator.copy182')}</p>
        </>
      ) : (
        <p role="status">{loading ? t('operator.status93') : t('operator.status94')}</p>
      )}
      {admin && (
        <Disclosure className="p4-host-audits" summary={t('operator.copy183')}>
          {state.hostAudits?.events?.length ? (
            <ul>
              {state.hostAudits.events.map((event) => (
                <li key={event.id}>
                  {t('operatorPanel.hostNumber', { id: event.machine_id })} ·{' '}
                  {localizedPolicy(event.old.manual, t)} → {localizedPolicy(event.new.manual, t)}
                  <small>
                    {time(event.created_at, locale)} ·{' '}
                    {hostText(event.actor_owner || event.actor_id, locale)} · {event.id}
                  </small>
                </li>
              ))}
            </ul>
          ) : (
            <p>{t('operator.copy184')}</p>
          )}
          {state.hostAudits?.has_more && (
            <Button
              disabled={blocked}
              onClick={() =>
                void controller
                  .readHostAudit({ offset: state.hostAudits.offset + state.hostAudits.limit })
                  .catch((error) => setError(error.message))
              }
            >
              {t('operator.copy185')}
            </Button>
          )}
        </Disclosure>
      )}
    </section>
  );
}
