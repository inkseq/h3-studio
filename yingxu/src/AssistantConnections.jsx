import { SettingRow, SettingsSection } from './ui/Settings.jsx';
import { Button } from './ui/Button.jsx';
import TelegramLink, { telegramUserLabel, validTelegramNonce } from './TelegramLink.jsx';
import { Segmented } from './ui/Segmented.jsx';

// Daily credit cap suggested for a new Telegram link; USD caps (admins) start uncapped.
const TELEGRAM_DEFAULT_CAP = '200';
import { microsToDollars } from './money.js';
import SpendingBreakdown from './SpendingBreakdown.jsx';
import { Bot } from 'lucide-react';
import { assistantCatalogs } from './i18n/assistantCatalogs.js';
import React, { useEffect, useMemo, useRef, useState, useEffectEvent } from 'react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { createQuickChatClient } from './quick-chat-client.js';
import {
  capMicros,
  capCredits,
  assistantScope,
  createAssistantOperations,
  creatorName,
  assistantDate,
  mergeConnectionResult,
  assistantConnectionsView,
} from './assistant-connections-model.js';
import { usePageActive } from './AppRouteCache.jsx';
import { useAccess } from './StudioHeader.jsx';
import './v2/ops-adapter.css';
const dollars = microsToDollars;
export function AssistantBadge({ createdBy }) {
  const { t } = useI18n(),
    name = creatorName(createdBy);
  return name ? (
    <span className="assistant-badge" title={t('assistants.badge', { name })}>
      <Bot size={13} aria-hidden="true" />
      {name}
    </span>
  ) : null;
}
export default function AssistantConnections({
  creditMode = false,
  account,
  sessionId,
  projectId,
  onLogin,
  embedded = false,
  active = true,
  telegram = false,
  telegramNonce = null,
}) {
  // Users never see dollars (#304): spending caps show in credits, or in USD to admins only.
  const access = useAccess(account),
    money = creditMode || !!access?.admin;
  const { t, locale } = useI18n(),
    client = useMemo(() => {
      const c = createQuickChatClient();
      c.setAccount(account);
      return c;
    }, [account]);
  const pageActive = usePageActive(),
    visible = useRef(pageActive && active);
  visible.current = pageActive && active;
  const [rows, setRows] = useState([]),
    [showInactive, setShowInactive] = useState(false),
    [connectionsPage, setConnectionsPage] = useState(null),
    [profile, setProfile] = useState(null),
    [settings, setSettings] = useState(null),
    [settingsDraft, setSettingsDraft] = useState(null),
    [operation, setOperation] = useState({}),
    [created, setCreated] = useState(null),
    [scope, setScope] = useState(sessionId ? 'session' : projectId ? 'story' : 'all'),
    [permission, setPermission] = useState('generate'),
    [cap, setCap] = useState(telegram ? (creditMode ? TELEGRAM_DEFAULT_CAP : '0') : '0'),
    [name, setName] = useState(telegram ? 'Telegram' : 'Codex'),
    [target, setTarget] = useState(telegram ? 'telegram' : 'agent'),
    [error, setError] = useState(''),
    [notice, setNotice] = useState(''),
    [loading, setLoading] = useState(false),
    [activity, setActivity] = useState(null),
    [now, setNow] = useState(Date.now()),
    epoch = useRef(0),
    ops = useRef(null),
    refreshSequence = useRef(0),
    activitySequence = useRef(0),
    connectionsCursor = useRef(undefined);
  // Only an unfinished operation from before (needing recovery) locks the page. A change in
  // flight locks just its own row; other clicks wait their turn (owner, 2026-10-26).
  const date = (value) => assistantDate(value, locale, t('assistants.dateUnavailable')),
    locked = !!operation.pending && !operation.busy,
    busy = locked || (loading && !rows.length),
    busyId = operation.busy ? operation.pending?.args?.[0] : null;
  const scopeLabel = (value) =>
    t(
      'assistants.' +
        (['session', 'story', 'all'].includes(value?.kind) ? value.kind : 'legacyScope'),
    );
  const permissionLabel = (value) =>
    t('assistants.' + (['organise', 'generate'].includes(value) ? value : 'legacyPermission'));
  // Credit mode is known only after /v1/credits loads; then suggest the Telegram cap once.
  useEffect(() => {
    if (target === 'telegram' && creditMode)
      setCap((old) => (old === '0' ? TELEGRAM_DEFAULT_CAP : old));
  }, [target, creditMode]);
  const codeAvailable = created?.code && created.connection.expires_at * 1000 > now;
  async function refresh() {
    const at = epoch.current,
      seq = ++refreshSequence.current;
    const [list, defaults] = await Promise.all([client.connections(), client.connectionSettings()]);
    if (at !== epoch.current || seq !== refreshSequence.current) return;
    setRows((old) => [
      ...(list.connections || []),
      ...old.filter((row) => !(list.connections || []).some((current) => current.id === row.id)),
    ]);
    if (connectionsCursor.current === undefined) {
      connectionsCursor.current = list.has_more ? list.next_offset : null;
      setConnectionsPage(connectionsCursor.current);
    }
    setProfile(list.authorization_profile);
    setSettings((old) => old || defaults);
    setSettingsDraft(
      (old) =>
        old || Object.fromEntries(Object.entries(defaults.value).map(([k, v]) => [k, String(v)])),
    );
    setCreated((old) => {
      if (!old) return null;
      const row = list.connections?.find((x) => x.id === old.connection.id);
      return old.connection.expires_at * 1000 <= Date.now() || (row && row.status !== 'pending')
        ? null
        : old;
    });
  }
  const refreshEvent = useEffectEvent((...args) => refresh(...args));
  // Defaults for a new connection, read when the scope changes; a later credit-mode change
  // does not wipe what the person typed.
  const resetDefaults = useEffectEvent(() => {
    // The bot makes films, so a Telegram link defaults to generate with a modest daily cap.
    setName(target === 'telegram' ? 'Telegram' : 'Codex');
    setCap(target === 'telegram' ? (creditMode ? TELEGRAM_DEFAULT_CAP : '0') : '0');
  });
  useEffect(() => {
    const at = ++epoch.current;
    setRows([]);
    setShowInactive(false);
    setConnectionsPage(null);
    connectionsCursor.current = undefined;
    activitySequence.current++;
    setCreated(null);
    resetDefaults();
    setPermission('generate');
    setSettings(null);
    setSettingsDraft(null);
    setActivity(null);
    setError('');
    setNotice('');
    setScope(sessionId ? 'session' : projectId ? 'story' : 'all');
    const current = createAssistantOperations({
      client,
      account,
      onChange: (value) => {
        if (epoch.current === at) setOperation(value);
      },
    });
    ops.current = current;
    setOperation(current.read());
    let timer;
    if (account) {
      setLoading(true);
      refreshEvent()
        .catch(() => {
          if (epoch.current === at) setError(t('assistants.error'));
        })
        .finally(() => {
          if (epoch.current === at) setLoading(false);
        });
      const poll = async () => {
        if (epoch.current !== at) return;
        if (visible.current) {
          setNow(Date.now());
          try {
            await refreshEvent();
          } catch {}
        }
        if (epoch.current === at) timer = setTimeout(poll, 5000);
      };
      timer = setTimeout(poll, 5000);
    }
    const epochRef = epoch;
    return () => {
      epochRef.current++;
      current.destroy();
      clearTimeout(timer);
      client.reset();
    };
  }, [client, account, sessionId, projectId, t]);
  const queue = useRef(Promise.resolve());
  function act(fn) {
    const run = queue.current.catch(() => {}).then(() => actNow(fn));
    queue.current = run;
    return run;
  }
  async function actNow(fn) {
    const at = epoch.current;
    setError('');
    setNotice('');
    try {
      const result = await fn();
      if (at !== epoch.current) return;
      setRows((old) => mergeConnectionResult(old, result));
      if (result !== undefined)
        setNotice(
          t(
            result?.no_mutation_committed === true
              ? 'assistants.conflict'
              : result?.code_available === false
                ? 'assistants.lost'
                : 'assistants.saved',
          ),
        );
      await refresh();
      return result;
    } catch (e) {
      if (at === epoch.current)
        setError(
          t(
            typeof e.message === 'string' && e.message.startsWith('assistants.')
              ? e.message
              : 'assistants.error',
          ),
        );
    }
  }
  async function issue(event) {
    event.preventDefault();
    const at = epoch.current;
    await act(async () => {
      const result = await ops.current.execute('createConnection', [], {
        name: name.trim(),
        authorization_profile_id: profile.id,
        authorization_profile_version: profile.version,
        scope: assistantScope(target === 'telegram' ? 'all' : scope, { sessionId, projectId }),
        permission,
        // Telegram grants say so; the bot-first page also binds the bot's one-time nonce (#382).
        ...(target === 'telegram' ? { client: 'telegram' } : {}),
        ...(target === 'telegram' && validTelegramNonce(telegramNonce)
          ? { telegram_nonce: telegramNonce }
          : {}),
        ...(creditMode
          ? { daily_limit_credits: capCredits(cap) }
          : { daily_limit_microusd: capMicros(cap) }),
      });
      if (at === epoch.current) {
        setCreated(result.code ? result : null);
        if (!result.code) setNotice(t('assistants.lost'));
      }
      return result;
    });
  }
  async function recover() {
    const at = epoch.current;
    await act(async () => {
      const method = operation.pending?.method,
        result = await ops.current.recover();
      if (at === epoch.current && method === 'createConnection') {
        setCreated(result.code ? result : null);
        if (!result.code) setNotice(t('assistants.lost'));
      }
      return result;
    });
  }
  async function activityPage(row, more = false) {
    const sequence = ++activitySequence.current,
      at = epoch.current,
      offset = more ? activity.next_offset : 0;
    await act(async () => {
      const result = await client.connectionActivity(row.id, offset);
      if (at !== epoch.current || sequence !== activitySequence.current) return;
      setActivity((old) => ({
        id: row.id,
        events: more && old?.id === row.id ? [...old.events, ...result.events] : result.events,
        next_offset: result.next_offset,
        has_more: result.has_more,
      }));
    });
  }
  async function moreConnections() {
    const at = epoch.current,
      offset = connectionsPage;
    await act(async () => {
      const result = await client.connections(offset);
      if (at !== epoch.current) return;
      setRows((old) => [
        ...old,
        ...result.connections.filter((row) => !old.some((prior) => prior.id === row.id)),
      ]);
      connectionsCursor.current = result.has_more ? result.next_offset : null;
      setConnectionsPage(connectionsCursor.current);
    });
  }
  async function defaults(event) {
    event.preventDefault();
    const at = epoch.current;
    await act(async () => {
      const value = Object.fromEntries(
        Object.entries(settingsDraft).map(([k, v]) => {
          const n = Number(v);
          if (!Number.isSafeInteger(n) || n < 1) throw Error('assistants.invalid');
          return [k, n];
        }),
      );
      const result = await ops.current.execute('saveConnectionSettings', [], {
        expected_version: settings.version,
        value,
      });
      if (at !== epoch.current) return;
      setSettings(result);
      setSettingsDraft(
        Object.fromEntries(Object.entries(result.value).map(([k, v]) => [k, String(v)])),
      );
      return result;
    });
  }
  const connection = created?.connection,
    paragraph = codeAvailable
      ? t('assistants.paragraph', {
          origin: location.origin,
          skill: location.origin + '/for-agents/guide.md',
          code: created.code,
          scope: scopeLabel(connection.scope),
          permission: permissionLabel(connection.permission),
          // #322: no cap sentence for view-only connections, and 0 means "no cap of its own", never "0 USD".
          capSentence:
            connection.permission === 'organise'
              ? t('assistants.capSentence.organise')
              : connection.unit === 'credits'
                ? !connection.daily_limit_credits
                  ? t('assistants.capSentence.none')
                  : t('assistants.capSentence.capCredits', { cap: connection.daily_limit_credits })
                : !connection.daily_limit_microusd
                  ? t('assistants.capSentence.none')
                  : t('assistants.capSentence.cap', {
                      cap: dollars(connection.daily_limit_microusd),
                    }),
          date: date(connection.expires_at),
        })
      : '';
  const metadata = codeAvailable
    ? JSON.stringify(
        {
          connection_id: connection.id,
          authorization_fingerprint: connection.authorization_fingerprint,
          authorization: connection.authorization,
        },
        null,
        2,
      )
    : '';
  return (
    <section className="assistant-connections">
      {!embedded && <h2>{t('assistants.title')}</h2>}
      {!account ? (
        <div className="assistant-signin">
          <p>{t('assistants.login')}</p>
          <Button variant="primary" className="keep-label" onClick={onLogin}>
            {t('story.copy14')}
          </Button>
        </div>
      ) : (
        <>
          {operation.pending && (
            <aside role="status">
              <p>{t('assistants.wait')}</p>
              {operation.pending.body?.scope && (
                <p>
                  {scopeLabel(operation.pending.body.scope)} ·{' '}
                  {permissionLabel(operation.pending.body.permission)}
                </p>
              )}
              <Button disabled={operation.busy || operation.pending.corrupt} onClick={recover}>
                {t('assistants.recover')}
              </Button>
            </aside>
          )}
          {!codeAvailable ? (
            <form onSubmit={issue} className="ui-form">
              <SettingsSection
                help={target === 'telegram' ? t('telegram.explain') : null}
                actions={
                  <Button
                    variant="primary"
                    type="submit"
                    disabled={busy || !profile || !name.trim()}
                  >
                    {t(target === 'telegram' ? 'telegram.connect' : 'assistants.connect')}
                  </Button>
                }
              >
                {!telegram && (
                  <div className="ui-settings-tabs">
                    <Segmented
                      className="assistant-target"
                      label={t('telegram.target')}
                      options={[
                        { value: 'agent', label: t('telegram.agentTab') },
                        { value: 'telegram', label: t('telegram.tab') },
                      ]}
                      value={target}
                      onChange={(value) => {
                        setTarget(value);
                        setName(value === 'telegram' ? 'Telegram' : 'Codex');
                        setPermission('generate');
                        setCap(
                          value === 'telegram' ? (creditMode ? TELEGRAM_DEFAULT_CAP : '0') : '0',
                        );
                      }}
                    />
                  </div>
                )}
                <fieldset disabled={busy || !profile}>
                  {target === 'telegram' ? (
                    <SettingRow as="div" label={t('assistants.scope')}>
                      {t('assistants.all')}
                    </SettingRow>
                  ) : (
                    <SettingRow label={t('assistants.scope')} wide>
                      <select value={scope} onChange={(e) => setScope(e.target.value)}>
                        {sessionId && <option value="session">{t('assistants.session')}</option>}
                        {projectId && <option value="story">{t('assistants.story')}</option>}
                        <option value="all">{t('assistants.all')}</option>
                      </select>
                    </SettingRow>
                  )}
                  <SettingRow label={t('assistants.name')} wide>
                    <input
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      maxLength={80}
                      required
                    />
                  </SettingRow>
                  {/* Assistants are connected to create; view-only was never used (owner,
                      2026-10-22). Paid work stays bounded by this daily cap and per-job
                      confirmation. */}
                  {money && (
                    <SettingRow
                      label={t(creditMode ? 'assistants.capCredits' : 'assistants.cap')}
                      help={t('assistants.zero')}
                    >
                      <input
                        inputMode={creditMode ? 'numeric' : 'decimal'}
                        value={cap}
                        onChange={(e) => setCap(e.target.value)}
                      />
                    </SettingRow>
                  )}
                </fieldset>
              </SettingsSection>
            </form>
          ) : target === 'telegram' ? (
            <TelegramLink
              code={created.code}
              expiresAt={connection.expires_at}
              autoOpen={telegram}
              onHide={() => setCreated(null)}
            />
          ) : (
            <article>
              <h3>{t('assistants.waiting')}</h3>
              <label>
                {t('assistants.paste')}
                <textarea readOnly value={paragraph} rows={5} />
              </label>
              <details>
                <summary>{t('assistants.technical')}</summary>
                <pre>{metadata}</pre>
              </details>
              <div className="assistant-actions">
                <Button
                  onClick={async () => {
                    const at = epoch.current;
                    try {
                      if (!created?.code || created.connection.expires_at * 1000 <= Date.now())
                        throw Error('expired');
                      await navigator.clipboard.writeText(paragraph + '\n' + metadata);
                      if (at === epoch.current) setNotice(t('assistants.copied'));
                    } catch {
                      if (at === epoch.current) setNotice(t('assistants.copyFailed'));
                    }
                  }}
                >
                  {t('assistants.copy')}
                </Button>
                <Button onClick={() => setCreated(null)}>{t('assistants.hide')}</Button>
              </div>
              <small>{t('assistants.single', { date: date(connection.expires_at) })}</small>
            </article>
          )}
          <ConnectionList
            rows={rows}
            showInactive={showInactive}
            onToggleInactive={() => setShowInactive((value) => !value)}
            hasMore={connectionsPage !== null}
            pendingId={operation.pending?.args?.[0]}
            busy={busy}
            busyId={busyId}
            scopeLabel={scopeLabel}
            permissionLabel={permissionLabel}
            money={money}
            onPatch={(row, body) =>
              act(() => ops.current.execute('updateConnection', [row.id], body))
            }
            onRevoke={(row) => {
              if (window.confirm(t('assistants.revokeConfirm')))
                void act(() => ops.current.execute('revokeConnection', [row.id]));
            }}
            onActivity={activityPage}
          />
          {connectionsPage !== null && (
            <Button disabled={busy} onClick={moreConnections}>
              {t('assistants.more')}
            </Button>
          )}
          {activity && (
            <article>
              <h3>{t('assistants.activity')}</h3>
              {activity.events.map((event) => (
                <p key={event.id}>
                  {t('assistants.activityEvent', {
                    event: t(
                      Object.hasOwn(assistantCatalogs.en, 'assistants.event.' + event.event)
                        ? 'assistants.event.' + event.event
                        : 'assistants.eventUnknown',
                    ),
                    date: date(event.created_at),
                  })}
                </p>
              ))}
              {!activity.events.length && <p>{t('assistants.noActivity')}</p>}
              {activity.has_more && (
                <Button disabled={busy} onClick={() => activityPage({ id: activity.id }, true)}>
                  {t('assistants.more')}
                </Button>
              )}
            </article>
          )}
          {settings && settingsDraft && (
            <details className="assistant-defaults">
              <summary>{t('assistants.settings')}</summary>
              <form onSubmit={defaults} className="ui-form">
                <SettingsSection
                  help={t('assistants.settingsHelp')}
                  actions={
                    <Button type="submit" disabled={busy}>
                      {t('assistants.saveSettings')}
                    </Button>
                  }
                >
                  {Object.keys(settings.value).map((field) => (
                    <SettingRow key={field} label={t('assistants.' + field)}>
                      <input
                        type="number"
                        min="1"
                        step="1"
                        disabled={busy}
                        value={settingsDraft[field]}
                        onChange={(e) =>
                          setSettingsDraft({ ...settingsDraft, [field]: e.target.value })
                        }
                      />
                    </SettingRow>
                  ))}
                </SettingsSection>
              </form>
            </details>
          )}
          <Button
            size="sm"
            className="assistant-refresh"
            disabled={busy}
            onClick={() => act(refresh)}
          >
            {t('assistants.refresh')}
          </Button>
        </>
      )}
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}
export function ConnectionList({
  rows,
  showInactive = false,
  onToggleInactive,
  hasMore = false,
  pendingId,
  busy,
  busyId = null,
  scopeLabel,
  permissionLabel,
  money = true,
  onPatch,
  onRevoke,
  onActivity,
}) {
  const { t } = useI18n(),
    view = assistantConnectionsView(rows, { showInactive, pendingId });
  return (
    <>
      <h3>{t('assistants.list')}</h3>
      {view.items.map(({ row, hidden }) => (
        <ConnectionRow
          key={row.id}
          row={row}
          hidden={hidden}
          busy={busy || busyId === row.id}
          t={t}
          scopeLabel={scopeLabel}
          permissionLabel={permissionLabel}
          money={money}
          onPatch={(body) => onPatch(row, body)}
          onRevoke={() => onRevoke(row)}
          onActivity={() => onActivity(row)}
        />
      ))}
      {view.inactiveCount > 0 && (
        <Button
          className="link assistant-inactive-toggle"
          aria-expanded={showInactive}
          onClick={onToggleInactive}
        >
          {t(
            showInactive
              ? 'assistants.hideInactive'
              : hasMore
                ? 'assistants.showLoadedInactive'
                : 'assistants.showInactive',
            { count: view.inactiveCount },
          )}
        </Button>
      )}
      {!rows.length && <p>{t('assistants.empty')}</p>}
      {rows.length > 0 && view.activeCount === 0 && !showInactive && (
        <p>{t('assistants.noActive')}</p>
      )}
    </>
  );
}
function ConnectionRow({
  row,
  hidden,
  busy,
  t,
  scopeLabel,
  permissionLabel,
  money,
  onPatch,
  onRevoke,
  onActivity,
}) {
  const { locale } = useI18n();
  const [draft, setDraft] = useState(null),
    [error, setError] = useState('');
  const editable = ['connected', 'paused'].includes(row.status),
    closed = ['revoked', 'expired'].includes(row.status);
  const credit = row.unit === 'credits',
    showCap = credit || money;
  const cap =
    draft?.value ??
    (credit ? String(row.daily_limit_credits ?? 0) : dollars(row.daily_limit_microusd));
  async function save(e) {
    e.preventDefault();
    setError('');
    try {
      const result = await onPatch({
        expected_version: draft?.version ?? row.state_version,
        ...(credit
          ? { daily_limit_credits: capCredits(cap) }
          : { daily_limit_microusd: capMicros(cap) }),
      });
      if (result) setDraft(null);
    } catch {
      setError(t('assistants.invalid'));
    }
  }
  return (
    <article hidden={hidden} data-connection-id={row.id} className="assistant-row">
      <div className="assistant-row-head">
        <strong>{row.name}</strong>
        <span className={'assistant-status ' + (row.paused ? 'paused' : row.status)}>
          {t(
            'assistants.' +
              (row.paused
                ? 'paused'
                : ['pending', 'connected', 'expired', 'revoked', 'unavailable'].includes(row.status)
                  ? row.status
                  : 'unavailable'),
          )}
        </span>
        <span className="assistant-row-scope">{scopeLabel(row.scope)}</span>
        <div className="assistant-actions">
          <Button
            size="sm"
            disabled={busy || !editable}
            onClick={() => onPatch({ expected_version: row.state_version, paused: !row.paused })}
          >
            {t(row.paused ? 'assistants.resume' : 'assistants.pause')}
          </Button>
          <Button
            size="sm"
            title={t('assistants.workStays')}
            disabled={busy || closed}
            onClick={onRevoke}
          >
            {t('assistants.revoke')}
          </Button>
        </div>
      </div>
      <details className="assistant-row-details">
        <summary>{t('assistants.details')}</summary>
        <p className="assistant-connection-created">
          {t('assistants.created', {
            date: assistantDate(row.created_at, locale, t('assistants.dateUnavailable')),
          })}{' '}
          · {t('assistants.shortId', { id: row.id.slice(-8) })}
        </p>
        {row.claimed_telegram_user && (
          <p className="assistant-connection-claimed">
            {t('telegram.claimedBy', { name: telegramUserLabel(row.claimed_telegram_user) })}
          </p>
        )}
        <p>
          {scopeLabel(row.scope)} · {permissionLabel(row.permission)}
        </p>
        {showCap && <SpendingBreakdown usage={row.usage} scope="connection" />}
        <form onSubmit={save} hidden={!showCap}>
          <label>
            <span className="assistant-label-help" title={t('assistants.zero')}>
              {t(credit ? 'assistants.capCredits' : 'assistants.cap')} <i>?</i>
            </span>
            <input
              inputMode={credit ? 'numeric' : 'decimal'}
              value={cap}
              onChange={(e) =>
                setDraft((old) => ({
                  value: e.target.value,
                  version: old?.version ?? row.state_version,
                }))
              }
              disabled={busy || !editable}
            />
          </label>
          <Button type="submit" disabled={busy || !editable}>
            {t('assistants.saveCap')}
          </Button>
          {draft && (
            <Button disabled={busy} onClick={() => setDraft(null)}>
              {t('assistants.reloadCap')}
            </Button>
          )}
          {error && <p role="alert">{error}</p>}
        </form>
        <Button size="sm" disabled={busy} onClick={onActivity}>
          {t('assistants.activity')}
        </Button>
      </details>
    </article>
  );
}
