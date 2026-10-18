import VideoCacheSettings from './VideoCacheSettings.jsx';
import SettingsGenerationDefaults from './SettingsGenerationDefaults.jsx';
import SpendingBreakdown from './SpendingBreakdown.jsx';
import { admissionExposure } from './spending-breakdown.js';
import { microsToDollars, dollarsToMicros, formatMoneyMicros } from './money.js';
import AssistantConnections from './AssistantConnections.jsx';
import React, { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { createCloudClient } from './cloud-client.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import { readBrowserLocale } from './i18n/locale.js';
import StudioHeader from './StudioHeader.jsx';
import AccountPassword from './AccountPassword.jsx';
import {
  UserRound,
  SlidersHorizontal,
  Bot,
  Wallet,
  ShieldCheck,
  LogOut,
  RefreshCw,
  BookOpen,
  ArrowUpRight,
} from 'lucide-react';
import { cloudController as cloud } from './cloud-controller.js';
import { useAppLocation, usePageActive } from './AppRouteCache.jsx';
import { navigateApp } from './app-navigation.js';
import { createP4OperatorController } from './p4-operator-controller.js';
import OperatorBudgetPanel, { AdminBudgetPanel } from './OperatorBudgetPanel.jsx';
import './v2/ops-adapter.css';

const dollars = microsToDollars;
function micros(value) {
  try {
    return dollarsToMicros(value);
  } catch {
    throw Error('settings.invalid');
  }
}
const money = formatMoneyMicros;
const settingsSection = (search) => {
  const value = new URLSearchParams(search).get('tab');
  return (
    {
      account: 'account',
      defaults: 'defaults',
      gen: 'defaults',
      assistants: 'assistants',
      agent: 'assistants',
      budget: 'budget',
      admin: 'admin',
    }[value] || 'account'
  );
};

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
          <button disabled={state.busy} onClick={() => void controller.recover().catch(() => {})}>
            {t('operatorPanel.checkAdmin')}
          </button>
        </section>
      )}
    </section>
  );
}

export default function SpendingSettings({ account }) {
  const { t } = useI18n(),
    api = useMemo(() => {
      const client = createCloudClient();
      client.setAccount(account);
      return client;
    }, [account]);
  const pageActive = usePageActive(),
    location = useAppLocation();
  useEffect(() => {
    if (pageActive) document.title = t('settings.documentTitle');
  }, [t, pageActive]);
  const [section, setSection] = useState(() => settingsSection(location.search));
  useEffect(() => {
    if (pageActive && location.path === '/settings') setSection(settingsSection(location.search));
  }, [pageActive, location.path, location.search]);
  const openSection = (value) => {
    setSection(value);
    const params = new URLSearchParams(location.search);
    if (value === 'account') params.delete('tab');
    else params.set('tab', value);
    navigateApp('/settings' + (params.size ? '?' + params : ''), { scroll: false });
  };
  const [user, setUser] = useState(null),
    [admin, setAdmin] = useState(null),
    [draft, setDraft] = useState(null),
    [adminDraft, setAdminDraft] = useState(null);
  const [busy, setBusy] = useState(false),
    [notice, setNotice] = useState(''),
    [error, setError] = useState(''),
    epoch = useRef(0);
  const load = async () => {
    if (!account) {
      setBusy(false);
      return;
    }
    const current = epoch.current;
    setBusy(true);
    setError('');
    try {
      const data = await api.request('/v1/account/spending-settings');
      if (current !== epoch.current) return;
      setUser(data);
      setDraft({
        daily: dollars(data.value.daily_limit_microusd),
        ask: dollars(data.value.ask_above_microusd),
        connections: Object.fromEntries(
          data.connections.map((row) => [row.actor_id, dollars(row.daily_limit_microusd)]),
        ),
      });
      if (data.admin) {
        const next = await api.request('/v1/operator/admin/spending-settings');
        if (current !== epoch.current) return;
        setAdmin(next);
        setAdminDraft({
          testTask: dollars(
            next.value.test_task_limit_microusd ?? next.value.test_daily_limit_microusd,
          ),
          hourly: dollars(next.value.max_hourly_price_microusd),
          fleetHourly: dollars(next.value.fleet_hourly_limit_microusd),
          idle: String(next.value.idle_shutdown_seconds / 60),
          runtime: String(next.value.max_runtime_seconds / 60),
        });
      }
    } catch (failure) {
      if (current === epoch.current)
        setError(failure.status === 401 ? t('settings.login') : failure.message);
    } finally {
      if (current === epoch.current) setBusy(false);
    }
  };
  useEffect(() => {
    epoch.current++;
    setUser(null);
    setAdmin(null);
    setDraft(null);
    setNotice('');
    void load();
    return () => {
      epoch.current++;
      api.reset();
    };
  }, [api]);
  async function save(isAdmin = false) {
    if (!account) return;
    const current = epoch.current;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      let body;
      if (isAdmin) {
        const seconds = (field) => {
          const value = Number(adminDraft[field]) * 60;
          if (!Number.isSafeInteger(value) || value < 1) throw Error('settings.invalid');
          return value;
        };
        body = {
          expected_version: admin.version,
          value: {
            [Object.hasOwn(admin.value, 'test_task_limit_microusd')
              ? 'test_task_limit_microusd'
              : 'test_daily_limit_microusd']: micros(adminDraft.testTask),
            max_hourly_price_microusd: micros(adminDraft.hourly),
            fleet_hourly_limit_microusd: micros(adminDraft.fleetHourly),
            idle_shutdown_seconds: seconds('idle'),
            max_runtime_seconds: seconds('runtime'),
          },
        };
      } else
        body = {
          expected_version: user.version,
          value: {
            daily_limit_microusd: micros(draft.daily),
            ask_above_microusd: micros(draft.ask),
            connection_limits: {
              ...user.value.connection_limits,
              ...Object.fromEntries(
                Object.entries(draft.connections).map(([key, value]) => [key, micros(value)]),
              ),
            },
          },
        };
      const result = await api.request(
        isAdmin ? '/v1/operator/admin/spending-settings' : '/v1/account/spending-settings',
        { method: 'PUT', body },
      );
      if (current !== epoch.current) return;
      if (isAdmin) setAdmin(result);
      else setUser((previous) => ({ ...previous, ...result }));
      setNotice('settings.saved');
    } catch (failure) {
      if (current === epoch.current)
        setError(failure.message === 'settings.invalid' ? t(failure.message) : failure.message);
    } finally {
      if (current === epoch.current) setBusy(false);
    }
  }
  const field = (label, value, onChange) => (
    <label className="limit-row">
      <div>
        <b>{t(label)}</b>
      </div>
      <span className="limit-input">
        <input
          inputMode="decimal"
          value={value}
          disabled={busy}
          onChange={(event) => onChange(event.target.value)}
        />
      </span>
    </label>
  );
  const exposure = admissionExposure(user?.usage, user?.value?.daily_limit_microusd);
  return (
    <div className="home ops-adapter">
      <StudioHeader account={account} active="settings" />
      <main className="home-main settings-page">
        <aside className="snav" aria-label={t('settings.title')}>
          {[
            ['account', 'settings.account', UserRound],
            ['defaults', 'settings.defaults', SlidersHorizontal],
            ['assistants', 'assistants.title', Bot],
            ['budget', 'settings.budget', Wallet],
            ...(admin ? [['admin', 'settings.admin', ShieldCheck]] : []),
          ].map(([id, label, Icon]) => (
            <button
              key={id}
              className={section === id ? 'on' : ''}
              aria-current={section === id ? 'page' : undefined}
              onClick={() => openSection(id)}
            >
              <Icon size={16} />
              {t(label)}
            </button>
          ))}
        </aside>
        <section className="spanel">
          <div className="row tight">
            <h2>{t(section === 'assistants' ? 'assistants.title' : 'settings.' + section)}</h2>
            <div className="spacer" />
            <button
              className="small ghost"
              title={t('settings.reload')}
              aria-label={t('settings.reload')}
              disabled={busy}
              onClick={() => void load()}
            >
              <RefreshCw size={14} />
            </button>
          </div>
          {error && <p role="alert">{error}</p>}
          {notice && <p role="status">{t(notice)}</p>}
          <div className="ops-tab-panel" hidden={section !== 'account'}>
            <div className="card-box">
              <div className="row tight">
                <span className="avatar big">
                  {account?.slice(0, 1).toLocaleUpperCase() || <UserRound size={21} />}
                </span>
                <div>
                  <b>{account || t('settings.login')}</b>
                  {admin && <small className="muted"> · {t('settings.admin')}</small>}
                </div>
              </div>
            </div>
            {account && (
              <>
                <div className="card-box">
                  <AccountPassword inline busy={busy} />
                </div>
                <VideoCacheSettings account={account} />
                <button
                  className="ghost danger"
                  disabled={busy}
                  onClick={() => void cloud.logout().catch((failure) => setError(failure.message))}
                >
                  <LogOut size={15} />
                  {t('operator.status117')}
                </button>
              </>
            )}
          </div>
          <div className="ops-tab-panel" hidden={section !== 'defaults'}>
            <SettingsGenerationDefaults
              account={account}
              api={api}
              active={pageActive && section === 'defaults'}
            />
            <p className="muted">{t('settings.defaultsHelp')}</p>
            <div className="card-box">
              <div className="row tight">
                <SlidersHorizontal size={18} />
                <div>
                  <b>{t('takes.preferences')}</b>
                  <br />
                  <small className="muted">{t('settings.defaults')}</small>
                </div>
                <div className="spacer" />
                <button
                  className="primary"
                  onClick={() => navigateApp('/quick-chat?preferences=takes')}
                >
                  {t('takes.preferences')}
                </button>
              </div>
            </div>
          </div>
          {(!user || !draft) &&
            section !== 'account' &&
            section !== 'defaults' &&
            section !== 'assistants' && (
              <p role="status">{busy ? t('settings.loading') : t('settings.login')}</p>
            )}
          {user && draft && (
            <form
              className="ops-tab-panel"
              hidden={section !== 'budget'}
              onSubmit={(event) => {
                event.preventDefault();
                void save();
              }}
            >
              <div className="card-box">
                <SpendingBreakdown usage={user.usage} scope="account" />
                {exposure && exposure.limit > 0 && (
                  <div className="agent-spend spend-admission">
                    <span>
                      {t('settings.usage', { amount: money(exposure.amount) })} /{' '}
                      {money(exposure.limit)}
                    </span>
                    <i>
                      <b style={{ width: exposure.percent + '%' }} />
                    </i>
                    <p className="spend-breakdown-note">{t('settings.admissionHelp')}</p>
                  </div>
                )}
              </div>
              <section id="my-budget" className="card-box limits" aria-label={t('settings.budget')}>
                {field('settings.daily', draft.daily, (value) =>
                  setDraft((previous) => ({ ...previous, daily: value })),
                )}
                {field('settings.ask', draft.ask, (value) =>
                  setDraft((previous) => ({ ...previous, ask: value })),
                )}
              </section>
              <p className="muted">
                {t('settings.zero')} · {t('settings.day')}
              </p>
              <div className="row tight" style={{ justifyContent: 'flex-end' }}>
                <button className="primary" disabled={busy}>
                  {t(busy ? 'settings.saving' : 'settings.save')}
                </button>
              </div>
            </form>
          )}
          {user && draft && section === 'budget' && pageActive && (
            <OperatorBudgetPanel account={account} />
          )}
          {admin && adminDraft && (
            <form
              className="ops-tab-panel"
              hidden={section !== 'admin'}
              onSubmit={(event) => {
                event.preventDefault();
                void save(true);
              }}
            >
              <section id="my-admin" className="card-box limits" aria-label={t('settings.admin')}>
                {[
                  ['settings.testTask', 'testTask'],
                  ['settings.hourly', 'hourly'],
                  ['settings.fleetHourly', 'fleetHourly'],
                  ['settings.idle', 'idle'],
                  ['settings.runtime', 'runtime'],
                ].map(([label, key]) => (
                  <React.Fragment key={key}>
                    {field(label, adminDraft[key], (value) =>
                      setAdminDraft((previous) => ({ ...previous, [key]: value })),
                    )}
                  </React.Fragment>
                ))}
              </section>
              <p className="muted">{t('settings.adminHelp')}</p>
              <div className="row tight" style={{ justifyContent: 'flex-end' }}>
                <button className="primary" disabled={busy}>
                  {t(busy ? 'settings.saving' : 'settings.save')}
                </button>
              </div>
            </form>
          )}
          {admin && section === 'admin' && pageActive && <BudgetAdministration account={account} />}
          <div id="my-assistants" hidden={section !== 'assistants'}>
            <AssistantConnections
              embedded
              account={account}
              active={pageActive && section === 'assistants'}
              onLogin={() => navigateApp('/quick-chat')}
            />
          </div>
        </section>
      </main>
    </div>
  );
}
