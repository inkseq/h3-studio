import { previewingAsUser } from './preview-as-user.js';
import { FloatingMessage } from './floating-notice.js';
import VideoCacheSettings from './VideoCacheSettings.jsx';
import SettingsGenerationDefaults from './SettingsGenerationDefaults.jsx';
import TakePreferencesSettings from './TakePreferencesSettings.jsx';
import SpendingBreakdown from './SpendingBreakdown.jsx';
import { admissionExposure } from './spending-breakdown.js';
import { microsToDollars, dollarsToMicros, formatMoneyMicros } from './money.js';
import AssistantConnections from './AssistantConnections.jsx';
import React, { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { createCloudClient } from './cloud-client.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import { readBrowserLocale } from './i18n/locale.js';
import StudioHeader, { useAccess, useAccountLabel } from './StudioHeader.jsx';
import AccountPassword from './AccountPassword.jsx';
import AccountGoogleLink from './AccountGoogleLink.jsx';
import { CreditsPanel, useCredits } from './CreditsUI.jsx';
import AdminConsole from './AdminConsole.jsx';
import CapacitySettings from './CapacitySettings.jsx';
import SkinPicker from './SkinPicker.jsx';
import {
  UserRound,
  SlidersHorizontal,
  Bot,
  Wallet,
  Coins,
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
// Credit mode (#306): the same settings route answers in whole credits instead of microUSD.
const creditText = (value) => String(Number.isSafeInteger(value) && value >= 0 ? value : 0);
function wholeCredits(value) {
  if (typeof value !== 'string' || !/^(?:0|[1-9]\d{0,11})$/.test(value.trim()))
    throw Error('settings.invalidCredits');
  return Number(value.trim());
}
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
      credits: 'credits',
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
  // Credits appear once the backend serves /v1/credits (#306).
  const credits = useCredits(account),
    access = useAccess(account),
    who = useAccountLabel(account);
  // Regular users never see dollars: in credit mode the USD budget tab is admin-only.
  const creditMode = user?.unit === 'credits';
  const showBudget = creditMode || !(credits || access?.creditsEnabled) || !!admin;
  // Hosted (WorkOS) sign-ins have no local password to change.
  const hosted = access?.authentication === 'workos';
  const [busy, setBusy] = useState(false),
    [notice, setNotice] = useState(''),
    [error, setError] = useState(''),
    [everywhere, setEverywhere] = useState(false),
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
      setDraft(
        data.unit === 'credits'
          ? {
              daily: creditText(data.value.daily_limit_credits),
              ask: creditText(data.value.ask_above_credits),
              connections: Object.fromEntries(
                data.connections.map((row) => [row.actor_id, creditText(row.daily_limit_credits)]),
              ),
            }
          : {
              daily: dollars(data.value.daily_limit_microusd),
              ask: dollars(data.value.ask_above_microusd),
              connections: Object.fromEntries(
                data.connections.map((row) => [row.actor_id, dollars(row.daily_limit_microusd)]),
              ),
            },
      );
      if (data.admin && !previewingAsUser()) {
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
      } else if (user.unit === 'credits')
        body = {
          expected_version: user.version,
          value: {
            daily_limit_credits: wholeCredits(draft.daily),
            ask_above_credits: wholeCredits(draft.ask),
            connection_credit_limits: {
              ...user.value.connection_credit_limits,
              ...Object.fromEntries(
                Object.entries(draft.connections).map(([key, value]) => [key, wholeCredits(value)]),
              ),
            },
          },
        };
      else
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
        setError(
          ['settings.invalid', 'settings.invalidCredits'].includes(failure.message)
            ? t(failure.message)
            : failure.message,
        );
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
            ...(credits || section === 'credits' ? [['credits', 'settings.credits', Coins]] : []),
            ...(showBudget
              ? [['budget', creditMode ? 'settings.limits' : 'settings.budget', Wallet]]
              : []),
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
            <h2>
              {t(
                section === 'assistants'
                  ? 'assistants.title'
                  : section === 'budget' && creditMode
                    ? 'settings.limits'
                    : 'settings.' + section,
              )}
            </h2>
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
                <span className="avatar big">{who.initial || <UserRound size={21} />}</span>
                <div>
                  <b>{who.label || t('settings.login')}</b>
                  {admin && <small className="muted"> · {t('settings.admin')}</small>}
                  {access?.email && access.email !== who.label && (
                    <>
                      <br />
                      <small className="muted">{access.email}</small>
                    </>
                  )}
                  {hosted && (
                    <>
                      <br />
                      <small className="muted">{t('account.hostedSignIn')}</small>
                    </>
                  )}
                </div>
              </div>
            </div>
            <SkinPicker />
            {account && (
              <>
                <AccountGoogleLink api={api} />
                {!hosted && (
                  <div className="card-box">
                    <AccountPassword inline busy={busy} />
                  </div>
                )}
                <VideoCacheSettings account={account} />
                <div className="row tight account-signout">
                  <button
                    className="ghost danger"
                    disabled={busy}
                    onClick={() =>
                      void cloud.logout().catch((failure) => setError(failure.message))
                    }
                  >
                    <LogOut size={15} />
                    {t('operator.status117')}
                  </button>
                  {everywhere ? (
                    <>
                      <small className="muted">{t('account.signOutEverywhereConfirm')}</small>
                      <button
                        className="danger"
                        disabled={busy}
                        onClick={() =>
                          void cloud
                            .logoutEverywhere()
                            .catch((failure) => setError(failure.message))
                        }
                      >
                        {t('account.signOutEverywhereYes')}
                      </button>
                      <button className="ghost" onClick={() => setEverywhere(false)}>
                        {t('library.cancel')}
                      </button>
                    </>
                  ) : (
                    <button className="ghost" disabled={busy} onClick={() => setEverywhere(true)}>
                      {t('account.signOutEverywhere')}
                    </button>
                  )}
                </div>
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
              </div>
              <TakePreferencesSettings api={api} active={pageActive && section === 'defaults'} />
            </div>
          </div>
          {(!user || !draft) &&
            section !== 'account' &&
            section !== 'defaults' &&
            section !== 'assistants' &&
            section !== 'credits' && (
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
              {!creditMode && (
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
              )}
              {creditMode && Number.isSafeInteger(user.usage?.exposure_credits) && (
                <p className="muted">
                  {t('settings.creditsToday', { count: user.usage.exposure_credits })}
                </p>
              )}
              <section id="my-budget" className="card-box limits" aria-label={t('settings.budget')}>
                {field(
                  creditMode ? 'settings.dailyCredits' : 'settings.daily',
                  draft.daily,
                  (value) => setDraft((previous) => ({ ...previous, daily: value })),
                )}
                {field(creditMode ? 'settings.askCredits' : 'settings.ask', draft.ask, (value) =>
                  setDraft((previous) => ({ ...previous, ask: value })),
                )}
              </section>
              <p className="muted">
                {t(creditMode ? 'settings.zeroCredits' : 'settings.zero')} · {t('settings.day')}
              </p>
              <div className="row tight" style={{ justifyContent: 'flex-end' }}>
                <button className="primary" disabled={busy}>
                  {t(busy ? 'settings.saving' : 'settings.save')}
                </button>
              </div>
            </form>
          )}
          {user && draft && !creditMode && section === 'budget' && pageActive && (
            <OperatorBudgetPanel account={account} />
          )}
          {admin && section === 'admin' && (
            <AdminConsole
              account={account}
              active={pageActive}
              testMachines={
                <>
                  {/* Versioned capacity policy (#456): preview, reason, confirm. The legacy
                      five-field form stays only for a server without the policy metadata. */}
                  {admin.capacity_policy && (
                    <CapacitySettings settings={admin} api={api} onSaved={() => void load()} />
                  )}
                  {adminDraft && !admin.capacity_policy && (
                    <form
                      className="ops-tab-panel"
                      hidden={section !== 'admin'}
                      onSubmit={(event) => {
                        event.preventDefault();
                        void save(true);
                      }}
                    >
                      <section
                        id="my-admin"
                        className="card-box limits"
                        aria-label={t('settings.admin')}
                      >
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
                  {/* Per-account dollar budgets and per-GPU prices are test-machine plumbing;
                      folded so the everyday limits above stay readable. */}
                  {pageActive && (
                    <details className="admin-fold">
                      <summary>{t('admin.legacyBudgets')}</summary>
                      <BudgetAdministration account={account} />
                    </details>
                  )}
                </>
              }
            />
          )}
          {section === 'credits' && <CreditsPanel account={account} active={pageActive} />}
          <div id="my-assistants" hidden={section !== 'assistants'}>
            <AssistantConnections
              creditMode={creditMode}
              embedded
              account={account}
              active={pageActive && section === 'assistants'}
              onLogin={() => navigateApp('/')}
            />
          </div>
        </section>
      </main>
    </div>
  );
}
