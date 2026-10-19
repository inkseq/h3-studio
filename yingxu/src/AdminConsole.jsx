// Settings → Admin (h3-studio#304): users, audit log and the existing test-machine limits.
// Port of prototype account.jsx AdminPanel. The backend enforces require_admin on every route;
// a tab whose endpoint is not deployed yet says so instead of failing.
import React, { useEffect, useMemo, useState } from 'react';
import { History, ShieldCheck, Users } from 'lucide-react';
import { createCloudClient } from './cloud-client.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import { apiDate, formatCredits } from './credits.js';
import './admin.css';

function useClient(account) {
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

function AdminUsers({ api, active }) {
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
          <span />
        </div>
        {list.rows.map((user) => (
          <React.Fragment key={user.id}>
            <div className={'user-row' + (user.status === 'suspended' ? ' off' : '')}>
              <span>
                <b>
                  {user.display_name || user.email || user.id}
                  {user.status === 'suspended' && (
                    <small className="muted"> · {t('admin.suspended')}</small>
                  )}
                </b>
                <small className="muted clip">
                  {[user.email, (user.providers || []).join(' / ')].filter(Boolean).join(' · ')}
                </small>
              </span>
              <span>{t(user.role === 'admin' ? 'admin.roleAdmin' : 'admin.roleUser')}</span>
              <span className="num">{formatCredits(user.balance, locale)}</span>
              <span className="num">{formatCredits(user.used_30d, locale)}</span>
              <span>
                <button
                  type="button"
                  className="small"
                  aria-expanded={open === user.id}
                  onClick={() => setOpen(open === user.id ? null : user.id)}
                >
                  {t('admin.manage')}
                </button>
              </span>
            </div>
            {open === user.id && (
              <div className="user-adjust">
                <label className="field">
                  <span>{t('admin.reason')}</span>
                  <input
                    value={reason}
                    maxLength={300}
                    placeholder={t('admin.reasonExample')}
                    onChange={(event) => setReason(event.target.value)}
                  />
                </label>
                <label className="field small">
                  <span>{t('admin.delta')}</span>
                  <input
                    inputMode="numeric"
                    value={delta}
                    onChange={(event) => setDelta(event.target.value)}
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
                <button
                  type="button"
                  className="small"
                  disabled={busy}
                  onClick={() =>
                    void update(user, { role: user.role === 'admin' ? 'user' : 'admin' })
                  }
                >
                  {t(user.role === 'admin' ? 'admin.makeUser' : 'admin.makeAdmin')}
                </button>
                <button
                  type="button"
                  className={'small' + (user.status === 'suspended' ? '' : ' danger')}
                  disabled={busy}
                  onClick={() =>
                    void update(user, {
                      status: user.status === 'suspended' ? 'active' : 'suspended',
                    })
                  }
                >
                  {t(user.status === 'suspended' ? 'admin.restore' : 'admin.suspend')}
                </button>
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

function AdminAudit({ api, active }) {
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
              <b>{[row.action, row.target].filter(Boolean).join(' · ')}</b>
              {row.reason && <small className="clip">{row.reason}</small>}
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
      <p className="muted">{t('admin.intro')}</p>
      <div className="seg admin-tabs" role="tablist">
        {[
          ['users', 'admin.users', Users],
          ['machines', 'admin.machines', ShieldCheck],
          ['audit', 'admin.audit', History],
        ].map(([id, label, Icon]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            className={'keep-label' + (tab === id ? ' on' : '')}
            onClick={() => setTab(id)}
          >
            <Icon size={13} aria-hidden="true" />
            {t(label)}
          </button>
        ))}
      </div>
      {tab === 'users' && <AdminUsers api={api} active={active} />}
      {tab === 'machines' && testMachines}
      {tab === 'audit' && <AdminAudit api={api} active={active} />}
    </div>
  );
}
