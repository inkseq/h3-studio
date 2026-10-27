import { EmptyState, Note } from './ui/Note.jsx';
import { Disclosure } from './ui/Disclosure.jsx';
import { Button } from './ui/Button.jsx';
import { Chip } from './ui/Chip.jsx';
import StudioHeader from './StudioHeader.jsx';
import { Dialog as SharedDialog } from './ui/Dialog.jsx';
import { FloatingMessage } from './floating-notice.js';
import { MenuItem } from './AccountMenu.jsx';
import { readBrowserLocale, translate } from './i18n/locale.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import React, { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import {
  ChevronRight,
  Clock,
  Plus,
  RefreshCw,
  Server,
  ShieldCheck,
  Cpu,
  Square,
  Timer,
} from 'lucide-react';
import { useCloud } from './CloudStudio.jsx';
import { cloudController as cloud } from './cloud-controller.js';
import { createP4OperatorController } from './p4-operator-controller.js';
import OperatorBudgetPanel, { AdminBudgetPanel, BudgetWorkspace } from './OperatorBudgetPanel.jsx';
import OperatorJobsPanel from './OperatorJobsPanel.jsx';
import DemandStartPanel from './DemandStartPanel.jsx';
import OperatorHostsPanel, { OfferHostHistory } from './OperatorHostsPanel.jsx';
import { inventoryError, inventoryOutcome, referenceHostOffers } from './operator-host-model.js';
import {
  priceRows,
  profilePrice,
  usdInput,
  wholeGiB,
  withDefaultPrices,
} from './operator-budget-model.js';
import {
  batchRows,
  costLabel,
  dstackProvider,
  elapsed,
  finite,
  groupedNodes,
  initialRows,
  keepText,
  lifetimeBody,
  modeLabel,
  money,
  phase,
  preparationSteps,
  preparationView,
  previewCurrent,
  queueLabel,
  rate,
  stopping,
  terminal,
  testTaskBody,
  time,
  todayMetrics,
  workspaceStale,
  choiceOf,
  defaultIdleMinutes,
  engineVersion,
  profileChoices,
  sameEngineInMode,
} from './p4-operator-model.js';
import { useAppLocation, usePageActive } from './AppRouteCache.jsx';
import { navigateApp } from './app-navigation.js';
import './v2/ops-adapter.css';

const quiet = (promise) => promise.catch(() => {});
// The service's launch refusals are codes; say what to do about the two common ones.
const LAUNCH_HELP = {
  dstack_no_compatible_offers: 'operator.error.noOffers',
  dstack_no_compatible_host_offers: 'operator.error.noOffers',
  dstack_execution_policy_unconfirmed: 'operator.error.policyUnconfirmed',
};
const launchError = (error, t) => {
  const code = error?.detail?.code || error?.code || String(error?.message || '').trim();
  const key = LAUNCH_HELP[code];
  return key ? t(key) : error?.message || String(error);
};
const label = (profile, locale = 'zh') =>
  profile?.label || profile?.model_id || translate(locale, 'operator.more.modelPending');
const localFuture = () => {
  const date = new Date(Date.now() + 3600000);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};
const keepUntil = (policy) => {
  if (!finite(policy?.hold_until) || policy.hold_until * 1000 <= Date.now()) return localFuture();
  const date = new Date(policy.hold_until * 1000);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};
const countText = (t, key, values) => t(key + (values.count === 1 ? '.one' : ''), values);
// CR43 admin host metadata (#193): country/region from the frozen offer; never inferred.
const hostPlace = (node) =>
  [
    node.host?.country,
    node.host?.region && node.host.region !== node.host?.country ? node.host.region : null,
  ]
    .filter(Boolean)
    .join(' / ') ||
  node.region ||
  '';

// The console's dialogs use the shared Dialog (h3-studio#514); p4-dialog keeps their content layout.
function Dialog({ title, subtitle, onClose, wide = false, children }) {
  return (
    <SharedDialog
      title={title}
      subtitle={subtitle}
      onClose={onClose}
      size={wide ? 'xl' : 'wide'}
      className={'p4-dialog' + (wide ? ' wide' : '')}
    >
      {children}
    </SharedDialog>
  );
}
function KeepFields({ value, onChange }) {
  const { t } = useI18n();
  return (
    <>
      <div className="p4-policies" role="group" aria-label={t('operator.status108')}>
        {[
          ['idle', t('operator.status1')],
          ['until', t('operator.status2')],
          ['until_stopped', t('operator.status3')],
        ].map(([key, text]) => (
          <Button
            key={key}
            aria-pressed={value.keep_on === key}
            onClick={() =>
              onChange({
                ...value,
                keep_on: key,
                idle_minutes: defaultIdleMinutes(key, value.until),
              })
            }
          >
            {text}
          </Button>
        ))}
      </div>
      <div className="p4-policy-extra">
        {value.keep_on === 'until' && (
          <label>
            {t('operator.copy49')}
            <input
              aria-label={t('operator.status2')}
              type="datetime-local"
              value={value.until}
              onChange={(event) => onChange({ ...value, until: event.target.value })}
            />
          </label>
        )}
        {value.keep_on !== 'until_stopped' && (
          <label>
            {t('operator.copy47')}
            <input
              aria-label={t('operator.status109')}
              type="number"
              min="1"
              max="1440"
              value={value.idle_minutes}
              onChange={(event) => onChange({ ...value, idle_minutes: Number(event.target.value) })}
            />
            {t('operator.copy48')}
          </label>
        )}
        {/* Both limits apply: whichever comes first stops the machine. Saying so here stops
            people setting "keep until 21:00" and losing the machine to the 10-minute idle. */}
        <small className="p4-policy-note">
          {t(
            value.keep_on === 'until'
              ? 'operator.keepNote.until'
              : value.keep_on === 'idle'
                ? 'operator.keepNote.idle'
                : 'operator.copy50',
            { minutes: value.idle_minutes },
          )}
        </small>
      </div>
    </>
  );
}
function Login() {
  const { t } = useI18n();
  const c = useCloud(),
    [username, setUsername] = useState(''),
    [password, setPassword] = useState(''),
    [error, setError] = useState('');
  const local = ['username-only-test', 'username-test'].includes(c.auth?.authentication);
  return (
    <section className="ui-empty">
      <ShieldCheck size={30} />
      <h2>{t('operator.copy51')}</h2>
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setError('');
          try {
            await cloud.login(username, password);
          } catch (error) {
            setError(error.message);
          }
        }}
      >
        <label>
          {t('operator.copy52')}
          <input
            autoComplete="username"
            required
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>
        {!local && (
          <label>
            {t('operator.copy53')}
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
        )}
        {error && <p role="alert">{error}</p>}
        <Button
          type="submit"
          className="p4-primary"
          disabled={c.busy || !c.auth || c.auth.auth_ready === false}
        >
          {t('operator.copy54')}
        </Button>
      </form>
    </section>
  );
}
function ProgressObservation({ view, detail = false }) {
  const { t, locale } = useI18n();
  const active = !['stopping', 'stopped'].includes(view.status.id),
    reason = view.reasonText || view.reasonCode;
  if (!reason && !(active && (view.stale || view.stalled)) && view.source) return null;
  return (
    <div className={'p4-progress-diagnostic' + (view.stale || view.stalled ? ' warm' : '')}>
      {reason && <span>{view.reasonText || t('operator.status4') + view.reasonCode}</span>}
      {detail && view.reasonText && view.reasonCode && <code>{view.reasonCode}</code>}
      {active && view.stalled && <span>{t('operator.copy55')}</span>}
      {active && view.stale && (
        <span>
          {t('operator.more.stale', {
            age: finite(view.observationAge)
              ? t('operator.more.ago', { time: elapsed(view.observationAge, locale) })
              : '',
          })}
        </span>
      )}
      {!view.source && <span>{t('operator.copy56')}</span>}
    </div>
  );
}
function ProgressAmount({ value, label }) {
  const { t } = useI18n();
  return (
    <div className="p4-progress-amount">
      <span>
        {label}：{value.label}
      </span>
      {value.percent !== null && (
        <progress aria-label={label + t('operator.status5')} max="100" value={value.percent} />
      )}
    </div>
  );
}
export function MachinePreparation({ node, detail = false }) {
  const { t, locale } = useI18n();
  const view = preparationView(node, locale);
  if (!detail)
    return (
      <div className="p4-progress-summary">
        {(view.status.id === 'downloading_weights' || view.source?.download) && (
          <small>{view.download.label}</small>
        )}
        {view.status.id === 'pulling_image' && (
          <small>
            {view.image.label
              .replace(t('operator.status6'), t('operator.status7'))
              .replace(t('operator.status8'), t('operator.status9'))}
          </small>
        )}
        {finite(view.elapsedSeconds) && (
          <small>
            {view.status.id === 'stopped' ? t('operator.status10') : t('operator.status11')}{' '}
            {elapsed(view.elapsedSeconds, locale)}
            {finite(view.phaseElapsed) &&
              t('operator.status12') + elapsed(view.phaseElapsed, locale)}
          </small>
        )}
        {['awaiting_first_job', 'loading_model', 'ready'].includes(view.status.id) && (
          <small>{view.modelLabel}</small>
        )}
        <ProgressObservation view={view} />
      </div>
    );
  return (
    <section className="p4-preparation" aria-label={t('operator.status110')}>
      <h3>{t('operator.copy57')}</h3>
      <ol className="p4-preparation-steps" aria-label={t('operator.status111')}>
        {preparationSteps(view, locale).map((step) => (
          <li
            key={step.id}
            aria-current={step.current ? 'step' : undefined}
            className={step.current ? 'current' : ''}
          >
            <span>{step.label}</span>
            {step.current && (
              <small>{step.pending ? t('operator.status13') : t('operator.status14')}</small>
            )}
          </li>
        ))}
      </ol>
      <div className="p4-kv p4-progress-times">
        <div>
          <small>
            {view.status.id === 'stopped' ? t('operator.status10') : t('operator.status11')}
          </small>
          {finite(view.elapsedSeconds)
            ? elapsed(view.elapsedSeconds, locale)
            : t('operator.status15')}
        </div>
        <div>
          <small>{t('operator.copy58')}</small>
          {finite(view.phaseElapsed) ? elapsed(view.phaseElapsed, locale) : t('operator.status15')}
        </div>
        <div>
          <small>{t('operator.copy59')}</small>
          {time(view.source?.started_at, locale)}
        </div>
        <div>
          <small>{t('operator.copy60')}</small>
          {time(view.source?.updated_at, locale)}
        </div>
      </div>
      <ProgressAmount label={t('operator.more.weights')} value={view.download} />
      {(view.status.id === 'pulling_image' || view.source?.image) && (
        <ProgressAmount
          label={t('operator.more.image')}
          value={{
            ...view.image,
            label: view.image.label
              .replace(t('operator.status6'), t('operator.status16'))
              .replace(t('operator.status8'), t('operator.status17')),
          }}
        />
      )}
      <div className="p4-model-state">
        <small>{t('operator.copy61')}</small>
        <span>{view.modelLabel}</span>
        {view.modelState === 'not_loaded' && <small>{t('operator.copy62')}</small>}
      </div>
      <ProgressObservation view={view} detail />
    </section>
  );
}
export function MachineRow({
  node,
  profiles = [],
  onDetail,
  onStop,
  onKeep,
  onMove,
  blocked = false,
  admin = false,
}) {
  const { t, locale } = useI18n();
  const view = preparationView(node, locale),
    status = view.status,
    profile = profiles.find(
      (profile) =>
        profile.runtime_profile_id === node.runtime_profile_id && profile.mode === node.mode,
    ),
    fee = rate(node);
  return (
    <div data-ui-surface="row" className={'mrow' + (stopping(node) ? ' stopping' : '')}>
      <Cpu size={22} className="mrow-icon" />
      <Button
        variant="ghost"
        data-ui-surface="row"
        className="mrow-main"
        onClick={() => onDetail(node)}
        title={t('operator.more.machineDetails', { id: node.id?.slice(0, 8) })}
      >
        <b>
          {node.gpu_model ||
            node.gpu_type ||
            profile?.gpu_names?.join(' / ') ||
            dstackProvider(node.provider)}{' '}
          × {node.gpu_count || 1}
          <span className={'mode ' + (node.mode || '')}>{modeLabel(node.mode, locale)}</span>
        </b>
        <small className="muted">
          {dstackProvider(node.provider)}
          {admin && <> · {node.owner_id}</>} · {hostPlace(node) || t('operator.status48')}
          {Number.isFinite(node.host?.advertised_download_mbps) && (
            <>
              {' '}
              · {t('operator.hostMbps', { mbps: Math.round(node.host.advertised_download_mbps) })}
            </>
          )}
          {Number.isFinite(node.completed_content_download?.duration_seconds) && (
            <>
              {' '}
              ·{' '}
              {t('operator.weightsIn', {
                minutes: Math.max(
                  1,
                  Math.round(node.completed_content_download.duration_seconds / 60),
                ),
              })}
            </>
          )}{' '}
          · {fee.label}
          {t('operator.more.perHour')} · <Timer size={11} />
          <span className="p4-keep">{keepText(node, locale)}</span>
        </small>
      </Button>
      <div className="mrow-state">
        <span className={'pill ' + (node.current_job ? 'busy' : status.tone)}>
          {status.id === 'stopped' ? t('operator.status18') : status.label}
        </span>
        <MachinePreparation node={node} />
      </div>
      <div className="mrow-actions">
        {onKeep && (
          <Button
            variant="ghost"
            size="sm"
            disabled={blocked || stopping(node)}
            onClick={() => onKeep(node)}
          >
            {t('operator.copy63')}
          </Button>
        )}
        {onMove && (
          <Button variant="ghost" size="sm" disabled={blocked} onClick={() => onMove(node)}>
            {t('operator.copy85')}
          </Button>
        )}
        {onStop && (
          <Button size="sm" disabled={blocked || stopping(node)} onClick={() => onStop(node)}>
            <Square size={12} />
            {t('operator.copy119')}
          </Button>
        )}
        {!onStop && (
          <Button variant="ghost" size="sm" onClick={() => onDetail(node)}>
            <ChevronRight size={16} />
          </Button>
        )}
      </div>
    </div>
  );
}
function TestTaskField({ value, onChange, disabled = false }) {
  const { t } = useI18n();
  return (
    <label className="qc-field">
      {t('settings.testTaskId')}
      <input
        aria-label={t('settings.testTaskId')}
        maxLength="100"
        pattern="[A-Za-z0-9_.:\-]+"
        placeholder="issue-267"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      />
      <small className="muted">{t('settings.testTaskHelp')}</small>
    </label>
  );
}
function StartDialog({
  options = {},
  profiles,
  groups,
  controller,
  busy,
  account,
  testTaskRequired = false,
  onClose,
}) {
  const { t, locale } = useI18n();
  const [taskId, setTaskId] = useState(
    options.test_task_id || options.node?.lifecycle?.test_gpu_guard?.task_id || '',
  );
  const task = testTaskBody(testTaskRequired, taskId);
  const [rows, setRows] = useState(() =>
      withDefaultPrices(initialRows(profiles, options), profiles),
    ),
    [groupId, setGroupId] = useState(options.groupId || ''),
    [groupName, setGroupName] = useState(''),
    [policy, setPolicy] = useState({
      keep_on: options.node?.lifecycle?.keep_on || 'idle',
      idle_minutes: (options.node?.lifecycle?.idle_shutdown_seconds || 600) / 60,
      until: keepUntil(options.node?.lifecycle),
    }),
    [previews, setPreviews] = useState([]),
    [results, setResults] = useState([]),
    [working, setWorking] = useState(false),
    [error, setError] = useState(''),
    [confirmed, setConfirmed] = useState(false);
  const choices = useMemo(() => profileChoices(profiles), [profiles]);
  const expanded = priceRows(rows, profiles, batchRows(rows, profiles)),
    maxRate = expanded?.reduce((sum, item) => sum + item.max_price_microusd, 0),
    count = expanded?.length || 0;
  const version = useRef(0),
    alive = useRef(true),
    confirmation = useRef(false);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );
  const pricePolicy = JSON.stringify(
    profiles.map((profile) => [
      profile.id,
      profile.price_policy_snapshot,
      profile.max_price_microusd,
      profile.price_ceiling_microusd,
    ]),
  );
  useEffect(() => {
    version.current++;
    setPreviews([]);
  }, [pricePolicy]);
  function changed(fn) {
    version.current++;
    setPreviews([]);
    setError('');
    fn();
  }
  function edit(id, patch) {
    changed(() =>
      setRows((current) => current.map((row) => (row.id === id ? { ...row, ...patch } : row))),
    );
  }
  async function check() {
    const requestPolicy = lifetimeBody(policy);
    if (!expanded || !requestPolicy || !task || (groupId === 'new' && !groupName.trim())) {
      setError(t('operator.more.invalidStart'));
      return;
    }
    const current = version.current;
    setWorking(true);
    setError('');
    setPreviews([]);
    const next = [];
    try {
      for (const item of expanded) {
        const selection = {
          profile_id: item.profile_id,
          max_price_microusd: item.max_price_microusd,
          ...requestPolicy,
          ...task,
          group_id: groupId && groupId !== 'new' ? groupId : null,
        };
        const preview = await controller.preview(selection);
        if (!alive.current || current !== version.current) return;
        if (!previewCurrent(preview, selection)) throw Error(t('operator.more.previewUnknown'));
        next.push({ ...item, selection, preview });
      }
      setPreviews(next);
    } catch (error) {
      if (alive.current) setError(launchError(error, t));
    } finally {
      if (alive.current) setWorking(false);
    }
  }
  async function start() {
    if (confirmation.current || working || !previews.length) return;
    confirmation.current = true;
    setWorking(true);
    setConfirmed(true);
    setError('');
    setResults([]);
    let targetGroup = groupId && groupId !== 'new' ? groupId : null;
    try {
      if (groupId === 'new') {
        const group = await controller.createGroup(groupName.trim());
        targetGroup = group.group_id || group.id || group.group?.id;
        if (!targetGroup) throw Error(t('operator.more.groupUnknown'));
      }
      for (let index = 0; index < previews.length; index++) {
        const item = previews[index];
        if (!alive.current) break;
        try {
          // Each original machine gets a NEW plan/preview. No advisory offer
          // is assigned repeatedly; dstack rechecks and allocates this run.
          const currentPolicy = lifetimeBody(policy);
          if (!currentPolicy) throw Error(t('operator.more.keepExpired'));
          const selection = { ...item.selection, ...currentPolicy, group_id: targetGroup };
          const preview = await controller.preview(selection);
          if (!previewCurrent(preview, selection))
            throw Error(t('operator.more.conditionsUnknown'));
          const receipt = await controller.start(preview);
          setResults((current) => [
            ...current,
            {
              profile_id: item.profile_id,
              index,
              state: 'accepted',
              node_id: receipt.node_id,
              receipt,
            },
          ]);
        } catch (error) {
          const unknown = !!controller.getState().pending;
          setResults((current) => [
            ...current,
            {
              profile_id: item.profile_id,
              index,
              state: unknown ? 'unknown' : 'failed',
              message: error.message,
            },
          ]);
          if (unknown) {
            setError(t('operator.more.startUnknown'));
            break;
          }
        }
      }
    } catch (error) {
      setError(error.message);
    } finally {
      if (alive.current) setWorking(false);
    }
  }
  return (
    <Dialog
      wide
      title={t('operator.copy132')}
      subtitle={t('operator.status112')}
      onClose={() => {
        if (!working) onClose();
      }}
    >
      {testTaskRequired && !confirmed && (
        <TestTaskField
          value={taskId}
          disabled={working || busy}
          onChange={(value) => changed(() => setTaskId(value))}
        />
      )}
      {!confirmed ? (
        <>
          <fieldset disabled={working || busy}>
            <div className="p4-formgrid">
              <label>
                {t('operator.copy64')}
                <select
                  aria-label={t('operator.copy64')}
                  value={groupId}
                  onChange={(event) => changed(() => setGroupId(event.target.value))}
                >
                  <option value="">{t('operator.copy65')}</option>
                  {groups.map((group) => (
                    <option key={group.id} value={group.id}>
                      {group.name}
                    </option>
                  ))}
                  <option value="new">{t('operator.copy66')}</option>
                </select>
              </label>
              {groupId === 'new' && (
                <label>
                  {t('operator.copy67')}
                  <input
                    aria-label={t('operator.copy67')}
                    maxLength="60"
                    value={groupName}
                    onChange={(event) => changed(() => setGroupName(event.target.value))}
                  />
                </label>
              )}
            </div>
            <div className="p4-between">
              <h3>{t('operator.copy68')}</h3>
              <Button
                className="p4-text"
                onClick={() =>
                  changed(() => setRows(withDefaultPrices(initialRows(profiles), profiles)))
                }
              >
                1 FL ＋ 1 REF
              </Button>
            </div>
            {rows.map((row, index) => {
              const selected = profiles.find((profile) => profile.id === row.profile_id),
                choice = choiceOf(choices, row.profile_id),
                engines = choice?.engines || [],
                price = profilePrice(selected),
                priceText =
                  row.price_caps?.[row.profile_id] ?? usdInput(selected?.max_price_microusd);
              return (
                <section className="p4-config" key={row.id}>
                  <header>
                    <b>{t('operator.more.config', { index: index + 1 })}</b>
                    <div>
                      <Button
                        className="p4-text"
                        onClick={() =>
                          changed(() =>
                            setRows((current) => [...current, { ...row, id: crypto.randomUUID() }]),
                          )
                        }
                      >
                        {t('operator.copy69')}
                      </Button>
                      <Button
                        className="p4-text"
                        disabled={rows.length === 1}
                        onClick={() =>
                          changed(() =>
                            setRows((current) => current.filter((item) => item.id !== row.id)),
                          )
                        }
                      >
                        {t('operator.copy70')}
                      </Button>
                    </div>
                  </header>
                  <div className="p4-configfields">
                    <label className="p4-field-wide">
                      {t('operator.copy71')}
                      <select
                        aria-label={t('operator.more.configModel', { index: index + 1 })}
                        value={choice ? choice.group.key : ''}
                        onChange={(event) => {
                          const group = choices.find((item) => item.key === event.target.value);
                          const list =
                            group?.modes.get(selected?.mode) ||
                            [...(group?.modes.values() || [])][0];
                          if (list?.[0]) edit(row.id, { profile_id: list[0].id });
                        }}
                      >
                        {choices.map((group) => (
                          <option key={group.key} value={group.key}>
                            {label(group.modes.values().next().value[0], locale)} ·{' '}
                            {group.gpu_names.join('/')}
                          </option>
                        ))}
                      </select>
                    </label>
                    <div className="p4-field">
                      <span className="p4-field-label">{t('operator.copy72')}</span>
                      <div className="seg p4-seg" role="group">
                        {['fl', 'ref'].map((mode) => {
                          const target = sameEngineInMode(choices, row.profile_id, mode);
                          return (
                            <Button
                              key={mode}
                              aria-pressed={selected?.mode === mode}
                              disabled={!target}
                              title={
                                target
                                  ? modeLabel(mode, locale)
                                  : t('operator.more.modeUnavailable')
                              }
                              onClick={() => target && edit(row.id, { profile_id: target.id })}
                            >
                              {t('operator.mode.short.' + mode)}
                            </Button>
                          );
                        })}
                      </div>
                    </div>
                    <label className="p4-field-narrow">
                      {t('operator.copy73')}
                      <input
                        aria-label={t('operator.more.configCount', { index: index + 1 })}
                        type="number"
                        min="1"
                        max="8"
                        value={row.quantity}
                        onChange={(event) => edit(row.id, { quantity: Number(event.target.value) })}
                      />
                    </label>
                    <label className="p4-field-narrow">
                      {t('operator.copy74')}
                      <input
                        aria-label={t('operator.more.configPrice', { index: index + 1 })}
                        inputMode="decimal"
                        value={priceText}
                        onChange={(event) =>
                          edit(row.id, {
                            price_caps: {
                              ...(row.price_caps || {}),
                              [row.profile_id]: event.target.value,
                            },
                          })
                        }
                      />
                      <small>{t('operator.more.ceiling', { price: money(price?.ceiling) })}</small>
                    </label>
                  </div>
                  {/* Same model and GPU can exist as several engine builds; only the newest
                      usually has a current execution policy, so older ones fail at launch. */}
                  {engines.length > 1 && (
                    <Disclosure
                      className="p4-engine"
                      summary={t('operator.more.engine', {
                        version: engineVersion(selected) || '—',
                      })}
                    >
                      <div className="p4-engine-list">
                        {engines.map((profile, position) => (
                          <Chip
                            key={profile.id}
                            pressed={profile.id === row.profile_id}
                            onClick={() => edit(row.id, { profile_id: profile.id })}
                          >
                            v{engineVersion(profile) || '?'}
                            {position === 0 && (
                              <small className="p4-engine-tag">
                                {t('operator.more.engineNewest')}
                              </small>
                            )}
                          </Chip>
                        ))}
                      </div>
                      <small>{t('operator.more.engineHelp')}</small>
                    </Disclosure>
                  )}
                  <small>
                    {selected && (
                      <>
                        {t('operator.more.spec', {
                          ram: wholeGiB(selected.memory_gib),
                          vram: wholeGiB(selected.gpu_memory_gib),
                          price: money(selected.max_price_microusd),
                        })}
                      </>
                    )}
                  </small>
                </section>
              );
            })}
            <Button
              onClick={() =>
                changed(() =>
                  setRows((current) => [
                    ...current,
                    ...withDefaultPrices(initialRows(profiles, { one: true }), profiles),
                  ]),
                )
              }
            >
              <Plus size={14} />
              {t('operator.copy75')}
            </Button>
            <h3 className="p4-fieldheading">{t('operator.copy63')}</h3>
            <KeepFields value={policy} onChange={(value) => changed(() => setPolicy(value))} />
          </fieldset>
          <OperatorBudgetPanel account={account} compact />
          <div className="p4-note">{t('operator.copy76')}</div>
          <div className="p4-total">
            <b>{countText(t, 'operator.more.total', { count })}</b>
            <strong>
              ≤ {money(maxRate)} {t('operator.more.perHour')}
            </strong>
          </div>
          {previews.length > 0 && (
            <div className="p4-preview">
              <b>{countText(t, 'operator.more.checked', { count: previews.length })}</b>
              <p>
                {t('operator.more.initialHold', {
                  amount: money(
                    previews.reduce((sum, item) => sum + item.preview.reservation_microusd, 0),
                  ),
                })}
              </p>
              <div className="p4-checklist">
                {previews.map((item, index) => (
                  <div key={index}>
                    <span>
                      {index + 1}.{' '}
                      {label(
                        profiles.find((profile) => profile.id === item.profile_id),
                        locale,
                      )}{' '}
                      ·{' '}
                      {modeLabel(
                        profiles.find((profile) => profile.id === item.profile_id)?.mode,
                        locale,
                      )}
                    </span>
                    <small>
                      {countText(t, 'operator.more.availableOffers', {
                        count:
                          item.preview.offers?.filter((offer) => offer.inventory_confirmed)
                            .length ?? '—',
                      })}
                    </small>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      ) : (
        <div className="p4-start-results" aria-live="polite">
          <p>{working ? t('operator.status22') : t('operator.status23')}</p>
          {results.map((result) => (
            <div key={result.index}>
              <b>
                {t('operator.more.machine', { index: result.index + 1 })} ·{' '}
                {modeLabel(
                  profiles.find((profile) => profile.id === result.profile_id)?.mode,
                  locale,
                )}
              </b>
              <span className={'p4-pill ' + (result.state === 'accepted' ? 'good' : 'warm')}>
                {
                  {
                    accepted: t('operator.status24'),
                    failed: t('operator.status25'),
                    unknown: t('operator.status26'),
                  }[result.state]
                }
              </span>
              {result.message && <small>{result.message}</small>}
            </div>
          ))}
          <small>{t('operator.copy77')}</small>
        </div>
      )}
      <FloatingMessage tone="error" text={error ? error : null} />
      <footer>
        <small>{t('operator.copy78')}</small>
        <div>
          <Button disabled={working} onClick={onClose}>
            {confirmed ? t('operator.status27') : t('operator.status28')}
          </Button>
          {!confirmed && (
            <Button
              className="p4-primary"
              disabled={working || busy || !expanded || !lifetimeBody(policy) || !task}
              onClick={previews.length ? start : check}
            >
              {working
                ? t('operator.status29')
                : previews.length
                  ? countText(t, 'operator.more.start', { count: previews.length })
                  : countText(t, 'operator.more.checkStart', { count })}
            </Button>
          )}
        </div>
      </footer>
    </Dialog>
  );
}
function KeepDialog({ target, kind, controller, busy, onClose }) {
  const { t } = useI18n();
  const lifecycle = target.lifecycle || target.nodes?.[0]?.lifecycle,
    [value, setValue] = useState({
      keep_on: lifecycle?.keep_on || 'idle',
      idle_minutes: (lifecycle?.idle_shutdown_seconds || 600) / 60,
      until: keepUntil(lifecycle),
    }),
    [error, setError] = useState(''),
    [receipt, setReceipt] = useState(null),
    [working, setWorking] = useState(false);
  async function save() {
    const body = lifetimeBody(value);
    if (!body) {
      setError(t('operator.more.invalidKeep'));
      return;
    }
    setWorking(true);
    try {
      const result =
        kind === 'group'
          ? await controller.groupHold(target, body)
          : await controller.hold(target, body);
      setReceipt(result);
    } catch (error) {
      setError(error.message);
    } finally {
      setWorking(false);
    }
  }
  return (
    <Dialog
      title={kind === 'group' ? t('operator.status30') : t('operator.status31')}
      subtitle={target.name || target.id?.slice(0, 8)}
      onClose={() => {
        if (!working) onClose();
      }}
    >
      <fieldset disabled={busy || working || !!receipt}>
        <KeepFields value={value} onChange={setValue} />
      </fieldset>
      <Note>
        {kind === 'group' ? t('operator.status32') : ''}
        {t('operator.more.protections')}
      </Note>
      {receipt && <ActionResults receipt={receipt} />}{' '}
      <FloatingMessage tone="error" text={error ? error : null} />
      <footer>
        <span />
        <div>
          <Button disabled={working} onClick={onClose}>
            {receipt ? t('operator.status27') : t('operator.status28')}
          </Button>
          {!receipt && (
            <Button
              className="p4-primary"
              disabled={busy || working || !lifetimeBody(value)}
              onClick={save}
            >
              {t('operator.copy79')}
            </Button>
          )}
        </div>
      </footer>
    </Dialog>
  );
}
function ActionResults({ receipt }) {
  const { t } = useI18n();
  return (
    <div className="p4-preview" role="status">
      <b>{receipt.state === 'partial' ? t('operator.status33') : t('operator.status34')}</b>
      {receipt.results?.map((result) => (
        <p key={result.node_id}>
          {result.node_id.slice(0, 8)} · {result.code || result.state}
        </p>
      ))}
      <small>{t('operator.copy80')}</small>
    </div>
  );
}
function StopDialog({ target, kind, controller, busy, onClose }) {
  const { t } = useI18n();
  const [impact, setImpact] = useState(null),
    [error, setError] = useState(''),
    [loading, setLoading] = useState(true),
    [receipt, setReceipt] = useState(null),
    [working, setWorking] = useState(false);
  useEffect(() => {
    let active = true;
    controller
      .stopImpact(kind, target.id)
      .then((value) => {
        if (active) {
          setImpact(value);
          setLoading(false);
        }
      })
      .catch((error) => {
        if (active) {
          setError(error.message);
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [controller, kind, target.id]);
  async function stop() {
    setWorking(true);
    setError('');
    try {
      // A running node's version moves with every observation (every few seconds), so a stop
      // can lose the race. Retry with a fresh impact while the person's choice still holds:
      // same warnings they confirmed. Anything else goes back to them.
      let current = impact;
      for (let attempt = 0; ; attempt++) {
        try {
          setReceipt(
            kind === 'group'
              ? await controller.groupStop(target, current)
              : await controller.stop(target, current),
          );
          break;
        } catch (error) {
          if (attempt >= 3 || !String(error.message).includes('operator_node_version_conflict'))
            throw error;
          const next = await controller.stopImpact(kind, target.id);
          if ((next?.warnings?.length || 0) !== (current?.warnings?.length || 0)) throw error;
          current = next;
        }
      }
    } catch (error) {
      setError(error.message);
      setImpact(null);
      try {
        setImpact(await controller.stopImpact(kind, target.id));
      } catch {
        // The impact summary is advisory; the dialog works without it.
      }
    } finally {
      setWorking(false);
    }
  }
  return (
    <Dialog
      title={kind === 'group' ? t('operator.status35') : t('operator.status36')}
      subtitle={target.name || target.id?.slice(0, 8)}
      onClose={() => {
        if (!working) onClose();
      }}
    >
      <p>{t('operator.copy81')}</p>
      {loading && <p role="status">{t('operator.copy82')}</p>}
      {impact?.warnings?.map((warning, index) => (
        <div className="p4-warning" role="alert" key={index}>
          <b>
            {countText(t, 'operator.more.waitingLoss', {
              count: warning.waiting_jobs,
              mode: warning.mode.toUpperCase(),
            })}
          </b>
          <p>{t('operator.more.capacityLoss')}</p>
        </div>
      ))}
      {receipt && <ActionResults receipt={receipt} />}{' '}
      <FloatingMessage tone="error" text={error ? error : null} />
      <Note>{t('operator.copy83')}</Note>
      <footer>
        <span />
        <div>
          <Button disabled={working} onClick={onClose}>
            {receipt ? t('operator.status27') : t('operator.status28')}
          </Button>
          {!receipt && (
            <Button
              className="p4-primary"
              disabled={busy || working || loading || !impact}
              onClick={stop}
            >
              {impact?.warnings?.length ? t('operator.status37') : t('operator.status38')}
            </Button>
          )}
        </div>
      </footer>
    </Dialog>
  );
}
export function MachineDetail({
  node,
  profiles,
  groups,
  controller,
  onClose,
  onStop,
  onKeep,
  onAnother,
  onMove,
  admin = false,
}) {
  const { t, locale } = useI18n();
  const [stageStats, setStageStats] = useState(null);
  useEffect(() => {
    let active = true;
    setStageStats(null);
    if (controller)
      controller
        .metrics(node.id)
        .then((value) => {
          if (active) setStageStats(value.metrics);
        })
        .catch(() => {
          if (active) setStageStats({ stage_query_status: 'unavailable' });
        });
    return () => {
      active = false;
    };
  }, [controller, node.id]);
  const status = preparationView(node, locale).status,
    profile = profiles.find(
      (profile) =>
        profile.runtime_profile_id === node.runtime_profile_id && profile.mode === node.mode,
    ),
    metrics = { ...(node.metrics || {}), ...(stageStats || {}) },
    today = todayMetrics(node, locale),
    fee = rate(node),
    usage = node.usage || {};
  return (
    <Dialog
      title={t('operator.status113')}
      subtitle={
        dstackProvider(node.provider) +
        ' · ' +
        node.id?.slice(0, 8) +
        (admin ? ' · ' + node.owner_id : '')
      }
      onClose={onClose}
    >
      <div className="p4-between">
        <span className={'p4-pill ' + status.tone}>{status.label}</span>
        <b>
          {fee.label}
          {t('operator.more.perHour')}
        </b>
      </div>
      <MachinePreparation node={node} detail />
      <div className="p4-kv">
        <div>
          <small>{t('operator.copy84')}</small>
          {label(profile, locale)} ·{' '}
          {node.gpu_model || profile?.gpu_names?.join('/') || t('operator.status39')}
        </div>
        <div>
          <small>{t('operator.copy72')}</small>
          {modeLabel(node.mode, locale)}
        </div>
        <div>
          <small>{t('operator.copy63')}</small>
          {keepText(node, locale)}
        </div>
        <div>
          <small>{t('operator.copy85')}</small>
          {groups.find((group) => group.id === node.group_id)?.name || t('operator.status40')}
          {!admin && (
            <Button className="p4-text p4-link" onClick={() => onMove(node)}>
              {t('operator.copy86')}
            </Button>
          )}
        </div>
      </div>
      {node.current_job && (
        <div className="p4-note">
          {t('operator.more.generating', {
            id: (node.current_job.job_id || node.current_job.id || '').slice(0, 8),
          })}
          {finite(node.current_job.elapsed_seconds) &&
            t('operator.more.elapsed', { time: elapsed(node.current_job.elapsed_seconds, locale) })}
        </div>
      )}
      <h3>{t('operator.copy87')}</h3>
      <div className="p4-metrics">
        <div>
          <small>{t('operator.copy88')}</small>
          <strong>
            {finite(metrics.average_generate_seconds)
              ? t('operator.more.secondsPerVideo', {
                  seconds: Math.round(metrics.average_generate_seconds),
                })
              : t('operator.status41')}
          </strong>
        </div>
        <div>
          <small>
            {t('operator.more.perVideo')} · {costLabel(metrics.generation_cost_basis, locale)}
          </small>
          <strong>{money(metrics.average_generation_cost_microusd)}</strong>
        </div>
        <div>
          <small>
            {t('operator.more.todayCost')} · {today.basis}
          </small>
          <strong>{today.cost}</strong>
        </div>
        <div>
          <small>{t('operator.copy89')}</small>
          <strong>
            {today.count}
            <em>{t('operator.copy90')}</em>
          </strong>
        </div>
      </div>
      {finite(metrics.average_completion_seconds) && (
        <p className="p4-meta">
          {countText(t, 'operator.more.submissionTiming', {
            seconds: Math.round(metrics.average_completion_seconds),
            count: metrics.samples ?? '—',
          })}
        </p>
      )}
      {finite(metrics.average_generate_seconds) && (
        <p className="p4-meta">
          {countText(t, 'operator.more.stageSamples', {
            count: metrics.generate_samples ?? '—',
            start: time(metrics.sample_window_start, locale),
            end: time(metrics.sample_window_end, locale),
          })}
        </p>
      )}
      <Disclosure className="p4-method" summary={t('operator.copy91')}>
        <p>{t('operator.copy92')}</p>
        <p>
          {metrics.generation_basis || metrics.basis || t('operator.status42')} ·{' '}
          {usage.timezone || 'Australia/Sydney'}
          {finite(metrics.window_start) &&
            t('operator.more.window', {
              start: time(metrics.window_start, locale),
              end: time(metrics.window_end, locale),
            })}
        </p>
      </Disclosure>
      {terminal(node) && (
        <div className="p4-kv">
          <div>
            <small>{t('operator.copy93')}</small>
            {elapsed(usage.runtime_seconds, locale)}
          </div>
          <div>
            <small>
              {t('operator.more.totalCost')} · {costLabel(usage.cost_basis, locale)}
            </small>
            {money(usage.total_cost_microusd)}
          </div>
          <div>
            <small>{t('operator.copy94')}</small>
            {countText(t, 'operator.more.videos', { count: usage.video_count ?? '—' })}
          </div>
        </div>
      )}
      {admin && node.reason_code && (
        <Disclosure className="p4-method" summary={t('operator.copy95')}>
          <code>{node.reason_code}</code>
          <p>{t('operator.copy96')}</p>
        </Disclosure>
      )}
      <footer>
        <Button disabled={terminal(node) || stopping(node)} onClick={() => onStop(node)}>
          {t('operator.copy97')}
        </Button>
        <div>
          {!admin && !terminal(node) && (
            <Button disabled={stopping(node)} onClick={() => onKeep(node)}>
              <Clock size={14} />
              {t('operator.copy63')}
            </Button>
          )}
          {!admin && (
            <Button className="p4-primary" onClick={() => onAnother(node)}>
              {t('operator.copy98')}
            </Button>
          )}
        </div>
      </footer>
    </Dialog>
  );
}
function MoveDialog({ node, groups, controller, busy, onClose }) {
  const { t } = useI18n();
  const [target, setTarget] = useState(node.group_id || ''),
    [error, setError] = useState('');
  return (
    <Dialog title={t('operator.status114')} onClose={onClose}>
      <label>
        {t('operator.copy85')}
        <select
          aria-label={t('operator.status114')}
          value={target}
          onChange={(event) => setTarget(event.target.value)}
        >
          <option value="">{t('operator.copy65')}</option>
          {groups.map((group) => (
            <option key={group.id} value={group.id}>
              {group.name}
            </option>
          ))}
        </select>
      </label>
      <Note>{t('operator.copy99')}</Note>
      <FloatingMessage tone="error" text={error ? error : null} />
      <footer>
        <span />
        <Button
          className="p4-primary"
          disabled={busy}
          onClick={async () => {
            try {
              await controller.move(node, target || null);
              onClose();
            } catch (error) {
              setError(error.message);
            }
          }}
        >
          {t('operator.copy79')}
        </Button>
      </footer>
    </Dialog>
  );
}
export function InventoryAdvisory({ item }) {
  const { t, locale } = useI18n();
  const outcome = inventoryOutcome(item, locale),
    profile = item.profile;
  if (outcome.state !== 'empty')
    return item.error ? <p className="p4-error">{item.error}</p> : null;
  return (
    <Note role="status">
      {outcome.message}
      <br />
      <small>
        {t('operatorPanel.inventoryFilters', {
          ram: wholeGiB(profile.memory_gib),
          vram: wholeGiB(profile.gpu_memory_gib),
          cpu: profile.cpu_count ?? '—',
          disk: wholeGiB(profile.disk_gib),
          price: money(profile.max_price_microusd),
        })}
      </small>
    </Note>
  );
}
function Inventory({ profiles, controller, onStart, testTaskRequired = false }) {
  const { t, locale } = useI18n();
  const [taskId, setTaskId] = useState(''),
    task = testTaskBody(testTaskRequired, taskId);
  const models = [...new Set(profiles.map((profile) => profile.model_id))],
    [model, setModel] = useState(models[0] || ''),
    [mode, setMode] = useState('fl'),
    [items, setItems] = useState([]),
    [pending, setPending] = useState(false),
    generation = useRef(0),
    alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
      generation.current++;
    },
    [],
  );
  function change(fn) {
    generation.current++;
    setItems([]);
    setPending(false);
    fn();
  }
  async function scan() {
    if (!task) return;
    const current = ++generation.current;
    const choices = profiles.filter(
      (profile) => profile.model_id === model && profile.mode === mode,
    );
    setItems(choices.map((profile) => ({ profile, state: 'pending' })));
    setPending(true);
    await Promise.allSettled(
      choices.map(async (profile) => {
        let patch;
        try {
          const preview = await controller.preview({
            profile_id: profile.id,
            keep_on: 'idle',
            hold_seconds: 0,
            group_id: null,
            ...task,
          });
          patch = { profile, preview, state: 'ready' };
        } catch (error) {
          patch = { profile, ...inventoryError(error) };
        }
        if (alive.current && generation.current === current)
          setItems((previous) =>
            previous.map((item) => (item.profile.id === profile.id ? patch : item)),
          );
      }),
    );
    if (alive.current && generation.current === current) setPending(false);
  }
  return (
    <>
      {testTaskRequired && (
        <TestTaskField value={taskId} onChange={(value) => change(() => setTaskId(value))} />
      )}
      <section className="p4-panel p4-inventorybar">
        <label>
          {t('operator.copy100')}
          <select
            aria-label={t('operator.status115')}
            value={model}
            onChange={(event) => change(() => setModel(event.target.value))}
          >
            {models.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('operator.copy72')}
          <select
            aria-label={t('operator.status116')}
            value={mode}
            onChange={(event) => change(() => setMode(event.target.value))}
          >
            <option value="fl">{t('operator.copy101')}</option>
            <option value="ref">{t('operator.copy102')}</option>
            <option
              value="both"
              disabled={
                !profiles.some((profile) => profile.mode === 'both' && profile.model_id === model)
              }
            >
              {t('operator.copy103')}
            </option>
          </select>
        </label>
        <Button className="p4-primary" disabled={pending || !model || !task} onClick={scan}>
          <RefreshCw size={15} />
          {pending ? t('operator.status43') : t('operator.status44')}
        </Button>
      </section>
      <p className="p4-meta">{t('operator.copy104')}</p>
      <div className="p4-offers">
        {items.flatMap((item) => {
          const groups = referenceHostOffers(item.preview?.offers || [], item.profile.id),
            outcome = inventoryOutcome(item, locale),
            fresh = finite(item.preview?.expires_at) && item.preview.expires_at * 1000 > Date.now();
          return (
            groups.length ? groups : [{ key: 'pending', offer: {}, count: 0, confirmed: 0 }]
          ).map((group) => {
            const offer = group.offer,
              allowed = fresh && item.preview?.can_start === true && group.confirmed > 0;
            return (
              <section className="p4-panel p4-offer" key={item.profile.id + group.key}>
                <span className={'p4-pill ' + (allowed ? 'good' : 'muted')}>
                  {dstackProvider(item.profile.backend)} ·{' '}
                  {outcome.state === 'pending'
                    ? outcome.label
                    : outcome.state === 'empty' || outcome.state === 'unknown'
                      ? outcome.label
                      : allowed
                        ? t('operator.status45')
                        : group.confirmed
                          ? t('operator.status46')
                          : t('operator.status47')}
                </span>
                <h2>
                  {offer.gpu_type || item.profile.gpu_names.join('/')} × {offer.gpu_count || 1}
                </h2>
                <small>
                  {label(item.profile, locale)} · {modeLabel(item.profile.mode, locale)}
                </small>
                <div className="p4-offer-price">
                  {outcome.state === 'empty'
                    ? '≤ ' + money(item.profile.max_price_microusd)
                    : money(offer.hourly_cost_microusd)}
                  <small>
                    {t(
                      outcome.state === 'empty'
                        ? 'operatorPanel.inventoryCapPerHour'
                        : 'operator.copy105',
                    )}
                  </small>
                </div>
                <div className="p4-kv">
                  <div>
                    <small>{t('operator.copy106')}</small>
                    {wholeGiB(offer.memory_gib ?? item.profile.memory_gib)} GiB
                  </div>
                  <div>
                    <small>{t('operator.copy107')}</small>
                    {wholeGiB(offer.gpu_memory_gib ?? item.profile.gpu_memory_gib)} GiB
                  </div>
                  <div>
                    <small>{t('operator.copy108')}</small>
                    {offer.cpu_count ?? '—'}
                  </div>
                  <div>
                    <small>{t('operator.copy109')}</small>
                    {wholeGiB(offer.disk_gib)} GiB
                  </div>
                  <div>
                    <small>{t('operator.copy110')}</small>
                    {countText(t, 'operator.more.offerCount', {
                      count: group.count || '—',
                      available:
                        group.count > 0
                          ? t('operator.more.offerAvailable', { count: group.confirmed })
                          : '',
                    })}
                  </div>
                  <div>
                    <small>{t('operator.copy111')}</small>
                    {offer.region || t('operator.status48')}
                  </div>
                </div>
                <InventoryAdvisory item={item} />
                {group.members?.length > 0 && (
                  <OfferHostHistory members={group.members} profileId={item.profile.id} />
                )}
                <small>{t('operator.copy112')}</small>
                <footer>
                  <span>{t('operator.copy113')}</span>
                  <Button
                    className="p4-primary"
                    disabled={!allowed}
                    onClick={() =>
                      onStart({ profileId: item.profile.id, one: true, groupId: null, ...task })
                    }
                  >
                    {t('operator.copy114')}
                  </Button>
                </footer>
              </section>
            );
          });
        })}
      </div>
      {!items.length && (
        <section className="ui-empty">
          <Server size={26} />
          <p>{t('operator.copy115')}</p>
        </section>
      )}
    </>
  );
}
function AdminView({ snapshot, profiles, onDetail, onStop }) {
  const { t, locale } = useI18n();
  if (!snapshot)
    return (
      <section className="ui-empty" role="status">
        {t('operator.copy116')}
      </section>
    );
  return (
    <>
      <h2>{t('operator.copy117')}</h2>
      <p className="p4-meta">{t('operator.copy118')}</p>
      <div className="p4-accountspend">
        {(snapshot.accounts || []).map((account) => (
          <section className="p4-panel" key={account.owner_id || account.account}>
            <h3>{account.owner_id || account.account}</h3>
            <strong>{money(account.today_cost_microusd ?? account.cost_microusd)}</strong>
            <small>
              {t('operator.more.todaySpend')} ·{' '}
              {costLabel(account.today_cost_basis || account.cost_basis, locale)}
            </small>
            {account.spend_caps?.map((cap, index) => (
              <small key={index}>
                {t('operator.more.accountSpend', {
                  spent: money(cap.spent_microusd),
                  limit: money(cap.limit_microusd),
                  reserved: money(cap.reserved_microusd),
                })}
              </small>
            ))}
            {finite(account.reserved_microusd) && (
              <small>
                {t('operator.more.reserved', { amount: money(account.reserved_microusd) })}
              </small>
            )}
          </section>
        ))}
      </div>
      <div className="p4-panel">
        {snapshot.nodes?.length ? (
          snapshot.nodes.map((node) => (
            <div className="p4-admin-machine" key={node.id}>
              <MachineRow node={node} profiles={profiles} onDetail={onDetail} admin />
              <Button disabled={terminal(node) || stopping(node)} onClick={() => onStop(node)}>
                {t('operator.copy119')}
              </Button>
            </div>
          ))
        ) : (
          <div className="ui-empty ui-empty-compact">{t('operator.copy120')}</div>
        )}
      </div>
      <Disclosure className="p4-method" summary={t('operator.copy121')}>
        <p>{t('operator.copy122')}</p>
        {snapshot.reviews?.map((row) => (
          <p key={row.node_id || row.id}>
            {row.owner_id} · {row.node_id?.slice(0, 8)} · {row.reason_code || row.state}
          </p>
        ))}
        <a href="/operator?legacy=1">{t('operator.copy123')}</a>
      </Disclosure>
    </>
  );
}
const LegacyOperator = React.lazy(() => import('./OperatorConsole.jsx'));
export default function P4OperatorConsole() {
  const { t, locale } = useI18n();
  const pageActive = usePageActive(),
    location = useAppLocation(),
    ownLocation = useRef(location);
  if (pageActive && location.path === '/operator') ownLocation.current = location;
  useEffect(() => {
    if (pageActive) document.title = t('operator.pageTitle');
  }, [locale, pageActive, t]);
  const c = useCloud(),
    controller = useMemo(() => createP4OperatorController({ locale: readBrowserLocale }), []),
    state = useSyncExternalStore(controller.subscribe, controller.getState, controller.getState),
    [view, setView] = useState('machines'),
    [dialog, setDialog] = useState(null),
    [, setNow] = useState(Date.now());
  const legacyView =
    state.snapshot?.operator?.admin === true &&
    new URLSearchParams(ownLocation.current.search).get('legacy') === '1';
  useEffect(() => {
    quiet(cloud.initialize());
    return () => controller.destroy();
  }, [controller]);
  useEffect(() => {
    setView('machines');
    setDialog(null);
    void controller.setAccount(c.account);
  }, [c.account, controller]);
  useEffect(() => {
    if (!pageActive || legacyView) return;
    let stopped = false,
      timer;
    const tick = async () => {
      setNow(Date.now());
      if (document.visibilityState !== 'hidden')
        try {
          await controller.poll(view === 'admin');
        } catch {
          // A failed refresh already shows in the controller state; the next poll retries.
        }
      // 15 s: each poll rebuilds every machine's projection on the small CPU host.
      if (!stopped) timer = setTimeout(tick, 15000);
    };
    timer = setTimeout(tick, 15000);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [controller, c.account, view, pageActive, legacyView]);
  const hasSnapshot = !!state.snapshot,
    isAdmin = state.snapshot?.operator?.admin === true,
    adminDialog = !!dialog?.admin;
  useEffect(() => {
    if (state.denied || !hasSnapshot || (adminDialog && !isAdmin)) {
      setDialog(null);
      if (state.denied || !isAdmin)
        setView((current) => (current === 'admin' ? 'machines' : current));
    }
  }, [state.denied, hasSnapshot, isAdmin, adminDialog]);
  const snapshot = state.snapshot,
    profiles = state.catalog?.profiles || [],
    groups = snapshot?.groups || [],
    nodes = snapshot?.nodes || [],
    busy = state.busy || !!state.pending,
    enabled = snapshot?.enabled === true && state.catalog?.enabled === true,
    live = nodes.filter((node) => !terminal(node)),
    buckets = groupedNodes(nodes, groups, locale),
    queue = snapshot?.queue || [],
    admin = !!snapshot?.operator?.admin,
    selectedSnapshot = dialog?.admin ? state.adminSnapshot : snapshot,
    selected = dialog?.node
      ? (selectedSnapshot?.nodes || []).find((node) => node.id === dialog.node.id) || dialog.node
      : null;
  const currentTarget = dialog?.target
    ? dialog.kind === 'group'
      ? dialog.target
      : (dialog.admin ? state.adminSnapshot?.nodes : nodes)?.find(
          (node) => node.id === dialog.target.id,
        ) || dialog.target
    : null;
  const stale = workspaceStale(snapshot),
    blocked = busy || stale;
  const onDetail = (node, adminMode = false) =>
      setDialog({ type: 'detail', node, admin: adminMode }),
    onStop = (node, adminMode = false) =>
      setDialog({ type: 'stop', kind: 'node', target: node, admin: adminMode });
  function start(options = {}) {
    setDialog({ type: 'start', options: { ...options, groupId: options.groupId || null } });
  }
  function another(node) {
    const match = profiles.find(
      (profile) =>
        profile.runtime_profile_id === node.runtime_profile_id &&
        profile.mode === node.mode &&
        dstackProvider(profile.backend) === dstackProvider(node.provider),
    );
    if (match) start({ profileId: match.id, one: true, groupId: node.group_id || null, node });
  }
  async function showAdmin() {
    setView('admin');
    try {
      await controller.readAdmin();
    } catch {
      // A failed refresh already shows in the controller state; the next poll retries.
    }
  }
  if (legacyView)
    return (
      <React.Suspense fallback={<p>{t('operator.copy124')}</p>}>
        <LegacyOperator />
      </React.Suspense>
    );
  // An admin reading these tiles is deciding for the whole service: counting only their own
  // machines showed "0 running" while another account had a machine busy (owner, 2026-10-22).
  const adminNodes = admin ? state.adminSnapshot?.nodes || null : null,
    adminLive = adminNodes?.filter((node) => !terminal(node)) || null,
    fleetLive = adminLive || live,
    othersLive = adminLive
      ? adminLive.filter((node) => node.owner_id && node.owner_id !== c.account)
      : [],
    otherOwners = [...new Set(othersLive.map((node) => node.owner_id))];
  const totalHourly = live.every((node) => finite(node.hourly_cost_microusd))
    ? live.reduce((sum, node) => sum + node.hourly_cost_microusd, 0)
    : null;
  const totalCost = nodes.every((node) => finite(node.usage?.total_cost_microusd))
    ? nodes.reduce((sum, node) => sum + node.usage.total_cost_microusd, 0)
    : null;
  return (
    <div className="home ops-adapter">
      <StudioHeader
        account={c.account}
        active="operator"
        menuItems={admin && <MenuItem onSelect={showAdmin}>{t('operator.copy130')}</MenuItem>}
      />
      <main className="home-main ops-gpu-page">
        <div className="home-head p4-page-heading">
          <h1 className="page-title p4-page-title">
            <Cpu size={20} />
            {t('v2.gpus')}
          </h1>
          <small className="muted p4-observed-at">
            {snapshot ? time(snapshot.observed_at, locale) : '—'}
          </small>
          <Button
            variant="ghost"
            size="sm"
            className="p4-page-refresh"
            title={t('operator.status118')}
            aria-label={t('operator.status118')}
            disabled={state.loading || !c.account}
            onClick={() => quiet(controller.refresh(view === 'admin'))}
          >
            <RefreshCw size={13} />
            {t('operator.status118')}
          </Button>
          <div className="spacer" />
          <Button
            variant="primary"
            className="p4-page-launch"
            disabled={blocked || !enabled || !profiles.length}
            onClick={() => start()}
          >
            <Plus size={16} />
            {t('operator.copy132')}
          </Button>
        </div>
        {state.error && !state.denied && <p role="alert">{state.error}</p>}
        {stale && (
          <p className="ops-warning" role="status">
            {t('operator.copy134')}
          </p>
        )}
        {state.pending && (
          <section className="card-box ops-warning" role="alert">
            <b>
              {state.pending.method === 'cancelJobs'
                ? t('operator.status50')
                : t('operator.status51')}
            </b>
            <p>
              {state.pending.method === 'cancelJobs'
                ? t('operator.status52')
                : t('operator.status53')}
            </p>
            <Button disabled={state.busy} onClick={() => quiet(controller.recover())}>
              {t('operator.copy135')}
            </Button>
          </section>
        )}
        {!c.account ? (
          <Login />
        ) : state.denied ? (
          <EmptyState
            icon={<ShieldCheck size={22} />}
            title={t('operator.copy136')}
            action={<a href="/quick-chat">{t('operator.copy137')}</a>}
          />
        ) : !snapshot ? (
          <EmptyState
            role="status"
            icon={<Cpu size={22} />}
            title={state.loading ? t('operator.status54') : t('operator.status55')}
          />
        ) : (
          <>
            <nav className="seg ops-page-tabs" aria-label={t('operator.status119')}>
              {[
                ['machines', 'operator.status61'],
                ['find', 'operator.status62'],
                ['hosts', 'operator.status63'],
                ['ended', 'operator.status18'],
                ['budget', 'operator.status64'],
              ].map(([id, key]) => (
                <Button
                  className={view === id ? 'on' : ''}
                  key={id}
                  aria-current={view === id ? 'page' : undefined}
                  onClick={() => setView(id)}
                >
                  {t(key)}
                </Button>
              ))}
              {admin && (
                <Button
                  className={view === 'admin' ? 'on' : ''}
                  aria-current={view === 'admin' ? 'page' : undefined}
                  onClick={showAdmin}
                >
                  <ShieldCheck size={13} />
                  {t('operator.copy139')}
                </Button>
              )}
            </nav>
            <div className="stats five">
              <div>
                <small>{t('operator.status56')}</small>
                <b>
                  {stale
                    ? '—'
                    : fleetLive.filter((node) => phase(node, locale).id === 'ready').length}
                </b>
              </div>
              <div>
                <small>{t('operator.status58')}</small>
                <b>
                  {stale
                    ? '—'
                    : fleetLive.filter((node) => phase(node, locale).id === 'running').length}
                </b>
              </div>
              <div>
                <small>{t('operator.status57')}</small>
                <b>
                  {stale
                    ? '—'
                    : fleetLive.filter(
                        (node) =>
                          !['ready', 'running', 'stopping'].includes(phase(node, locale).id),
                      ).length}
                </b>
              </div>
              <div>
                <small>{t('operator.status59')}</small>
                <b>{stale ? '—' : money(totalHourly)}</b>
                <small className="muted">{t('operator.status60')}</small>
              </div>
              <div>
                <small>{t('operator.more.totalCost')}</small>
                <b>{money(totalCost)}</b>
                <Button
                  variant="link"
                  className="keep-label"
                  onClick={() => navigateApp('/settings?tab=budget')}
                >
                  {t('settings.budget')}
                </Button>
              </div>
            </div>
            {admin && !adminNodes && (
              <p className="p4-fleet-note muted">
                <Button variant="link" onClick={showAdmin}>
                  {t('operator.fleet.load')}
                </Button>
              </p>
            )}
            {admin && othersLive.length > 0 && (
              <p className="p4-fleet-note" role="status">
                {t('operator.fleet.others', {
                  count: othersLive.length,
                  owners: otherOwners.join('、'),
                })}{' '}
                <Button variant="link" onClick={showAdmin}>
                  {t('operator.fleet.view')}
                </Button>
              </p>
            )}
            {view === 'machines' && (
              <>
                <div className="queues">
                  {['fl', 'ref'].map((mode) => {
                    const item = queue.find((item) => item.mode === mode);
                    return (
                      <div key={mode} className={'queue ' + mode}>
                        <div>
                          <span className={'mode ' + mode}>{modeLabel(mode, locale)}</span>
                          <b>{item?.waiting ?? '—'}</b>
                          <small>{t('operator.copy140')}</small>
                        </div>
                        <small className="muted">
                          {item ? queueLabel(item, locale) : t('operator.status65')}
                        </small>
                        {!!item?.waiting && (
                          <Button
                            variant="primary"
                            size="sm"
                            disabled={
                              blocked ||
                              !enabled ||
                              !profiles.some((profile) => profile.mode === mode)
                            }
                            onClick={() => start({ mode, one: true, groupId: null })}
                          >
                            {t('operator.copy141')}
                          </Button>
                        )}
                        <small className="auto">
                          {t('operator.more.queueCounts', {
                            busy: item?.busy ?? '—',
                            ready: item?.ready ?? '—',
                            preparing: item?.preparing ?? '—',
                          })}
                        </small>
                      </div>
                    );
                  })}
                </div>
                {buckets
                  .filter((group) => group.nodes.length || group.id)
                  .map((group) => (
                    <section
                      className="mgroup"
                      key={group.id || 'ungrouped'}
                      id={'p4-group-' + group.id}
                    >
                      <header className="row tight">
                        <h3 className="sect">{group.name}</h3>
                        <small className="muted">
                          {countText(t, 'operator.more.groupPrice', {
                            count: group.nodes.length,
                            price: money(
                              group.nodes.every((node) => finite(node.hourly_cost_microusd))
                                ? group.nodes.reduce(
                                    (sum, node) => sum + node.hourly_cost_microusd,
                                    0,
                                  )
                                : null,
                            ),
                          })}
                        </small>
                        <div className="spacer" />
                        <Button
                          size="sm"
                          disabled={blocked || !enabled}
                          onClick={() => start({ groupId: group.id, one: true })}
                        >
                          <Plus size={13} />
                          {t('operator.copy142')}
                        </Button>
                        {group.id && group.nodes.length > 0 && (
                          <>
                            <Button
                              size="sm"
                              disabled={blocked}
                              onClick={() =>
                                setDialog({ type: 'keep', kind: 'group', target: group })
                              }
                            >
                              {t('operator.copy143')}
                            </Button>
                            <Button
                              variant="danger"
                              size="sm"
                              disabled={blocked || !group.nodes.some((node) => !stopping(node))}
                              onClick={() =>
                                setDialog({ type: 'stop', kind: 'group', target: group })
                              }
                            >
                              <Square size={12} />
                              {t('operator.copy144')}
                            </Button>
                          </>
                        )}
                      </header>
                      <div className="machines-list">
                        {group.nodes.length ? (
                          group.nodes.map((node) => (
                            <MachineRow
                              key={node.id}
                              node={node}
                              profiles={profiles}
                              blocked={blocked}
                              onDetail={onDetail}
                              onStop={onStop}
                              onKeep={(node) =>
                                setDialog({ type: 'keep', kind: 'node', target: node })
                              }
                              onMove={(node) => setDialog({ type: 'move', node })}
                            />
                          ))
                        ) : (
                          <div className="scene-empty">{t('operator.copy145')}</div>
                        )}
                      </div>
                    </section>
                  ))}
                {!live.length && !buckets.some((group) => group.id) && (
                  <div className="scene-empty">{t('operator.copy145')}</div>
                )}
              </>
            )}
            {view === 'budget' && (
              <BudgetWorkspace
                account={c.account}
                controller={controller}
                state={state}
                busy={blocked}
              />
            )}
            {view === 'find' && (
              <Inventory
                profiles={profiles}
                controller={controller}
                onStart={start}
                testTaskRequired={state.catalog?.test_task_id_required === true}
              />
            )}
            {view === 'hosts' && (
              <OperatorHostsPanel
                key={state.account + ':owner'}
                controller={controller}
                state={state}
                busy={blocked}
              />
            )}
            {view === 'ended' && (
              <div className="card-box">
                {nodes.filter(terminal).length ? (
                  nodes.filter(terminal).map((node) => (
                    <Button
                      className="mrow ended-row"
                      data-ui-surface="row"
                      key={node.id}
                      onClick={() => onDetail(node)}
                    >
                      <Cpu size={18} className="mrow-icon" />
                      <div className="mrow-main">
                        <b>
                          {dstackProvider(node.provider)} · {modeLabel(node.mode, locale)}
                        </b>
                        <small className="muted">
                          {/* When it ran comes first: an old rental read as today's is what
                              made the owner think a running machine had been stopped. */}
                          {finite(node.preparation?.started_at)
                            ? time(node.preparation.started_at, locale)
                            : t('operator.status15')}{' '}
                          · {elapsed(node.usage?.runtime_seconds, locale)} ·{' '}
                          {money(node.usage?.total_cost_microusd)} ·{' '}
                          {costLabel(node.usage?.cost_basis, locale)}
                        </small>
                      </div>
                    </Button>
                  ))
                ) : (
                  <p className="muted">{t('operator.copy146')}</p>
                )}
              </div>
            )}
            {view === 'admin' && admin && (
              <>
                <OperatorHostsPanel
                  key={state.account + ':admin'}
                  controller={controller}
                  state={state}
                  admin
                  busy={blocked}
                />
                <DemandStartPanel controller={controller} state={state} busy={blocked} />
                <OperatorJobsPanel controller={controller} state={state} busy={blocked} />
                <AdminBudgetPanel controller={controller} state={state} busy={blocked} />
                <AdminView
                  snapshot={state.adminSnapshot}
                  profiles={profiles}
                  onDetail={(node) => onDetail(node, true)}
                  onStop={(node) => onStop(node, true)}
                />
              </>
            )}
          </>
        )}
        <p className="home-foot muted">
          {t('operator.copy138')} {t('operator.copy147')}
        </p>
      </main>
      {snapshot && !state.denied && dialog?.type === 'start' && (
        <StartDialog
          options={dialog.options}
          profiles={profiles}
          groups={groups}
          controller={controller}
          busy={blocked}
          account={c.account}
          testTaskRequired={state.catalog?.test_task_id_required === true}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog?.type === 'detail' && selected && (
        <MachineDetail
          node={selected}
          controller={controller}
          profiles={profiles}
          groups={selectedSnapshot?.groups || groups}
          admin={dialog.admin}
          onClose={() => setDialog(null)}
          onStop={(node) => onStop(node, dialog.admin)}
          onKeep={(node) => setDialog({ type: 'keep', kind: 'node', target: node })}
          onAnother={another}
          onMove={(node) => setDialog({ type: 'move', node })}
        />
      )}
      {dialog?.type === 'keep' && (
        <KeepDialog
          kind={dialog.kind}
          target={currentTarget}
          controller={controller}
          busy={blocked}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog?.type === 'stop' && (
        <StopDialog
          kind={dialog.kind}
          target={currentTarget}
          controller={controller}
          busy={blocked}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog?.type === 'move' && selected && (
        <MoveDialog
          node={selected}
          groups={groups}
          controller={controller}
          busy={blocked}
          onClose={() => setDialog(null)}
        />
      )}
    </div>
  );
}
