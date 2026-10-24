import React, { useEffect, useMemo, useState } from 'react';
import {
  Activity,
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
import StoryChatAdminSwitch from './StoryChatAdminSwitch.jsx';
import { AdminAudit, AdminPricing, AdminUsers, useClient } from './AdminConsole.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { navigateApp } from './app-navigation.js';
import { useAppLocation, usePageActive } from './AppRouteCache.jsx';
import { readAccessFor } from './account-access.js';
import { Badge, EmptyState, Note } from './ui/Note.jsx';
import { Button, IconButton } from './ui/Button.jsx';
import { Spinner } from './ui/Field.jsx';
import { Segmented } from './ui/Segmented.jsx';
import { FloatingMessage } from './floating-notice.js';
import { cancellationBody } from './operator-jobs-model.js';
import {
  alerts as findAlerts,
  duration,
  jobRows,
  machineRows,
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
];
const SETTINGS_PATH = '/v1/operator/admin/spending-settings';

// Everything the overview and jobs pages read, refreshed together every 30 s.
function useAdminData(api, active) {
  const [data, setData] = useState({ state: 'loading' });
  const load = async () => {
    try {
      const [workspace, jobs, settings] = await Promise.all([
        api.request('/v1/operator/dstack/admin/workspace'),
        api.request('/v1/operator/admin/jobs?limit=100'),
        api.request(SETTINGS_PATH),
      ]);
      setData({
        state: 'ready',
        workspace,
        jobs: jobs.jobs || [],
        settings,
        at: Date.now() / 1000,
      });
    } catch (failure) {
      setData((old) => ({
        ...old,
        state: failure.status === 403 ? 'forbidden' : old.workspace ? 'stale' : 'error',
        error: failure.message,
      }));
    }
  };
  useEffect(() => {
    if (!active) return undefined;
    void load();
    const timer = setInterval(load, 30000);
    return () => clearInterval(timer);
  }, [api, active]);
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
  const jobs = useMemo(() => jobRows(data.jobs, data.at), [data]);
  const spend = spendSummary(data.settings);
  const items = findAlerts({ machines, jobs, spend, workspace: data.workspace });
  const waiting = jobs.filter((job) => job.status === 'queued');
  const longest = waiting.reduce((max, job) => Math.max(max, job.ageSeconds || 0), 0);
  const hourly = machines.reduce((sum, row) => sum + (row.hourly || 0), 0);
  return (
    <>
      <div className="ah-kpis">
        <Kpi
          label={t('adminHome.kpi.machines')}
          value={machines.length}
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
          value={items.length}
          tone={items.some((item) => item.tone === 'bad') ? 'bad' : items.length ? 'warn' : 'good'}
        />
      </div>
      <section className="ah-card">
        <header>
          <h2>{t('adminHome.needsAttention')}</h2>
        </header>
        <AlertList items={items} />
      </section>
      <section className="ah-card">
        <header>
          <h2>{t('adminHome.machinesNow')}</h2>
          <Button size="sm" variant="ghost" onClick={() => navigateApp('/operator')}>
            <Cpu size={14} aria-hidden="true" />
            {t('adminHome.openConsole')}
          </Button>
        </header>
        <Machines rows={machines} />
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

function Limits({ api, active }) {
  const [settings, setSettings] = useState(null);
  const load = async () => setSettings(await api.request(SETTINGS_PATH));
  useEffect(() => {
    if (active) void load().catch(() => setSettings(null));
  }, [api, active]);
  if (!settings) return <Spinner />;
  return <CapacitySettings settings={settings} api={api} onSaved={() => void load()} />;
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
  const data = useAdminData(api, active && access?.admin === true);
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
          <nav className="ah-nav" aria-label={t('adminHome.title')}>
            <h1>{t('adminHome.title')}</h1>
            {SECTIONS.map(([id, label, Icon]) => (
              <button
                key={id}
                type="button"
                className={section === id ? 'on' : ''}
                aria-current={section === id ? 'page' : undefined}
                onClick={() => go(id)}
              >
                <Icon size={16} aria-hidden="true" />
                {t(label)}
              </button>
            ))}
          </nav>
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
            {section === 'limits' && <Limits api={api} active={active} />}
            {section === 'users' && <AdminUsers api={api} active={active} />}
            {section === 'pricing' && (
              <>
                <StoryChatAdminSwitch api={api} active={active} />
                <AdminPricing api={api} active={active} />
              </>
            )}
            {section === 'log' && <AdminAudit api={api} active={active} />}
          </main>
        </div>
      )}
    </div>
  );
}
