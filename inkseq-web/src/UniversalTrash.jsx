import StudioHeader from './StudioHeader.jsx';
import { useCloud } from './use-cloud.js';
import { Disclosure } from './ui/Disclosure.jsx';
import { Segmented } from './ui/Segmented.jsx';
import { Button } from './ui/Button.jsx';
import { Dialog, DialogActions } from './ui/Dialog.jsx';
import { Field } from './ui/Field.jsx';
import CreationHistory from './CreationHistory.jsx';
import { createQuickChatClient } from './quick-chat-client.js';
import { createQuickChatController } from './quick-chat-controller.js';
import React, {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  useEffectEvent,
} from 'react';
import { TrashProvider, useTrash } from './trash-provider.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import ItemMenu from './ItemMenu.jsx';
import { MenuItem } from './ui/Menu.jsx';
import {
  BookOpen,
  Box,
  Clapperboard,
  Film,
  Image,
  Layers,
  MapPin,
  Music,
  RotateCcw,
  Trash2,
  UserRound,
  SlidersHorizontal,
} from 'lucide-react';
import { usePageActive, useAppLocation } from './AppRouteCache.jsx';
import { navigateApp } from './app-navigation.js';
import './v2/ops-adapter.css';
const trashIcon = (kind) =>
  ({
    project: BookOpen,
    chapter: BookOpen,
    scene: Clapperboard,
    shot: Clapperboard,
    character: UserRound,
    look: UserRound,
    location: MapPin,
    location_version: MapPin,
    prop: Box,
    image: Image,
    video: Film,
    audio: Music,
    artifact: Layers,
    take: Layers,
  })[kind] || Box;
export function TrashMenu({
  target,
  onRename,
  onEdit,
  onChat,
  onTrashSettled,
  disabled = false,
  onMedia = false,
}) {
  const ctx = useTrash(),
    { t } = useI18n();
  if (!ctx?.account) return null;
  return (
    <ItemMenu disabled={disabled || ctx.busy || !!ctx.pending} onMedia={onMedia}>
      {onRename && <MenuItem onSelect={onRename}>{t('trash.rename')}</MenuItem>}
      {onEdit && <MenuItem onSelect={onEdit}>{t('trash.edit')}</MenuItem>}
      {onChat && <MenuItem onSelect={onChat}>{t('trash.editInChat')}</MenuItem>}
      <MenuItem
        danger
        onSelect={async (event) => {
          // A menu opened inside a dialog (the story item list) renders inside it, so the
          // row can close that dialog once the item has really moved to the trash.
          const dialog = event.target?.closest?.('dialog');
          const receipt = await ctx.request('trash', [target]);
          if (dialog?.isConnected) {
            if (onTrashSettled) onTrashSettled();
            else if (receipt?.state === 'completed')
              dialog.dispatchEvent(new Event('inkseq-close-after-trash'));
          }
        }}
      >
        {t('trash.move')}
      </MenuItem>
    </ItemMenu>
  );
}
// One-click "move to trash" for panels with a single obvious action (no menu).
// With a `label` it is a plain text button, e.g. "Delete this story".
export function TrashButton({ target, onTrashSettled, label, disabled = false }) {
  const ctx = useTrash(),
    { t } = useI18n();
  if (!ctx?.account) return null;
  return (
    <Button
      className={label ? 'ghost danger trash-button-text' : 'ghost round trash-button'}
      title={label || t('trash.move')}
      aria-label={label || t('trash.move')}
      disabled={disabled || ctx.busy || !!ctx.pending}
      onClick={async () => {
        const receipt = await ctx.request('trash', [target]);
        if (receipt?.state === 'completed' || !label) onTrashSettled?.();
      }}
    >
      <Trash2 size={15} />
      {label}
    </Button>
  );
}
export default function UniversalTrash() {
  const ctx = useTrash(),
    { sessionReady } = useCloud(),
    { t, locale } = useI18n(),
    pageActive = usePageActive(),
    location = useAppLocation(),
    [rows, setRows] = useState([]),
    [next, setNext] = useState(null),
    [kind, setKind] = useState(''),
    [group, setGroup] = useState('all'),
    [selected, setSelected] = useState([]),
    [loading, setLoading] = useState(false),
    [quickCount, setQuickCount] = useState(null),
    [error, setError] = useState(''),
    epoch = useRef(0);
  useEffect(() => {
    if (pageActive) document.title = t('trash.title');
  }, [pageActive, t]);
  useEffect(() => {
    if (pageActive && new URLSearchParams(location.search).get('kind') === 'conversation') {
      setGroup('session');
      setKind('conversation');
      setSelected([]);
    }
  }, [pageActive, location.search]);
  const account = ctx?.account,
    revision = ctx?.revision;
  useEffect(() => {
    let live = true;
    setQuickCount(null);
    if (!account) return;
    const client = createQuickChatClient();
    client.setAccount(account);
    client
      .sessions({ view: 'trash' })
      .then((value) => {
        if (live && Array.isArray(value.sessions))
          setQuickCount({ account, count: value.sessions.length, more: !!value.next_cursor });
      })
      .catch(() => {});
    return () => {
      live = false;
      client.reset();
    };
  }, [account, revision]);
  const receiveQuickCount = (count, more) =>
    setQuickCount((previous) =>
      previous?.account === account && previous.count === count && previous.more === more
        ? previous
        : { account, count, more },
    );
  const load = async (append = false) => {
    const n = ++epoch.current;
    setLoading(true);
    setError('');
    try {
      const data = await ctx.controller.list(kind, append ? next : 0);
      if (n !== epoch.current) return;
      setRows((old) =>
        append
          ? [...old, ...data.items.filter((x) => !old.some((y) => y.trash_id === x.trash_id))]
          : data.items,
      );
      setNext(data.next_offset);
      if (!append) setSelected([]);
    } catch {
      if (n === epoch.current) setError(t('trash.failed'));
    } finally {
      if (n === epoch.current) setLoading(false);
    }
  };
  const loadEvent = useEffectEvent((...args) => load(...args));
  useEffect(() => {
    setRows([]);
    setSelected([]);
    if (ctx?.account && kind !== 'conversation') loadEvent();
    const epochRef = epoch;
    return () => {
      epochRef.current++;
    };
  }, [ctx?.account, ctx?.revision, kind]);
  const inGroup = (row, value = group) =>
    value === 'all' ||
    (value === 'story' && ['project', 'chapter', 'scene', 'shot', 'artifact'].includes(row.kind)) ||
    (value === 'lib' &&
      [
        'character',
        'location',
        'prop',
        'look',
        'location_version',
        'image',
        'video',
        'audio',
        'asset',
      ].includes(row.kind)) ||
    (value === 'session' && row.kind === 'take');
  const items = rows.filter((row) => inGroup(row)),
    targets = items.filter((row) => selected.includes(row.trash_id)).map((row) => row.target),
    locked = loading || ctx?.busy || !!ctx?.pending;
  const changeGroup = (value) => {
    const nextKind = value === 'session' ? 'conversation' : '';
    setGroup(value);
    setKind(nextKind);
    setSelected([]);
    const url = new URL(window.location.href);
    if (nextKind) url.searchParams.set('kind', nextKind);
    else url.searchParams.delete('kind');
    navigateApp(url.pathname + url.search, { replace: true, restore: false, scroll: false });
  };
  const empty = () =>
    ctx
      .run(async () => {
        const all = [...rows];
        let offset = next;
        const seen = new Set();
        while (offset !== null) {
          if (seen.has(offset)) throw Error('trash.failed');
          seen.add(offset);
          const page = await ctx.controller.list(kind, offset);
          all.push(...page.items);
          offset = page.next_offset;
        }
        return all.filter((r) => r.state === 'trashed' && inGroup(r)).map((r) => r.target);
      })
      .then((value) => {
        if (value?.length) ctx.request('purge', value);
      });
  return (
    <div className="home ops-adapter">
      <StudioHeader account={ctx?.account} active="trash" />
      <main className="trash-page">
        <div className="trash-head">
          <h1 className="page-title">
            <Trash2 size={20} />
            {t('trash.title')}
            <small>
              {ctx?.account && kind !== 'conversation'
                ? rows.length +
                  (group === 'all' && quickCount?.account === account ? quickCount.count : 0) +
                  (next !== null ? '+' : '')
                : ''}
            </small>
          </h1>
          <div className="spacer" />
          {ctx?.account && kind !== 'conversation' && (
            <Button
              variant="ghost"
              size="sm"
              className="danger-text"
              disabled={locked || !rows.length}
              onClick={empty}
            >
              {t(kind || group !== 'all' ? 'trash.emptyFiltered' : 'trash.empty')}
            </Button>
          )}
        </div>
        <p className="muted">{t('trash.retention')}</p>
        {!ctx?.account && !sessionReady ? (
          <i className="ui-wait ui-wait-block" />
        ) : !ctx?.account ? (
          <div className="ui-empty">
            <span>
              <Trash2 size={22} />
            </span>
            <b>{t('trash.signIn')}</b>
            <a href="/quick-chat">{t('trash.signIn')}</a>
          </div>
        ) : (
          <>
            <Segmented
              className="trash-filter"
              options={[
                ['all', t('trash.all')],
                ['story', locale === 'zh' ? '故事和镜头' : 'Stories and shots'],
                ['lib', t('library.title')],
                ['session', t('nav.quickCreate')],
              ].map(([id, label]) => ({
                value: id,
                label: (
                  <>
                    {label}
                    <span className="trash-tab-count">
                      {id === 'session'
                        ? quickCount?.account === account
                          ? quickCount.count + (quickCount.more ? '+' : '')
                          : '—'
                        : rows.filter((row) => inGroup(row, id)).length +
                          (id === 'all' && quickCount?.account === account ? quickCount.count : 0) +
                          (next !== null || (id === 'all' && quickCount?.more) ? '+' : '')}
                    </span>
                  </>
                ),
              }))}
              value={group}
              onChange={changeGroup}
            />
            {kind === 'conversation' ? (
              <div className="ops-conversation-trash">
                <ConversationTrash
                  key={ctx.account}
                  account={ctx.account}
                  busy={ctx.busy || !!ctx.pending}
                  onCount={receiveQuickCount}
                />
              </div>
            ) : (
              <>
                <Disclosure
                  className="ops-tools"
                  summary={
                    <>
                      <SlidersHorizontal size={14} />
                      {t('trash.filters')}
                    </>
                  }
                >
                  <div className="row tight">
                    <select
                      aria-label={t('trash.all')}
                      value={kind}
                      onChange={(e) => {
                        setKind(e.target.value);
                        setGroup('all');
                      }}
                    >
                      {[
                        '',
                        'project',
                        'chapter',
                        'scene',
                        'shot',
                        'character',
                        'location',
                        'prop',
                        'look',
                        'location_version',
                        'image',
                        'video',
                        'audio',
                        'asset',
                        'artifact',
                        'take',
                      ].map((k) => (
                        <option key={k} value={k}>
                          {t(k ? 'trash.kind.' + k : 'trash.all')}
                        </option>
                      ))}
                    </select>
                    <Button size="sm" disabled={locked} onClick={() => load()}>
                      {t('trash.refresh')}
                    </Button>
                    <Button
                      size="sm"
                      disabled={locked}
                      onClick={() =>
                        setSelected(
                          items.filter((r) => r.state === 'trashed').map((r) => r.trash_id),
                        )
                      }
                    >
                      {t('trash.selectAll')}
                    </Button>
                  </div>
                </Disclosure>
                {error && <p role="alert">{error}</p>}
                {items.length ? (
                  <div className="trash-list" aria-busy={loading}>
                    {targets.length > 0 && (
                      <div className="trash-bar" role="status">
                        {locale === 'zh'
                          ? `已选 ${targets.length} 项`
                          : `${targets.length} selected`}
                        <div className="spacer" />
                        <Button
                          size="sm"
                          disabled={locked}
                          onClick={() => ctx.request('restore', targets)}
                        >
                          <RotateCcw size={13} />
                          {t('trash.restore')}
                        </Button>
                        <Button
                          variant="danger"
                          size="sm"
                          disabled={locked}
                          onClick={() => ctx.request('purge', targets)}
                        >
                          {t('trash.purge')}
                        </Button>
                      </div>
                    )}
                    {items.map((row) => {
                      const Icon = trashIcon(row.kind);
                      return (
                        <div className="trash-row" key={row.trash_id}>
                          <input
                            type="checkbox"
                            disabled={locked || row.state !== 'trashed'}
                            checked={selected.includes(row.trash_id)}
                            aria-label={row.title || t('trash.untitled')}
                            onChange={(e) =>
                              setSelected((old) =>
                                e.target.checked
                                  ? [...old, row.trash_id]
                                  : old.filter((id) => id !== row.trash_id),
                              )
                            }
                          />
                          <span className="thumb" aria-hidden="true">
                            <Icon size={18} />
                          </span>
                          <div className="name">
                            <b className="clip" title={row.title}>
                              {row.title || t('trash.untitled')}
                            </b>
                            <small>
                              {t('trash.kind.' + row.kind)} ·{' '}
                              {row.state === 'purging' ? t('trash.pending') : t('trash.moved')}
                            </small>
                          </div>
                          <span className="left" title={t('trash.moved')}>
                            {row.trashed_at
                              ? new Date(row.trashed_at * 1000).toLocaleDateString(
                                  locale === 'zh' ? 'zh-CN' : 'en',
                                )
                              : '—'}
                          </span>
                          <div className="acts">
                            <Button
                              size="sm"
                              disabled={locked || row.state !== 'trashed'}
                              onClick={() => ctx.request('restore', [row.target])}
                            >
                              <RotateCcw size={13} />
                              {t('trash.restore')}
                            </Button>
                            <Button
                              variant="ghost-danger"
                              size="sm"
                              title={t('trash.purge')}
                              aria-label={t('trash.purge')}
                              disabled={locked || row.state !== 'trashed'}
                              onClick={() => ctx.request('purge', [row.target])}
                            >
                              <Trash2 size={13} />
                            </Button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  !error && (
                    <div className="ui-empty" role={loading ? 'status' : undefined}>
                      <span>
                        <Trash2 size={22} />
                      </span>
                      <b>
                        {loading
                          ? t('settings.loading')
                          : group === 'all' &&
                              quickCount?.account === account &&
                              quickCount.count > 0
                            ? t('trash.noneOutsideQuick')
                            : t('trash.none')}
                      </b>
                      {!loading &&
                        group === 'all' &&
                        quickCount?.account === account &&
                        quickCount.count > 0 && (
                          <Button size="sm" onClick={() => changeGroup('session')}>
                            {t('trash.quickElsewhere', { count: quickCount.count })}
                          </Button>
                        )}
                    </div>
                  )
                )}
                {next !== null && (
                  <Button size="sm" disabled={locked} onClick={() => load(true)}>
                    {t('trash.more')}
                  </Button>
                )}
              </>
            )}
          </>
        )}
      </main>
    </div>
  );
}

export function StoryItemManager({ state, onClose, onEdit }) {
  const { t } = useI18n(),
    [filter, setFilter] = useState('');
  const p = state.project;
  return (
    <Dialog className="ops-dialog" title={t('trash.manage')} onClose={onClose}>
      <input
        type="search"
        aria-label={t('trash.search')}
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      {p.entities
        .filter((e) => (e.title || '').toLocaleLowerCase().includes(filter.toLocaleLowerCase()))
        .map((e) => (
          <div className="ops-item-row" key={e.id}>
            <div>
              <b>{e.title}</b>
              <small> · {t('trash.kind.' + e.type)}</small>
            </div>
            <TrashMenu
              target={{ kind: 'entity', project_id: p.id, id: e.id }}
              onTrashSettled={onClose}
              onEdit={() => onEdit(e.id)}
              onRename={() => onEdit(e.id)}
            />
          </div>
        ))}
    </Dialog>
  );
}

function ConversationTrash({ account, busy, onCount }) {
  const controller = useMemo(() => createQuickChatController(), []),
    state = useSyncExternalStore(controller.subscribe, controller.getState, controller.getState);
  useEffect(() => {
    if (state.account === account && state.historyReceipt?.state === 'completed')
      window.dispatchEvent(
        new CustomEvent('inkseq-quick-history-changed', { detail: { account } }),
      );
  }, [account, state.account, state.historyReceipt?.action_id, state.historyReceipt?.state]);
  useEffect(() => {
    let active = true;
    controller
      .setAccount(account)
      .then(() => {
        if (active && controller.getState().account === account)
          return controller.historyView('trash');
      })
      .catch(() => {});
    return () => {
      active = false;
      controller.destroy();
    };
  }, [account, controller]);
  const onCountEvent = useEffectEvent((...args) => onCount(...args));
  useEffect(() => {
    if (state.account === account && state.historyView === 'trash' && state.trashAsOf !== null)
      onCountEvent?.(state.trashSessions.length, !!state.trashCursor);
  }, [
    state.account,
    state.historyView,
    state.trashAsOf,
    state.trashSessions,
    state.trashCursor,
    account,
  ]);
  return (
    <CreationHistory
      state={state}
      controller={controller}
      busy={busy || state.busy}
      embeddedTrash
      onSelect={() => {}}
    />
  );
}

export function RenameDialog({ title, onSave, onClose }) {
  const { t } = useI18n(),
    [value, setValue] = useState(title || ''),
    [saving, setSaving] = useState(false),
    [error, setError] = useState(''),
    alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );
  return (
    <Dialog
      className="ops-dialog"
      title={t('trash.rename')}
      onClose={() => {
        if (!saving) onClose();
      }}
    >
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          if (saving) return;
          setSaving(true);
          setError('');
          try {
            await onSave(value);
            if (alive.current) onClose();
          } catch {
            if (alive.current) setError(t('trash.failed'));
          } finally {
            if (alive.current) setSaving(false);
          }
        }}
      >
        <Field label={t('trash.name')}>
          <input value={value} onChange={(e) => setValue(e.target.value)} disabled={saving} />
        </Field>
        {error && <p role="alert">{error}</p>}
        <DialogActions>
          <Button disabled={saving} onClick={onClose}>
            {t('trash.cancel')}
          </Button>
          <Button type="submit" variant="primary" disabled={saving || !value.trim()}>
            {t('trash.save')}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
export { TrashProvider, useTrash };
