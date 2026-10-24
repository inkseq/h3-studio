// Settings → Admin (h3-studio#304): users, audit log and the existing test-machine limits.
// Port of prototype account.jsx AdminPanel. The backend enforces require_admin on every route;
// a tab whose endpoint is not deployed yet says so instead of failing.
import { Segmented } from './ui/Segmented.jsx';
import React, { useEffect, useMemo, useState } from 'react';
import { FloatingMessage } from './floating-notice.js';
import StoryChatAdminSwitch from './StoryChatAdminSwitch.jsx';
import { ChevronDown, History, ShieldCheck, TrendingUp, Users } from 'lucide-react';
import { createCloudClient } from './cloud-client.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import { apiDate, formatCredits } from './credits.js';
import './admin.css';

export function useClient(account) {
  return useMemo(() => {
    const client = createCloudClient();
    client.setAccount(account);
    return client;
  }, [account]);
}

// Reads a cursor-paged list; 404 means the endpoint is not deployed on this server yet.
function usePaged(api, path, active) {
  const [rows, setRows] = useState([]),
    [next, setNext] = useState(null),
    [state, setState] = useState('loading'),
    [error, setError] = useState('');
  const load = async (cursor = null, replace = true) => {
    setError('');
    try {
      const join = path.includes('?') ? '&' : '?';
      const body = await api.request(
        cursor ? path + join + 'cursor=' + encodeURIComponent(cursor) : path,
      );
      const list = Array.isArray(body) ? body : body?.items || body?.users || body?.entries || [];
      setRows((previous) => (replace ? list : [...previous, ...list]));
      setNext(typeof body?.next_cursor === 'string' ? body.next_cursor : null);
      setState('ready');
    } catch (failure) {
      setState(failure.status === 404 ? 'missing' : 'error');
      setError(failure.message);
    }
  };
  useEffect(() => {
    if (active) void load();
  }, [api, path, active]);
  return { rows, setRows, next, state, error, load };
}

export function AdminUsers({ api, active }) {
  const { t, locale } = useI18n();
  const [query, setQuery] = useState(''),
    [search, setSearch] = useState(''),
    [open, setOpen] = useState(null),
    [delta, setDelta] = useState('100'),
    [reason, setReason] = useState(''),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState('');
  useEffect(() => {
    const timer = setTimeout(() => setSearch(query.trim()), 300);
    return () => clearTimeout(timer);
  }, [query]);
  const list = usePaged(api, '/v1/admin/users?q=' + encodeURIComponent(search), active);
  if (list.state === 'missing') return <p className="muted">{t('admin.notDeployed')}</p>;
  const update = async (user, patch, path = '', method = 'PATCH') => {
    if (!reason.trim()) return setNotice(t('admin.reasonRequired'));
    setBusy(true);
    setNotice('');
    try {
      const body = { ...patch, reason: reason.trim() };
      const result = await api.request('/v1/admin/users/' + encodeURIComponent(user.id) + path, {
        method,
        body,
        key: 'admin-' + user.id + '-' + Date.now(),
      });
      list.setRows((rows) =>
        rows.map((row) =>
          row.id === user.id
            ? { ...row, ...(path ? { balance: result?.balance ?? row.balance } : result || patch) }
            : row,
        ),
      );
      setReason('');
      setNotice(t('admin.saved'));
    } catch (failure) {
      setNotice(failure.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card-box">
      <div className="row tight">
        <input
          className="lib-search"
          type="search"
          placeholder={t('admin.searchUsers')}
          aria-label={t('admin.searchUsers')}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      {list.error && <p role="alert">{list.error}</p>}
      {notice && <p role="status">{notice}</p>}
      <div className="user-table">
        <div className="user-row head">
          <span>{t('admin.account')}</span>
          <span>{t('admin.role')}</span>
          <span>{t('admin.balance')}</span>
          <span>{t('admin.used30')}</span>
          <span>{t('admin.joined')}</span>
          <span />
        </div>
        {list.rows.map((user) => (
          <React.Fragment key={user.id}>
            <div className={'user-row' + (user.status === 'suspended' ? ' off' : '')}>
              <span>
                <b className="clip">
                  {user.display_name || user.email || user.id}
                  {user.status === 'suspended' && (
                    <small className="muted"> · {t('admin.suspended')}</small>
                  )}
                </b>
                <small className="muted clip">
                  {[
                    user.display_name && user.email !== user.display_name ? user.email : '',
                    (user.providers || []).map((p) => t('admin.provider.' + p)).join(' · '),
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                </small>
              </span>
              <span>
                {user.role === 'admin' ? (
                  <em className="admin-pill">{t('admin.roleAdmin')}</em>
                ) : (
                  <small className="muted">{t('admin.roleUser')}</small>
                )}
              </span>
              <span className="num">{formatCredits(user.balance, locale)}</span>
              <span className="num">{formatCredits(user.used_30d, locale)}</span>
              <span className="num muted">
                {apiDate(user.created_at)?.toLocaleDateString(locale) || '—'}
              </span>
              <span>
                <button
                  type="button"
                  className="ghost small"
                  aria-expanded={open === user.id}
                  onClick={() => setOpen(open === user.id ? null : user.id)}
                >
                  {t('admin.manage')}
                  <ChevronDown size={13} aria-hidden="true" />
                </button>
              </span>
            </div>
            {open === user.id && (
              <div className="user-adjust">
                <label className="field small">
                  <span>{t('admin.delta')}</span>
                  <input
                    inputMode="numeric"
                    value={delta}
                    onChange={(event) => setDelta(event.target.value)}
                  />
                </label>
                <label className="field">
                  <span>{t('admin.reason')}</span>
                  <input
                    value={reason}
                    maxLength={300}
                    placeholder={t('admin.reasonExample')}
                    onChange={(event) => setReason(event.target.value)}
                  />
                </label>
                <button
                  type="button"
                  className="primary"
                  disabled={busy || !/^-?[1-9]\d*$/.test(delta) || !reason.trim()}
                  onClick={() => void update(user, { delta: Number(delta) }, '/credits', 'POST')}
                >
                  {t('admin.adjust')}
                </button>
                <span className="user-adjust-actions">
                  <button
                    type="button"
                    className="ghost small"
                    disabled={busy}
                    onClick={() =>
                      void update(user, { role: user.role === 'admin' ? 'user' : 'admin' })
                    }
                  >
                    {t(user.role === 'admin' ? 'admin.makeUser' : 'admin.makeAdmin')}
                  </button>
                  <button
                    type="button"
                    className={'ghost small' + (user.status === 'suspended' ? '' : ' danger')}
                    disabled={busy}
                    onClick={() =>
                      void update(user, {
                        status: user.status === 'suspended' ? 'active' : 'suspended',
                      })
                    }
                  >
                    {t(user.status === 'suspended' ? 'admin.restore' : 'admin.suspend')}
                  </button>
                </span>
              </div>
            )}
          </React.Fragment>
        ))}
      </div>
      {list.state === 'ready' && !list.rows.length && <p className="muted">{t('admin.noUsers')}</p>}
      {list.next && (
        <button
          type="button"
          className="ghost small"
          onClick={() => void list.load(list.next, false)}
        >
          {t('credits.more')}
        </button>
      )}
    </div>
  );
}

const usd = (value, digits = 2) =>
  typeof value === 'number' && Number.isFinite(value) ? '$' + value.toFixed(digits) : '—';
const pct = (value) =>
  typeof value === 'number' && Number.isFinite(value) ? Math.round(value * 100) + '%' : '—';

// Read-only pricing view from GET /v1/admin/credits/prices (#306). Margins use the worst-case
// net per credit (tax-inclusive, fees, biggest bonus); per-tier margin needs real GPU cost.
export function AdminPricing({ api, active }) {
  const { t, locale } = useI18n();
  const [data, setData] = useState(null),
    [state, setState] = useState('loading');
  useEffect(() => {
    if (!active) return undefined;
    let live = true;
    api
      .request('/v1/admin/credits/prices')
      .then((body) => {
        if (!live) return;
        setData(body);
        setState('ready');
      })
      .catch((failure) => live && setState(failure.status === 404 ? 'missing' : 'error'));
    return () => {
      live = false;
    };
  }, [api, active]);
  if (state === 'missing') return <p className="muted">{t('admin.notDeployed')}</p>;
  if (state === 'error') return <p role="alert">{t('admin.pricingError')}</p>;
  if (!data) return <p role="status">{t('settings.loading')}</p>;
  const estimate = data.margin_estimate || {},
    tiers = estimate.tiers || {},
    a = estimate.assumptions || {};
  return (
    <>
      <div className="card-box">
        <h4>{t('admin.pricingTiers')}</h4>
        <div className="margin-table">
          <div className="head">
            <span>{t('admin.pricingTier')}</span>
            <span>{t('admin.pricingRate')}</span>
            <span>{t('admin.pricingNet')}</span>
            <span>{t('admin.pricingMaxGpu')}</span>
            <span>{t('admin.pricingMargin')}</span>
          </div>
          {['fast', 'standard', 'refine'].map((tier) => {
            const row = tiers[tier] || {},
              basis = data.basis?.[tier] || {};
            return (
              <div key={tier} className={row.below_30_percent ? 'bad' : ''}>
                <span>
                  <b>{t('credits.tier.' + tier)}</b>
                  <small className="muted">
                    {[basis.gpu, basis.seconds_per_5s_clip && basis.seconds_per_5s_clip + 's']
                      .filter(Boolean)
                      .join(' · ')}
                  </small>
                </span>
                <span>
                  {t('credits.perSecond', { count: data.rates?.[tier] ?? '—' })}
                  <small className="muted">
                    {t('credits.perClip', { count: row.credits_per_5s_clip ?? '—' })}
                  </small>
                </span>
                <span>{usd(row.estimated_net_usd_per_5s_clip, 3)}</span>
                <span>
                  {usd(row.max_gpu_hourly_usd_for_30_percent)}/h
                  <small className="muted">
                    {t('admin.pricingBreakEven', { value: usd(row.break_even_gpu_hourly_usd) })}
                  </small>
                </span>
                <span>
                  {row.estimated_margin == null
                    ? t('admin.pricingUnmeasured')
                    : pct(row.estimated_margin)}
                </span>
              </div>
            );
          })}
        </div>
        <small className="muted">
          {t('admin.pricingAssumptions', {
            tax: pct(a.consumer_tax_rate),
            fee: pct(a.processing_and_mor_fee_rate),
            fixed: usd(a.fixed_fee_usd_per_pack),
            multiplier: data.cost_multiplier ?? '—',
          })}
        </small>
      </div>
      <div className="card-box">
        <h4>{t('admin.pricingPacks')}</h4>
        <div className="margin-table">
          <div className="head">
            <span>{t('admin.pricingPack')}</span>
            <span>{t('admin.pricingPrice')}</span>
            <span>{t('admin.balance')}</span>
            <span>{t('admin.pricingNetPack')}</span>
            <span>{t('admin.pricingPerCredit')}</span>
          </div>
          {(estimate.packs || []).map((pack) => (
            <div key={pack.pack_id}>
              <span>
                <b>{pack.pack_id}</b>
                {pack.pack_id === estimate.worst_case_pack_id && (
                  <small className="muted">{t('admin.pricingWorst')}</small>
                )}
              </span>
              <span>{usd(pack.amount_cents / 100)}</span>
              <span>
                {formatCredits(pack.credits, locale)}
                {pack.bonus_percent ? (
                  <small className="good"> +{pack.bonus_percent}%</small>
                ) : null}
              </span>
              <span>{usd(pack.estimated_net_usd)}</span>
              <span>{usd(pack.estimated_net_usd_per_credit, 4)}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="card-box admin-stats">
        <h4>{t('admin.pricingReport')}</h4>
        <dl>
          <div>
            <dt>{t('admin.statCaptured')}</dt>
            <dd>{formatCredits(data.report_30d?.captured_credits ?? 0, locale)}</dd>
          </div>
          <div>
            <dt>{t('admin.statCost')}</dt>
            <dd>{usd(data.report_30d?.provider_cost_usd)}</dd>
          </div>
          <div>
            <dt>{t('admin.statWelcome')}</dt>
            <dd>{formatCredits(data.welcome_credits ?? 0, locale)}</dd>
          </div>
        </dl>
      </div>
    </>
  );
}

// Audit actions are short English strings from several services; known ones read better.
function auditLabel(action, t) {
  const text = String(action || '');
  const fixed = {
    'story_chat.pictures': 'admin.act.pictures',
    'credits adjusted': 'admin.act.credits',
    'credits enabled': 'admin.act.creditsOn',
    'credits disabled': 'admin.act.creditsOff',
    'budget limit changed': 'admin.act.budget',
    'machine price changed': 'admin.act.machinePrice',
    settled: 'admin.act.settled',
    waived: 'admin.act.waived',
    'generation budget bound': 'admin.act.budgetBound',
    'billing packs updated': 'admin.act.packs',
  }[text];
  if (fixed) return t(fixed);
  return text
    .split('; ')
    .map((part) => {
      const [field, value] = part.split(' → ');
      const key = { role: 'admin.act.role.', status: 'admin.act.status.' }[field];
      return key && ['admin', 'user', 'suspended', 'active'].includes(value)
        ? t(key + value)
        : part;
    })
    .join(' · ');
}

// The one-click picture switch records a fixed English note; show it in the page language.
function auditReason(reason, t) {
  return (
    {
      'Pictures turned on in Admin': t('admin.act.turnedOn'),
      'Pictures turned off in Admin': t('admin.act.turnedOff'),
    }[reason] || reason
  );
}

export function AdminAudit({ api, active }) {
  const { t, locale } = useI18n();
  const list = usePaged(api, '/v1/admin/audit', active);
  if (list.state === 'missing') return <p className="muted">{t('admin.notDeployed')}</p>;
  return (
    <div className="card-box">
      {list.error && <p role="alert">{list.error}</p>}
      <div className="audit-log">
        {list.rows.map((row, index) => (
          <div key={row.id || index}>
            <History size={14} aria-hidden="true" />
            <span>
              <b>
                {auditLabel(row.action, t)}
                {row.target && row.target !== 'Story chat pictures' && (
                  <small className="muted"> · {row.target}</small>
                )}
              </b>
              {row.reason && (
                <small className="clip" title={row.reason}>
                  {auditReason(row.reason, t)}
                </small>
              )}
              <small className="muted">
                {[row.actor, apiDate(row.created_at)?.toLocaleString(locale)]
                  .filter(Boolean)
                  .join(' · ')}
              </small>
            </span>
          </div>
        ))}
      </div>
      {list.state === 'ready' && !list.rows.length && <p className="muted">{t('admin.noAudit')}</p>}
      {list.next && (
        <button
          type="button"
          className="ghost small"
          onClick={() => void list.load(list.next, false)}
        >
          {t('credits.more')}
        </button>
      )}
    </div>
  );
}

export default function AdminConsole({ account, active, testMachines }) {
  const { t } = useI18n();
  const api = useClient(account);
  const [tab, setTab] = useState('users');
  return (
    <div className="admin-console">
      <Segmented
        tabs
        className="admin-tabs"
        options={[
          ['users', 'admin.users', Users],
          ['pricing', 'admin.pricing', TrendingUp],
          ['machines', 'admin.machines', ShieldCheck],
          ['audit', 'admin.audit', History],
        ].map(([id, label, Icon]) => ({
          value: id,
          label: (
            <>
              <Icon size={15} aria-hidden="true" />
              {t(label)}
            </>
          ),
        }))}
        value={tab}
        onChange={setTab}
      />
      {tab === 'users' && <AdminUsers api={api} active={active} />}
      {tab === 'pricing' && (
        <>
          <StoryChatAdminSwitch api={api} active={active} />
          <AdminPricing api={api} active={active} />
        </>
      )}
      {tab === 'machines' && testMachines}
      {tab === 'audit' && <AdminAudit api={api} active={active} />}
    </div>
  );
}
