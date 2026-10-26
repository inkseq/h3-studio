import { useI18n } from './i18n/I18nProvider.jsx';
import { Button } from './ui/Button.jsx';
import { Dialog } from './ui/Dialog.jsx';
import React, { useEffect, useState } from 'react';
import { RotateCcw } from 'lucide-react';
import {
  historySelectable,
  historyReason,
  purgeCountdown,
  historyBytes,
} from './creation-history-model.js';
const safely = (promise) => promise.catch(() => {});
const date = (value) => (Number.isFinite(value) ? new Date(value * 1000).toLocaleString() : '');
function Totals({ title, value, content = false }) {
  const { t, locale } = useI18n();
  const HISTORY_ACTIONS = {
    trash: t('history.copy55'),
    restore: t('history.copy56'),
    purge: t('history.copy57'),
    empty_trash: t('history.copy58'),
  };
  return (
    <section className="qc-history-totals">
      <h3>{title}</h3>
      <p>
        {content &&
          t('history.copy59', { sessions: value.sessions, turns: value.turns, cards: value.cards })}
        {t('history.copy61', { assets: value.assets, artifacts: value.artifacts })}
      </p>
      <small>
        {t('history.copy62', { count: value.objects })} · {historyBytes(value.bytes, locale)}
      </small>
    </section>
  );
}
function Preview({ value, state, controller }) {
  const { t, locale } = useI18n();
  const HISTORY_ACTIONS = {
    trash: t('history.copy55'),
    restore: t('history.copy56'),
    purge: t('history.copy57'),
    empty_trash: t('history.copy58'),
  };
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [value.preview_id]);
  const expired = now >= value.expires_at,
    blocked = value.items.filter((item) => !item.eligible),
    permanent = ['purge', 'empty_trash'].includes(value.action);
  return (
    <div className="qc-history-preview">
      <p>
        {permanent
          ? t('history.copy1')
          : value.action === 'trash'
            ? t('history.copy2')
            : t('history.copy3')}
      </p>
      {permanent ? (
        <>
          <Totals title={t('history.copy4')} value={value.removed} content />
          <Totals title={t('history.copy5')} value={value.kept} />
        </>
      ) : (
        <p className="qc-history-totals">
          {t('history.copy65', {
            count: value.items.length,
            action: HISTORY_ACTIONS[value.action],
          })}
        </p>
      )}
      <p className="qc-muted">{t('history.copy8')}</p>
      {value.review_hold_release_required && <p className="qc-warning">{t('history.copy9')}</p>}
      {!!value.warnings.length && (
        <section className="qc-history-warnings">
          <h3>{t('history.copy10')}</h3>
          <ul>
            {value.warnings.map((warning, index) => (
              <li key={index}>
                {warning.label || t('history.copy11')}：
                {warning.project_id || warning.shot_id ? t('history.copy12') : t('history.copy13')}
              </li>
            ))}
          </ul>
        </section>
      )}
      {!!blocked.length && (
        <p className="qc-error" role="alert">
          {t('history.copy70', {
            count: blocked.length,
            reasons: [
              ...new Set(blocked.map((item) => historyReason(item.blocked_reason, locale))),
            ].join('; '),
          })}
        </p>
      )}
      {expired && <p className="qc-warning">{t('history.copy14')}</p>}
      <div className="qc-dialog-footer">
        <Button disabled={state.busy} onClick={() => controller.dismissHistoryPreview()}>
          {t('history.copy15')}
        </Button>
        <Button
          className={permanent ? 'qc-history-danger' : 'qc-primary'}
          disabled={
            state.busy || !!state.pending || expired || !!blocked.length || !value.items.length
          }
          onClick={() => safely(controller.confirmHistory())}
        >
          {HISTORY_ACTIONS[value.action]}
          {t('history.copy67', { count: value.items.length })}
        </Button>
      </div>
    </div>
  );
}
function Receipt({ state, controller, returnToActive = false, showPending = true }) {
  const { t } = useI18n();
  const HISTORY_ACTIONS = {
    trash: t('history.copy55'),
    restore: t('history.copy56'),
    purge: t('history.copy57'),
    empty_trash: t('history.copy58'),
  };
  const receipt = state.historyReceipt,
    pending = state.pending?.method === 'actionHistory',
    blocked = receipt?.items.filter((item) => item.state === 'blocked').length || 0;
  const receiptKey = receipt?.action_id + ':' + receipt?.state,
    [dismissed, setDismissed] = useState(null);
  const receiptCompleted = receipt?.state === 'completed';
  useEffect(() => {
    if (!returnToActive || !receiptCompleted || blocked) return;
    const timer = setTimeout(() => setDismissed(receiptKey), 12000);
    return () => clearTimeout(timer);
  }, [returnToActive, receiptCompleted, receiptKey, blocked]);
  async function undo() {
    const owner = state.account,
      ids = receipt.items
        .filter((item) => item.state === 'completed' && item.trash_state === 'trashed')
        .map((item) => item.id);
    let keepPreview = false;
    try {
      await controller.historyView('trash');
      const seen = new Set();
      let live = controller.getState();
      while (
        ids.some((id) => !live.trashSessions.some((row) => row.id === id)) &&
        live.trashCursor &&
        !seen.has(live.trashCursor)
      ) {
        if (live.account !== owner) return;
        seen.add(live.trashCursor);
        await controller.list({ view: 'trash', more: true });
        live = controller.getState();
      }
      if (
        live.account !== owner ||
        ids.some((id) => !live.trashSessions.some((row) => row.id === id))
      )
        return;
      controller.selectHistory(ids);
      const value = await controller.previewHistory('restore');
      if (value.items.length && value.items.every((item) => item.eligible))
        await controller.confirmHistory();
      else keepPreview = true;
    } finally {
      const live = controller.getState();
      if (returnToActive && !keepPreview && live.account === owner && !live.busy)
        await controller.historyView('active');
    }
  }

  return (
    <>
      {receipt && (!returnToActive || dismissed !== receiptKey) && (
        <p
          className={receipt.state === 'pending' || blocked ? 'qc-warning' : 'qc-history-receipt'}
          role="status"
        >
          {HISTORY_ACTIONS[receipt.action]}：
          {receipt.state === 'pending'
            ? t('history.copy16')
            : blocked
              ? t('history.copy60', { count: blocked })
              : t('history.copy17')}
          {t('history.copy66')}
          {receipt.action === 'trash' && receipt.state === 'completed' && !blocked && (
            <Button disabled={state.busy || !!state.pending} onClick={() => safely(undo())}>
              {t('trash.undo')}
            </Button>
          )}
        </p>
      )}
      {showPending && pending && (
        <div className="qc-warning">
          <p>{state.pending.actionId ? t('history.copy18') : t('history.copy19')}</p>
          <Button disabled={state.busy} onClick={() => safely(controller.recover())}>
            {state.pending.actionId ? t('history.copy20') : t('history.copy21')}
          </Button>
        </div>
      )}
    </>
  );
}
const InlineHistory = ({ title, children }) => (
  <section aria-label={title}>
    <h2>{title}</h2>
    {children}
  </section>
);
export function HistoryFeedback({ state, controller, busy }) {
  const { t } = useI18n(),
    preview = state.historyPreview;
  const close = () => {
    if (busy) return;
    controller.dismissHistoryPreview();
    if (state.historyView !== 'active') safely(controller.historyView('active'));
  };
  return (
    <>
      <Receipt state={state} controller={controller} returnToActive showPending={false} />
      {preview && (
        <Dialog
          title={
            t(
              'history.' +
                { trash: 'copy55', restore: 'copy56', purge: 'copy57', empty_trash: 'copy58' }[
                  preview.action
                ],
            ) + t('history.copy42')
          }
          onClose={close}
        >
          <Preview key={preview.preview_id} value={preview} state={state} controller={controller} />
          {state.error && (
            <p className="qc-error" role="alert">
              {state.error}
            </p>
          )}
        </Dialog>
      )}
    </>
  );
}
export default function CreationHistory({ state, controller, busy, embeddedTrash = false }) {
  const { t, locale } = useI18n();
  const preview = state.historyPreview,
    selected = state.historySelection,
    locked = busy || !!state.pending,
    rows = state.trashSessions;
  function toggle(id) {
    try {
      controller.selectHistory(
        selected.includes(id) ? selected.filter((value) => value !== id) : [...selected, id],
      );
    } catch {}
  }
  function selectAll() {
    try {
      controller.selectHistory(
        rows
          .filter((row) => historySelectable(row, 'trash'))
          .slice(0, 100)
          .map((row) => row.id),
      );
    } catch {}
  }
  async function softAction(action) {
    const value = await controller.previewHistory(action);
    if (value.items.length && value.items.every((item) => item.eligible))
      await controller.confirmHistory();
  }
  function leave() {
    if (!busy) safely(controller.historyView('active'));
  }
  const error = state.error && (
    <p className="qc-error" role="alert">
      {state.error}
    </p>
  );
  const controls = (
    <div className="qc-history-selection">
      <Button disabled={locked} onClick={selectAll}>
        {t('history.copy23')}
      </Button>
      <Button disabled={locked || !selected.length} onClick={() => controller.selectHistory([])}>
        {t('history.copy24')}
      </Button>
      <span>{t('history.copy63', { count: selected.length })}</span>
    </div>
  );
  if (!embeddedTrash) return <HistoryFeedback state={state} controller={controller} busy={busy} />;
  const HistoryContainer = InlineHistory;
  return (
    <>
      <HistoryContainer
        title={t('history.copy47')}
        wide
        onClose={leave}
        className="qc-history-dialog"
      >
        {preview ? (
          <Preview key={preview.preview_id} value={preview} state={state} controller={controller} />
        ) : (
          <>
            <p className="qc-muted">
              {t('history.copy68')}
              {state.trashAsOf && (
                <small>{t('history.copy69', { time: date(state.trashAsOf) })}</small>
              )}
            </p>
            <Receipt state={state} controller={controller} />
            {controls}
            <div className="qc-history-trash-list">
              {state.trashSessions.map((row) => (
                <label className="qc-history-trash-row" key={row.id}>
                  <input
                    type="checkbox"
                    aria-label={t('history.copy48') + (row.title || t('history.copy35'))}
                    checked={selected.includes(row.id)}
                    disabled={locked || !historySelectable(row, 'trash')}
                    onChange={() => toggle(row.id)}
                  />
                  <span>
                    <b>{row.title || t('history.copy35')}</b>
                    <small>
                      {purgeCountdown(row, state.trashAsOf, locale)}
                      {row.is_test ? t('history.copy49') : ''}
                    </small>
                    {row.blocked_reason && (
                      <small>{historyReason(row.blocked_reason, locale)}</small>
                    )}
                  </span>
                  <small>{date(row.trashed_at)}</small>
                </label>
              ))}
              {!state.trashSessions.length && (
                <p className="ui-empty ui-empty-compact">{t('history.copy50')}</p>
              )}
            </div>
            {state.trashCursor && (
              <Button
                disabled={busy}
                onClick={() => safely(controller.list({ view: 'trash', more: true }))}
              >
                {t('history.copy51')}
              </Button>
            )}
            <div className="qc-history-actions">
              <Button
                disabled={locked || !selected.length}
                onClick={() => safely(softAction('restore'))}
              >
                <RotateCcw size={14} />
                {t('history.copy52')}
              </Button>
              <Button
                disabled={locked || !selected.length}
                onClick={() => safely(controller.previewHistory('purge'))}
              >
                {t('history.copy53')}
              </Button>
              <Button
                className="qc-history-danger"
                disabled={locked || !state.trashSessions.length}
                onClick={() => safely(controller.previewHistory('empty_trash'))}
              >
                {t('history.copy54')}
              </Button>
            </div>
          </>
        )}
        {error}
      </HistoryContainer>
    </>
  );
}
