import React, { useEffect, useMemo, useState, useSyncExternalStore, useEffectEvent } from 'react';
import {
  Activity,
  BarChart3,
  AlertTriangle,
  Cpu,
  History,
  LayoutDashboard,
  ListChecks,
  RefreshCw,
  ShieldCheck,
  TrendingUp,
  Users,
} from 'lucide-react';
import StudioHeader from './StudioHeader.jsx';
import CapacitySettings from './CapacitySettings.jsx';
import { AdminBudgetPanel } from './OperatorBudgetPanel.jsx';
import { createP4OperatorController } from './p4-operator-controller.js';
import { readBrowserLocale } from './i18n/locale.js';
import StoryChatAdminSwitch from './StoryChatAdminSwitch.jsx';
import { AdminAudit, AdminPricing, AdminUsers, useClient } from './AdminConsole.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { navigateApp } from './app-navigation.js';
import { useAppLocation, usePageActive } from './AppRouteCache.jsx';
import { readAccessFor } from './account-access.js';
import { Badge, EmptyState, Note } from './ui/Note.jsx';
import { Button, IconButton } from './ui/Button.jsx';
import { SideNav, SideNavItem } from './ui/SideNav.jsx';
import { Spinner } from './ui/Field.jsx';
import { Segmented } from './ui/Segmented.jsx';
import { FloatingMessage } from './floating-notice.js';
import { cancellationBody } from './operator-jobs-model.js';
import {
  alerts as findAlerts,
  duration,
  jobRows,
  machineRows,
  reviewRows,
  spendSummary,
  usd,
} from './admin-overview-model.js';
import './admin-home.css';

const SECTIONS = [
  ['overview', 'adminHome.overview', LayoutDashboard],
  ['jobs', 'adminHome.jobs', ListChecks],
  ['limits', 'adminHome.limits', ShieldCheck],
  ['users', 'adminHome.users', Users],
  ['pricing', 'adminHome.pricing', TrendingUp],
  ['log', 'adminHome.log', History],
  ['stats', 'adminHome.stats', BarChart3],
];
const SETTINGS_PATH = '/v1/operator/admin/spending-settings';

// Everything the overview and jobs pages read, refreshed together every 30 s.
// Only the overview needs the (heavy) all-account machine read; the jobs page reads jobs.
// Refresh every 60 s while the tab is visible (the CPU host is small, h3-studio#555).
function useAdminData(api, active, section) {
  const [data, setData] = useState({ state: 'loading' });
  const needsMachines = section === 'overview';
  // Each read shows as soon as it arrives: the live machine read can take far longer than the
  // job list, and the page should not wait for the slowest one. Older data stays until replaced.
  const load = async () => {
    if (globalThis.document?.visibilityState === 'hidden') return;
    const merge = (patch) => setData((old) => ({ ...old, ...patch, at: Date.now() / 1000 }));
    const jobs = api.request('/v1/operator/admin/jobs?limit=100').then(
      (result) => merge({ state: 'ready', jobs: result.jobs || [] }),
      (failure) =>
        setData((old) => ({
          ...old,
          state: failure.status === 403 ? 'forbidden' : old.jobs ? 'stale' : 'error',
          error: failure.message,
        })),
    );
    const extra = needsMachines
      ? [
          api.request('/v1/operator/dstack/admin/workspace?live=1').then(
            (workspace) => merge({ workspace, machinesFailed: false }),
            () => merge({ machinesFailed: true }),
          ),
          api.request(SETTINGS_PATH).then(
            (settings) => merge({ settings }),
            () => {},
          ),
        ]
      : [];
    await Promise.all([jobs, ...extra]);
  };
  const loadEvent = useEffectEvent((...args) => load(...args));
  useEffect(() => {
    if (!active || !['overview', 'jobs'].includes(section)) return undefined;
    void loadEvent();
    const timer = setInterval(loadEvent, 60000);
    const visible = () => globalThis.document?.visibilityState === 'visible' && void loadEvent();
    globalThis.document?.addEventListener('visibilitychange', visible);
    return () => {
      clearInterval(timer);
      globalThis.document?.removeEventListener('visibilitychange', visible);
    };
  }, [api, active, section]);
  return { ...data, reload: load };
}

function Kpi({ label, value, hint, tone }) {
  return (
    <div className={'ah-kpi' + (tone ? ' ' + tone : '')}>
      <span>{label}</span>
      <b>{value}</b>
      {hint && <small>{hint}</small>}
    </div>
  );
}

function StatusDot({ tone }) {
  return <span className={'ah-dot ' + (tone || 'muted')} aria-hidden="true" />;
}

function AlertList({ items }) {
  const { t } = useI18n();
  if (!items.length)
    return (
      <div className="ah-allclear">
        <ShieldCheck size={16} aria-hidden="true" />
        {t('adminHome.allClear')}
      </div>
    );
  return (
    <ul className="ah-alerts">
      {items.map((item, index) => (
        <li key={index} className={item.tone}>
          <AlertTriangle size={16} aria-hidden="true" />
          <div>
            <b>
              {t('adminHome.alert.' + item.kind, {
                count: item.count,
                owner: item.machine?.owner,
                gpu: item.machine?.gpu,
                age: duration(item.machine?.ageSeconds),
              })}
            </b>
            {item.reason && item.reason !== 'other' && (
              <small>{t('adminHome.reason.' + item.reason)}</small>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}

function Machines({ rows }) {
  const { t } = useI18n();
  if (!rows.length) return <EmptyState compact>{t('adminHome.noMachines')}</EmptyState>;
  return (
    <div className="ah-table-wrap">
      <table className="ah-table">
        <thead>
          <tr>
            <th>{t('adminHome.col.status')}</th>
            <th>{t('adminHome.col.owner')}</th>
            <th>{t('adminHome.col.machine')}</th>
            <th className="num">{t('adminHome.col.hourly')}</th>
            <th className="num">{t('adminHome.col.today')}</th>
            <th className="num">{t('adminHome.col.age')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} className={row.issue ? 'flag' : ''}>
              <td>
                <span className="ah-status">
                  <StatusDot tone={row.issue ? 'bad' : row.status.tone} />
                  <span>
                    {row.issue ? t('adminHome.issue.' + row.issue.kind) : row.status.label}
                    {row.issue?.reason && row.issue.reason !== 'other' && (
                      <small title={row.issue.code}>
                        {t('adminHome.reason.' + row.issue.reason)}
                      </small>
                    )}
                  </span>
                </span>
              </td>
              <td>{row.owner}</td>
              <td>
                {row.gpu} <Badge tone={row.mode}>{row.mode?.toUpperCase()}</Badge>
                <small className="ah-sub">
                  {[row.provider, row.region].filter(Boolean).join(' · ')}
                </small>
              </td>
              <td className="num">{usd(row.hourly)}</td>
              <td className="num">{usd(row.today)}</td>
              <td className="num">{duration(row.ageSeconds)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Jobs({ rows, api, onChanged, filter = 'attention' }) {
  const { t } = useI18n();
  const [view, setView] = useState(filter),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  const shown = rows.filter((row) =>
    view === 'attention'
      ? row.stuck || row.waitingLong || row.status === 'queued'
      : view === 'active'
        ? !['succeeded', 'failed', 'cancelled'].includes(row.status)
        : true,
  );
  async function cancel(row) {
    const body = cancellationBody([row.raw], 'operator_request_cancelled');
    if (!body) return;
    setBusy(true);
    setError('');
    try {
      await api.request('/v1/operator/admin/jobs/cancel', {
        method: 'POST',
        body,
        key: 'admin-cancel-' + row.id,
      });
      await onChanged();
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="ah-toolbar">
        <Segmented
          options={[
            { value: 'attention', label: t('adminHome.jobsAttention') },
            { value: 'active', label: t('adminHome.jobsActive') },
            { value: 'all', label: t('adminHome.jobsAll') },
          ]}
          value={view}
          onChange={setView}
        />
      </div>
      {!shown.length ? (
        <EmptyState compact>{t('adminHome.noJobs')}</EmptyState>
      ) : (
        <div className="ah-table-wrap">
          <table className="ah-table">
            <thead>
              <tr>
                <th>{t('adminHome.col.status')}</th>
                <th>{t('adminHome.col.owner')}</th>
                <th>{t('adminHome.col.job')}</th>
                <th className="num">{t('adminHome.col.waited')}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {shown.map((row) => (
                <tr key={row.id} className={row.stuck || row.waitingLong ? 'flag' : ''}>
                  <td>
                    <span className="ah-status">
                      <StatusDot
                        tone={row.stuck ? 'bad' : row.waitingLong ? 'warn' : statusTone(row.status)}
                      />
                      <span>
                        {row.stuck
                          ? t('adminHome.job.stuck')
                          : t('adminHome.job.' + row.status, { fallback: row.status })}
                        {row.reason && (
                          <small title={row.code}>{t('adminHome.reason.' + row.reason)}</small>
                        )}
                      </span>
                    </span>
                  </td>
                  <td>{row.owner}</td>
                  <td>
                    <code className="ah-id">{row.id.slice(0, 8)}</code>
                  </td>
                  <td className="num">{duration(row.ageSeconds)}</td>
                  <td className="ah-actions">
                    {row.cancellable && (
                      <Button size="sm" variant="ghost" disabled={busy} onClick={() => cancel(row)}>
                        {t('adminHome.cancelJob')}
                      </Button>
                    )}
                    {row.stuck && <small className="muted">{t('adminHome.stuckHint')}</small>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <FloatingMessage text={error || null} onDismiss={() => setError('')} />
    </>
  );
}

const statusTone = (status) =>
  status === 'running' ? 'good' : status === 'queued' ? 'warm' : 'muted';

function Overview({ data, api }) {
  const { t, locale } = useI18n();
  const machines = useMemo(() => machineRows(data.workspace, data.at, locale), [data, locale]);
  const review = useMemo(() => reviewRows(data.workspace), [data]);
  const jobs = useMemo(() => jobRows(data.jobs, data.at), [data]);
  const spend = spendSummary(data.settings);
  const items = findAlerts({ machines, jobs, spend, workspace: data.workspace });
  const waiting = jobs.filter((job) => job.status === 'queued');
  const longest = waiting.reduce((max, job) => Math.max(max, job.ageSeconds || 0), 0);
  const hourly = machines.reduce((sum, row) => sum + (row.hourly || 0), 0);
  // Machines (and alerts that depend on them) wait for the live read; jobs show meanwhile.
  const machinesPending = data.workspace === undefined && !data.machinesFailed;
  const machinesState = machinesPending ? (
    <Spinner size={16} label={t('adminHome.loading')} />
  ) : data.workspace === undefined ? (
    <Note tone="bad">{t('adminHome.loadFailed')}</Note>
  ) : null;
  return (
    <>
      <div className="ah-kpis">
        <Kpi
          label={t('adminHome.kpi.machines')}
          value={data.workspace === undefined ? '—' : machines.length}
          hint={t('adminHome.kpi.hourly', { amount: usd(hourly) })}
        />
        <Kpi
          label={t('adminHome.kpi.waiting')}
          value={waiting.length}
          hint={waiting.length ? t('adminHome.kpi.longest', { age: duration(longest) }) : null}
          tone={longest > 1800 ? 'warn' : ''}
        />
        <Kpi
          label={t('adminHome.kpi.spend')}
          value={
            spend ? (
              <>
                {usd(spend.used)}
                <small> / {usd(spend.limit)}</small>
              </>
            ) : (
              '—'
            )
          }
          hint={
            spend?.unconfirmed ? t('adminHome.kpi.unconfirmed', { count: spend.unconfirmed }) : null
          }
          tone={spend?.blocked ? 'bad' : spend?.nearCap ? 'warn' : ''}
        />
        <Kpi
          label={t('adminHome.kpi.alerts')}
          value={machinesState ? '—' : items.length}
          tone={
            machinesState
              ? ''
              : items.some((item) => item.tone === 'bad')
                ? 'bad'
                : items.length
                  ? 'warn'
                  : 'good'
          }
        />
      </div>
      <section className="ah-card">
        <header>
          <h2>{t('adminHome.needsAttention')}</h2>
        </header>
        {machinesState || <AlertList items={items} />}
      </section>
      <section className="ah-card">
        <header>
          <h2>{t('adminHome.machinesNow')}</h2>
          <Button size="sm" variant="ghost" onClick={() => navigateApp('/operator')}>
            <Cpu size={14} aria-hidden="true" />
            {t('adminHome.openConsole')}
          </Button>
        </header>
        {machinesState || <Machines rows={machines} />}
        {review.length > 0 && (
          <details className="ah-review">
            <summary>{t('adminHome.reviewRecords', { count: review.length })}</summary>
            <p className="muted">{t('adminHome.reviewHelp')}</p>
            <ul>
              {review.map((row) => (
                <li key={row.id}>
                  <b>{row.gpu}</b> · {row.owner} · {row.provider}
                  {row.mode ? ' · ' + row.mode : ''}
                  {row.code && <small className="muted"> · {row.code}</small>}
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>
      <section className="ah-card">
        <header>
          <h2>{t('adminHome.queueNow')}</h2>
        </header>
        <Jobs rows={jobs} api={api} onChanged={data.reload} />
      </section>
    </>
  );
}

// Per-account dollar budgets and per-GPU prices (moved from Settings → Admin).
function BudgetAdministration({ account }) {
  const { t } = useI18n();
  const controller = useMemo(() => createP4OperatorController({ locale: readBrowserLocale }), []),
    state = useSyncExternalStore(controller.subscribe, controller.getState, controller.getState);
  useEffect(() => () => controller.destroy(), [controller]);
  useEffect(() => {
    void controller.setAccount(account);
  }, [account, controller]);
  const current = state.account === account,
    authorized = current && state.snapshot?.operator?.admin === true;
  return (
    <section className="ops-tab-panel">
      {state.error && current && <p role="alert">{state.error}</p>}
      {authorized ? (
        <AdminBudgetPanel
          controller={controller}
          state={state}
          busy={state.busy || !!state.pending}
        />
      ) : (
        <p role="status">
          {t(
            !current || state.loading
              ? 'operatorPanel.adminPermissionPending'
              : 'operatorPanel.adminPermissionDenied',
          )}
        </p>
      )}
      {state.pending && current && authorized && (
        <section className="card-box" role="alert">
          <p>{t('operatorPanel.pendingAdmin')}</p>
          <Button disabled={state.busy} onClick={() => void controller.recover().catch(() => {})}>
            {t('operatorPanel.checkAdmin')}
          </Button>
        </section>
      )}
    </section>
  );
}

const BAND = {
  le5s: '≤5s',
  '5-8s': '5–8s',
  '8-12s': '8–12s',
  gt12s: '>12s',
  le480: '≤480P',
  le720: '≤720P',
  gt720: '>720P',
};
const band = (value) => BAND[value] || value;
const refs = (row) =>
  row.image_refs_band === 'unknown' || row.image_refs_band === undefined
    ? '—'
    : row.image_refs_band + (row.audio_ref === 'yes' ? ' + ♪' : '');

// Seconds per GPU type and content (h3-studio#548): history from the ledger, read-only.
function AdminStats({ api, active }) {
  const { t } = useI18n();
  const [days, setDays] = useState(14),
    [data, setData] = useState(null),
    [error, setError] = useState('');
  useEffect(() => {
    if (!active) return undefined;
    let live = true;
    setError('');
    api
      .request('/v1/operator/admin/generation-stats?days=' + days)
      .then((value) => live && setData(value))
      .catch((failure) => {
        if (!live) return;
        setData(null);
        setError(failure.status === 404 ? t('adminHome.stats.notDeployed') : failure.message);
      });
    return () => {
      live = false;
    };
  }, [api, active, days, t]);
  return (
    <>
      <div className="ah-toolbar">
        <Segmented
          options={[7, 14, 30].map((value) => ({
            value,
            label: t('adminHome.stats.days', { count: value }),
          }))}
          value={days}
          onChange={setDays}
        />
      </div>
      {error && <Note tone="warn">{error}</Note>}
      {!error && !data && <Spinner />}
      {data && !data.groups.length && <EmptyState compact>{t('adminHome.stats.empty')}</EmptyState>}
      {data && data.groups.length > 0 && (
        <>
          <section className="ah-card">
            <header>
              <h2>{t('adminHome.stats.byContent')}</h2>
              <Note>{t('adminHome.stats.total', { count: data.count })}</Note>
            </header>
            <div className="ah-table-wrap">
              <table className="ah-table">
                <thead>
                  <tr>
                    <th>GPU</th>
                    <th>{t('adminHome.stats.mode')}</th>
                    <th>{t('adminHome.stats.length')}</th>
                    <th>{t('adminHome.stats.resolution')}</th>
                    <th>{t('adminHome.stats.refs')}</th>
                    <th className="num">{t('adminHome.stats.count')}</th>
                    <th className="num">{t('adminHome.stats.median')}</th>
                    <th className="num">P90</th>
                    <th className="num">{t('adminHome.stats.max')}</th>
                    <th className="num">{t('adminHome.stats.cost')}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.groups.map((g, index) => (
                    <tr key={index}>
                      <td>{g.gpu_type}</td>
                      <td>{g.mode.toUpperCase()}</td>
                      <td>{band(g.length)}</td>
                      <td>{band(g.resolution)}</td>
                      <td>{refs(g)}</td>
                      <td className="num">{g.count}</td>
                      <td className="num">{duration(g.generate_seconds.median)}</td>
                      <td className="num">{duration(g.generate_seconds.p90)}</td>
                      <td className="num">{duration(g.generate_seconds.max)}</td>
                      <td className="num">{usd(g.median_cost_microusd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <section className="ah-card">
            <header>
              <h2>{t('adminHome.stats.recent')}</h2>
            </header>
            <div className="ah-table-wrap">
              <table className="ah-table">
                <thead>
                  <tr>
                    <th>{t('adminHome.stats.finished')}</th>
                    <th>{t('adminHome.col.owner')}</th>
                    <th>GPU</th>
                    <th>{t('adminHome.stats.mode')}</th>
                    <th className="num">{t('adminHome.stats.videoSeconds')}</th>
                    <th className="num">{t('adminHome.stats.resolution')}</th>
                    <th>{t('adminHome.stats.refs')}</th>
                    <th className="num">{t('adminHome.stats.took')}</th>
                    <th className="num">{t('adminHome.stats.waited')}</th>
                    <th className="num">{t('adminHome.stats.cost')}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent.slice(0, 50).map((row) => (
                    <tr key={row.job_id + ':' + row.attempt}>
                      <td>{new Date(row.finished_at * 1000).toLocaleString()}</td>
                      <td>{row.owner_id}</td>
                      <td>{row.gpu_type}</td>
                      <td>{row.mode.toUpperCase()}</td>
                      <td className="num">{row.seconds ?? '—'}</td>
                      <td className="num">{row.height ? row.height + 'P' : '—'}</td>
                      <td>{refs(row)}</td>
                      <td className="num">{duration(row.generate_seconds)}</td>
                      <td className="num">{duration(row.wait_seconds)}</td>
                      <td className="num">{usd(row.cost_microusd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </>
  );
}

function Limits({ api, active, account }) {
  const [settings, setSettings] = useState(null);
  const load = async () => setSettings(await api.request(SETTINGS_PATH));
  const loadEvent = useEffectEvent((...args) => load(...args));
  useEffect(() => {
    if (active) void loadEvent().catch(() => setSettings(null));
  }, [api, active]);
  const { t } = useI18n();
  if (!settings) return <Spinner />;
  return (
    <>
      {settings.capacity_policy ? (
        <CapacitySettings settings={settings} api={api} onSaved={() => void load()} />
      ) : (
        <Note tone="warn">{t('adminHome.noPolicy')}</Note>
      )}
      <details className="admin-fold">
        <summary>{t('admin.legacyBudgets')}</summary>
        <BudgetAdministration account={account} />
      </details>
    </>
  );
}

export default function AdminHome({ account }) {
  const { t } = useI18n();
  const active = usePageActive();
  const location = useAppLocation();
  const section = new URLSearchParams(location.search).get('section') || SECTIONS[0][0];
  const api = useClient(account);
  const [access, setAccess] = useState(null);
  useEffect(() => {
    let live = true;
    readAccessFor(account).then((value) => live && setAccess(value));
    return () => {
      live = false;
    };
  }, [account]);
  const data = useAdminData(api, active && access?.admin === true, section);
  const go = (id) => navigateApp('/admin?section=' + id);
  return (
    <div className="home admin-home">
      <StudioHeader account={account} active="admin" />
      {access && !access.admin ? (
        <main className="ah-main">
          <EmptyState icon={<ShieldCheck size={22} />} title={t('adminHome.adminOnly')} />
        </main>
      ) : (
        <div className="ah-shell">
          <SideNav className="ah-nav" aria-label={t('adminHome.title')}>
            <h1>{t('adminHome.title')}</h1>
            {SECTIONS.map(([id, label, Icon]) => (
              <SideNavItem
                key={id}
                active={section === id}
                icon={Icon}
                aria-current={section === id ? 'page' : undefined}
                onClick={() => go(id)}
              >
                {t(label)}
              </SideNavItem>
            ))}
          </SideNav>
          <main className="ah-main">
            <header className="ah-head">
              <div>
                <h2>{t(SECTIONS.find(([id]) => id === section)?.[1] || 'adminHome.overview')}</h2>
                {['overview', 'jobs'].includes(section) && data.at && (
                  <Note>
                    <Activity size={12} aria-hidden="true" />{' '}
                    {t('adminHome.updated', {
                      time: new Date(data.at * 1000).toLocaleTimeString(),
                    })}
                  </Note>
                )}
              </div>
              {['overview', 'jobs'].includes(section) && (
                <IconButton label={t('adminHome.refresh')} onClick={() => void data.reload()}>
                  <RefreshCw size={16} />
                </IconButton>
              )}
            </header>
            {['overview', 'jobs'].includes(section) &&
              (data.state === 'loading' ? (
                <Spinner size={18} label={t('adminHome.loading')} />
              ) : data.state === 'error' ? (
                <Note tone="bad">{t('adminHome.loadFailed')}</Note>
              ) : section === 'overview' ? (
                <Overview data={data} api={api} />
              ) : (
                <section className="ah-card">
                  <Jobs
                    rows={jobRows(data.jobs, data.at)}
                    api={api}
                    onChanged={data.reload}
                    filter="active"
                  />
                </section>
              ))}
            {section === 'limits' && <Limits api={api} active={active} account={account} />}
            {section === 'users' && <AdminUsers api={api} active={active} />}
            {section === 'pricing' && (
              <>
                <StoryChatAdminSwitch api={api} active={active} />
                <AdminPricing api={api} active={active} />
              </>
            )}
            {section === 'log' && <AdminAudit api={api} active={active} />}
            {section === 'stats' && <AdminStats api={api} active={active} />}
          </main>
        </div>
      )}
    </div>
  );
}
