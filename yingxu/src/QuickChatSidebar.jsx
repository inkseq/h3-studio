import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useAccess } from './StudioHeader.jsx';
import { createPortal } from 'react-dom';
import { Archive, CheckSquare, MoreHorizontal, Pencil, Tag, Trash2 } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { AssistantBadge } from './AssistantConnections.jsx';
import { ChatDialog } from './QuickChatControls.jsx';
import { historyReason, historySelectable } from './creation-history-model.js';
import {
  conversationRowContext,
  currentConversationRow,
  trashConversationRows,
} from './quick-chat-sidebar.js';

function RowPopover({ anchor, onClose, children }) {
  const box = useRef(null),
    [position, setPosition] = useState(null);
  useLayoutEffect(() => {
    const row = anchor?.getBoundingClientRect(),
      menu = box.current;
    if (!row || !menu) {
      onClose();
      return;
    }
    const width = Math.min(230, window.innerWidth - 24),
      height = menu.offsetHeight;
    setPosition({
      left: Math.max(12, Math.min(row.right - width, window.innerWidth - width - 12)),
      top:
        row.bottom + height + 8 <= window.innerHeight
          ? row.bottom + 4
          : Math.max(12, row.top - height - 4),
      width,
    });
    menu.querySelector('button:not(:disabled)')?.focus({ preventScroll: true });
  }, [anchor]);
  useEffect(() => {
    const away = (event) => {
        if (!box.current?.contains(event.target) && !anchor?.contains(event.target)) onClose();
      },
      key = (event) => {
        if (event.key === 'Escape') {
          event.preventDefault();
          event.stopImmediatePropagation();
          onClose();
          anchor?.focus({ preventScroll: true });
        }
      },
      scroll = (event) => {
        if (!box.current?.contains(event.target)) onClose();
      };
    window.addEventListener('pointerdown', away, true);
    window.addEventListener('keydown', key, true);
    window.addEventListener('scroll', scroll, true);
    window.addEventListener('resize', onClose);
    return () => {
      window.removeEventListener('pointerdown', away, true);
      window.removeEventListener('keydown', key, true);
      window.removeEventListener('scroll', scroll, true);
      window.removeEventListener('resize', onClose);
    };
  }, [anchor, onClose]);
  return createPortal(
    <div
      ref={box}
      className="more-menu row-menu quick-row-menu"
      role="menu"
      style={{
        position: 'fixed',
        right: 'auto',
        bottom: 'auto',
        visibility: position ? 'visible' : 'hidden',
        ...position,
      }}
    >
      {children}
    </div>,
    document.body,
  );
}
export function QuickChatRowActions({
  row,
  current,
  blocked,
  disabled,
  onRename,
  onTest,
  onArchive,
  onTrash,
}) {
  const { t } = useI18n();
  return (
    <>
      <button type="button" role="menuitem" disabled={disabled} onClick={onRename}>
        <Pencil size={13} />
        {t('trash.rename')}
      </button>
      <button
        type="button"
        role="menuitem"
        disabled={disabled || row.archive_pending === true}
        onClick={onTest}
      >
        <Tag size={13} />
        {t(row.is_test ? 'quickSidebar.unmarkTest' : 'quickSidebar.markTest')}
      </button>
      {current && row.is_test && (
        <button
          type="button"
          role="menuitem"
          disabled={disabled || row.archive_pending === true}
          onClick={onArchive}
        >
          <Archive size={13} />
          {t(row.archive_pending ? 'history.copy32' : 'history.copy33')}
        </button>
      )}
      <hr />
      <button
        type="button"
        role="menuitem"
        className="danger-item"
        disabled={disabled || !!blocked}
        title={blocked || t('quickSidebar.restorable')}
        onClick={onTrash}
      >
        <Trash2 size={13} />
        {t('trash.move')}
      </button>
      <small className="quick-row-hint">{blocked || t('quickSidebar.restorable')}</small>
    </>
  );
}
export default function QuickChatSidebar({ state, controller, busy, onSelect, active = true }) {
  // Test-record filtering is an operator tool.
  const access = useAccess(state.account);
  const { t, locale } = useI18n(),
    [menu, setMenu] = useState(null),
    [editing, setEditing] = useState(null),
    [selecting, setSelecting] = useState(false),
    [error, setError] = useState(''),
    [message, setMessage] = useState(''),
    [archive, setArchive] = useState(null),
    identity = useRef(state.account),
    cancelRename = useRef(false),
    saving = useRef(false),
    triggers = useRef(new Map());
  identity.current = state.account;
  const locked = busy || !!state.pending || state.historyView !== 'active',
    selected = state.historySelection || [],
    rows = state.sessions;
  const text = (error) =>
    error?.message?.startsWith('quickSidebar.')
      ? t(error.message)
      : error?.message || t('quickSidebar.failed');
  useEffect(() => {
    setMenu(null);
    setEditing(null);
    setSelecting(false);
    setError('');
    setMessage('');
    setArchive(null);
  }, [state.account]);
  useEffect(() => {
    if (!active) setMenu(null);
  }, [active]);
  useEffect(() => {
    if (menu && !rows.some((row) => row.id === menu.id && row.version === menu.version))
      setMenu(null);
  }, [rows, menu]);
  async function run(fn) {
    const owner = state.account;
    setError('');
    setMessage('');
    try {
      const value = await fn();
      if (identity.current === owner) setMessage(t('quickSidebar.saved'));
      return value;
    } catch (error) {
      if (identity.current === owner) setError(text(error));
      throw error;
    }
  }
  const safely = (fn) => run(fn).catch(() => {});
  function beginRename(target) {
    try {
      const row = currentConversationRow(controller.getState(), target);
      cancelRename.current = false;
      setEditing({ target, value: row.title || '' });
      setMenu(null);
      setError('');
    } catch (error) {
      setError(text(error));
    }
  }
  async function saveRename() {
    if (!editing || cancelRename.current || saving.current) return;
    const edit = editing,
      title = edit.value.trim();
    if (!title) {
      setError(t('quickSidebar.titleRequired'));
      return;
    }
    saving.current = true;
    try {
      const row = currentConversationRow(controller.getState(), edit.target);
      if (title !== row.title) await run(() => controller.patchRow(edit.target, { title }));
      if (identity.current === edit.target.account) setEditing(null);
    } catch (error) {
      if (identity.current === edit.target.account) setError(text(error));
    } finally {
      saving.current = false;
    }
  }
  function toggle(id) {
    try {
      controller.selectHistory(
        selected.includes(id) ? selected.filter((value) => value !== id) : [...selected, id],
      );
    } catch (error) {
      setError(text(error));
    }
  }
  function closeSelection() {
    setSelecting(false);
    try {
      controller.selectHistory([]);
    } catch {}
  }
  async function archiveTest() {
    const target = archive;
    await run(async () => {
      const live = controller.getState();
      currentConversationRow(live, target);
      if (live.session?.id !== target.id || live.session.version !== target.version)
        throw Error('quickSidebar.changed');
      await controller.patch({
        expected_version: target.version,
        is_test: true,
        test_accepted: true,
      });
      if (identity.current === target.account) setArchive(null);
    });
  }
  return (
    <>
      <div className="quick-sidebar-tools">
        <button
          type="button"
          className="ghost small"
          disabled={locked || !state.account}
          aria-pressed={selecting}
          onClick={() => {
            if (selecting) closeSelection();
            else {
              controller.selectHistory([]);
              setSelecting(true);
              setMenu(null);
            }
          }}
        >
          <CheckSquare size={13} />
          {t(selecting ? 'history.copy26' : 'history.copy27')}
        </button>
        {access?.admin && (
          <label>
            <input
              type="checkbox"
              disabled={locked || !state.account}
              checked={state.hideTests}
              onChange={(event) => safely(() => controller.hideTests(event.target.checked))}
            />
            {t('history.copy28')}
          </label>
        )}
      </div>
      {selecting && (
        <div className="quick-sidebar-selection">
          <span>{t('history.copy63', { count: selected.length })}</span>
          <button
            type="button"
            className="ghost small"
            disabled={locked}
            onClick={() =>
              controller.selectHistory(
                rows
                  .filter((row) => historySelectable(row, 'active'))
                  .slice(0, 100)
                  .map((row) => row.id),
              )
            }
          >
            {t('history.copy23')}
          </button>
          <button
            type="button"
            className="ghost small"
            disabled={locked || !selected.length}
            onClick={() => controller.selectHistory([])}
          >
            {t('history.copy24')}
          </button>
          <button
            type="button"
            className="danger-text small"
            disabled={locked || !selected.length}
            onClick={() =>
              safely(() =>
                trashConversationRows(
                  controller,
                  rows
                    .filter((row) => selected.includes(row.id))
                    .map((row) => conversationRowContext(state, row)),
                ),
              )
            }
          >
            <Trash2 size={13} />
            {t('history.copy64', { count: selected.length })}
          </button>
        </div>
      )}
      {error && (
        <p className="quick-sidebar-error" role="alert">
          {error}
        </p>
      )}
      {message && (
        <span className="visually-hidden" role="status">
          {message}
        </span>
      )}
      <div className="qc-list">
        {rows.map((row) => {
          const target = conversationRowContext(state, row),
            renaming = editing?.target.id === row.id,
            open = menu?.id === row.id,
            blocked = historySelectable(row, 'active')
              ? ''
              : historyReason(row.blocked_reason, locale);
          return (
            <div
              key={row.id}
              data-conversation-row={row.id}
              className={
                'qc-row' + (row.id === state.session?.id ? ' on' : '') + (open ? ' menu-open' : '')
              }
            >
              {selecting && (
                <input
                  className="quick-row-select"
                  type="checkbox"
                  aria-label={t('history.copy34') + (row.title || t('history.copy35'))}
                  checked={selected.includes(row.id)}
                  disabled={locked || !historySelectable(row, 'active')}
                  title={blocked || undefined}
                  onChange={() => toggle(row.id)}
                />
              )}
              {renaming ? (
                <form
                  className="quick-row-rename"
                  onSubmit={(event) => {
                    event.preventDefault();
                    saveRename();
                  }}
                >
                  <input
                    autoFocus
                    className="qc-rename"
                    aria-label={t('quickSidebar.renameLabel', {
                      title: row.title || t('history.copy35'),
                    })}
                    value={editing.value}
                    maxLength={160}
                    disabled={locked}
                    onFocus={(event) => event.target.select()}
                    onChange={(event) => setEditing({ ...editing, value: event.target.value })}
                    onBlur={() => saveRename()}
                    onKeyDown={(event) => {
                      if (event.key === 'Escape') {
                        event.preventDefault();
                        cancelRename.current = true;
                        setEditing(null);
                        triggers.current.get(row.id)?.focus({ preventScroll: true });
                      }
                      if (event.key === 'Enter') {
                        event.preventDefault();
                        event.currentTarget.blur();
                      }
                    }}
                  />
                </form>
              ) : (
                <button className="qc-open" disabled={busy} onClick={() => onSelect(row.id)}>
                  <span className="clip" title={row.title || t('history.copy35')}>
                    {row.title || t('history.copy35')}
                  </span>
                  <AssistantBadge createdBy={row.created_by} />
                  <small>
                    {Number.isFinite(row.updated_at)
                      ? new Date(row.updated_at * 1000).toLocaleString(
                          locale === 'zh' ? 'zh-CN' : 'en',
                        )
                      : ''}
                    {row.is_test ? ' · ' + t('history.copy36') : ''}
                  </small>
                </button>
              )}
              {!selecting && (
                <button
                  type="button"
                  ref={(element) => {
                    if (element) triggers.current.set(row.id, element);
                    else triggers.current.delete(row.id);
                  }}
                  className="qc-dots"
                  disabled={locked || renaming}
                  aria-label={t('quickSidebar.menuLabel', {
                    title: row.title || t('history.copy35'),
                  })}
                  aria-expanded={open}
                  aria-haspopup="menu"
                  onClick={() => setMenu(open ? null : target)}
                >
                  <MoreHorizontal size={15} />
                </button>
              )}
              {open && (
                <RowPopover anchor={triggers.current.get(row.id)} onClose={() => setMenu(null)}>
                  <QuickChatRowActions
                    row={row}
                    current={row.id === state.session?.id}
                    blocked={blocked}
                    disabled={locked}
                    onRename={() => beginRename(menu)}
                    onTest={() => {
                      const chosen = menu;
                      setMenu(null);
                      safely(() => controller.patchRow(chosen, { is_test: !row.is_test }));
                    }}
                    onArchive={() => {
                      setArchive(menu);
                      setMenu(null);
                      setError('');
                    }}
                    onTrash={() => {
                      const chosen = menu;
                      setMenu(null);
                      safely(() => trashConversationRows(controller, [chosen]));
                    }}
                  />
                </RowPopover>
              )}
            </div>
          );
        })}
        {!rows.length && (
          <p className="muted qc-none">
            {t(state.hideTests ? 'history.copy39' : 'history.copy40')}
          </p>
        )}
        {state.sessionsCursor && (
          <button
            type="button"
            className="ghost small"
            disabled={busy}
            onClick={() => safely(() => controller.list({ more: true }))}
          >
            {t('history.copy41')}
          </button>
        )}
      </div>
      {archive && (
        <ChatDialog
          title={t('history.copy43')}
          onClose={() => {
            if (!busy) setArchive(null);
          }}
        >
          <p>{t('history.copy44')}</p>
          {error && (
            <p className="qc-error" role="alert">
              {error}
            </p>
          )}
          <div className="qc-dialog-footer">
            <button type="button" disabled={busy} onClick={() => setArchive(null)}>
              {t('history.copy45')}
            </button>
            <button
              type="button"
              className="qc-primary"
              disabled={locked}
              onClick={() => archiveTest().catch(() => {})}
            >
              {t('history.copy46')}
            </button>
          </div>
        </ChatDialog>
      )}
    </>
  );
}
