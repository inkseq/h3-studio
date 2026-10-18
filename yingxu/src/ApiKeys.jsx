import { useI18n } from './i18n/I18nProvider.jsx';
import './i18n/workspace-language.css';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Copy, KeyRound, RefreshCw, ShieldCheck, X } from 'lucide-react';
import { createCloudClient } from './cloud-client.js';
import { translate } from './i18n/locale.js';
import './api-keys.css';

const profiles = {
  create: {
    labelKey: 'account.status59',
    scopes: [
      'projects:read',
      'projects:create',
      'projects:write',
      'assets:read',
      'assets:write',
      'jobs:read',
      'jobs:write',
    ],
  },
  edit: {
    labelKey: 'account.status60',
    scopes: [
      'projects:read',
      'projects:create',
      'projects:write',
      'assets:read',
      'assets:write',
      'jobs:read',
    ],
  },
  read: { labelKey: 'account.status61', scopes: ['projects:read', 'assets:read', 'jobs:read'] },
};
const date = (value, locale = 'zh') =>
  value
    ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString(
        locale === 'zh' ? 'zh-CN' : 'en',
      )
    : translate(locale, 'account.more.unused');

export default function ApiKeys({
  account,
  projects = [],
  moreProjects = false,
  initialGrant = null,
}) {
  const { t, locale } = useI18n();
  const client = useMemo(() => {
    const api = createCloudClient();
    api.setAccount(account);
    return api;
  }, [account]);
  const live = useRef(false),
    [keys, setKeys] = useState([]),
    [name, setName] = useState(''),
    [profile, setProfile] = useState(
      Object.hasOwn(profiles, initialGrant?.profile) ? initialGrant.profile : 'edit',
    ),
    [allProjects, setAllProjects] = useState(initialGrant?.allProjects ?? true),
    [selected, setSelected] = useState(initialGrant?.projectIds || []),
    [days, setDays] = useState(initialGrant?.expiresInDays || 7),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [notice, setNotice] = useState(''),
    [created, setCreated] = useState(null),
    [reveal, setReveal] = useState(false),
    [confirmRevoke, setConfirmRevoke] = useState(null);
  async function refresh() {
    const data = await client.apiKeys();
    if (live.current) setKeys(data.api_keys || []);
  }
  useEffect(() => {
    live.current = true;
    setCreated(null);
    setKeys([]);
    setError('');
    setBusy(true);
    refresh()
      .catch(() => {
        if (live.current) setError(t('account.status62'));
      })
      .finally(() => {
        if (live.current) setBusy(false);
      });
    return () => {
      live.current = false;
      client.reset();
    };
  }, [client]);
  async function act(fn) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await fn();
    } catch {
      if (live.current) setError(t('account.status63'));
    } finally {
      if (live.current) setBusy(false);
    }
  }
  async function create(e) {
    e.preventDefault();
    await act(async () => {
      const scopes = profiles[profile].scopes.filter(
        (scope) => allProjects || scope !== 'projects:create',
      );
      const result = await client.createApiKey({
        name: name.trim(),
        scopes,
        all_projects: allProjects,
        project_ids: allProjects ? [] : selected,
        expires_in_days: Number(days),
      });
      if (!live.current) return;
      if (typeof result.api_key !== 'string' || !result.api_key) throw Error('Missing key');
      setCreated({ id: result.key.id, secret: result.api_key });
      setReveal(false);
      setName('');
      setNotice(t('account.status64'));
      try {
        await refresh();
      } catch {
        if (live.current) setError(t('account.status65'));
      }
    });
  }
  async function copy() {
    try {
      await navigator.clipboard.writeText(created.secret);
      setNotice(t('account.status66'));
    } catch {
      setReveal(true);
      setNotice(t('account.status67'));
    }
  }
  async function revoke(id) {
    await act(async () => {
      await client.revokeApiKey(id);
      if (!live.current) return;
      setConfirmRevoke(null);
      if (created?.id === id) {
        setCreated(null);
        setReveal(false);
      }
      setNotice(t('account.status68'));
      try {
        await refresh();
      } catch {
        if (live.current) setError(t('account.status69'));
      }
    });
  }
  return (
    <section className="api-key-panel" aria-label={t('account.more.keyTitle')}>
      <div className="cloud-section-head">
        <h3>
          <KeyRound size={17} /> {t('account.more.keyTitle')}
        </h3>
        <button type="button" disabled={busy} onClick={() => act(refresh)}>
          <RefreshCw size={14} />
          {t('account.copy31')}
        </button>
      </div>
      <p>
        {t('account.more.intro')}
        <b>{account}</b>
        {t('account.more.ownerHelp')}
      </p>
      {created && (
        <section className="api-key-created" aria-label={t('account.accessible10')}>
          <div>
            <b>{t('account.copy32')}</b>
            <button
              type="button"
              aria-label={t('account.accessible11')}
              onClick={() => {
                setCreated(null);
                setReveal(false);
              }}
            >
              <X size={16} />
            </button>
          </div>
          <input
            aria-label={t('account.accessible12')}
            autoComplete="off"
            type={reveal ? 'text' : 'password'}
            value={created.secret}
            readOnly
            spellCheck={false}
          />
          <div>
            <button type="button" onClick={() => setReveal(!reveal)}>
              {reveal ? t('account.status70') : t('account.status71')}
            </button>
            <button type="button" onClick={copy}>
              <Copy size={14} />
              {t('account.copy33')}
            </button>
          </div>
          <small>{t('account.copy34')}</small>
        </section>
      )}
      <p>
        <a href="/v1/agent-skill.zip" download>
          {t('account.copy35')}
        </a>{' '}
        ·{' '}
        <a href="/v1/agent-guide" target="_blank" rel="noreferrer">
          {t('account.copy36')}
        </a>
      </p>
      <form onSubmit={create} className="api-key-form">
        <label>
          {t('account.copy37')}
          <input
            required
            maxLength={80}
            placeholder={t('account.accessible13')}
            value={name}
            disabled={busy}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <div className="api-key-options">
          <label>
            {t('account.copy38')}
            <select value={profile} disabled={busy} onChange={(e) => setProfile(e.target.value)}>
              {Object.entries(profiles).map(([id, p]) => (
                <option key={id} value={id}>
                  {t(p.labelKey)}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t('account.copy39')}
            <select value={days} disabled={busy} onChange={(e) => setDays(Number(e.target.value))}>
              {[7, 30, 90, 365].map((n) => (
                <option key={n} value={n}>
                  {t('account.more.days', { days: n })}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label>
          {t('account.copy40')}
          <select
            value={allProjects ? 'all' : 'selected'}
            disabled={busy}
            onChange={(e) => setAllProjects(e.target.value === 'all')}
          >
            <option value="all">{t('account.copy41')}</option>
            <option value="selected">{t('account.copy42')}</option>
          </select>
        </label>
        {!allProjects && (
          <fieldset>
            <legend>{t('account.copy43')}</legend>
            {projects.map((p) => (
              <label key={p.id} className="api-key-project">
                <input
                  type="checkbox"
                  checked={selected.includes(p.id)}
                  disabled={busy}
                  onChange={(e) =>
                    setSelected(
                      e.target.checked ? [...selected, p.id] : selected.filter((id) => id !== p.id),
                    )
                  }
                />
                {p.title}
              </label>
            ))}
            {!projects.length && <p>{t('account.copy44')}</p>}
            {moreProjects && <small>{t('account.copy45')}</small>}
            <small>{t('account.copy46')}</small>
          </fieldset>
        )}
        <button
          className="primary"
          disabled={busy || !name.trim() || (!allProjects && !selected.length)}
        >
          <KeyRound size={14} />
          {t('account.copy47')}
        </button>
      </form>
      {error && (
        <p role="alert" className="cloud-error">
          {error}
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      <div className="api-key-list">
        {keys.map((k) => (
          <article key={k.id}>
            <div>
              <b>{k.name}</b>
              <code>{k.prefix}…</code>
              <small>
                {k.all_projects
                  ? t('account.status72')
                  : t('account.more.stories', { count: k.project_ids?.length || 0 })}{' '}
                ·{' '}
                {k.revoked_at
                  ? t('account.status73')
                  : Number(k.expires_at) * 1000 < Date.now()
                    ? t('account.status74')
                    : t('account.status75')}
              </small>
              <small>
                {t('account.more.dates', {
                  expires: date(k.expires_at, locale),
                  used: date(k.last_used_at, locale),
                })}
              </small>
              <details>
                <summary>{t('account.copy48')}</summary>
                <p>{k.scopes?.join('、')}</p>
              </details>
            </div>
            {!k.revoked_at &&
              (confirmRevoke === k.id ? (
                <div className="api-key-revoke">
                  <p>{t('account.copy49')}</p>
                  <button type="button" disabled={busy} onClick={() => revoke(k.id)}>
                    {t('account.copy50')}
                  </button>
                  <button type="button" disabled={busy} onClick={() => setConfirmRevoke(null)}>
                    {t('account.copy51')}
                  </button>
                </div>
              ) : (
                <button type="button" disabled={busy} onClick={() => setConfirmRevoke(k.id)}>
                  {t('account.copy52')}
                </button>
              ))}
          </article>
        ))}
        {!busy && !keys.length && <p className="muted">{t('account.copy53')}</p>}
      </div>
      <p className="api-key-hint">
        <ShieldCheck size={15} />
        {t('account.copy54')}
      </p>
    </section>
  );
}
