import React, { useEffect, useRef, useState } from 'react';
import { Star, RefreshCw, ArrowUpRight } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { usePageActive } from './AppRouteCache.jsx';
import { ChatDialog } from './QuickChatControls.jsx';
import { LibraryMedia } from './Library.jsx';
import { readSessionTakes, sessionTakeRows, takeStatusGroup } from './session-takes.js';
import './v2/session-takes.css';

export default function QuickChatSessionTakes({ state, controller, onClose, onJump }) {
  const { t } = useI18n(),
    active = usePageActive(),
    generation = useRef(0);
  const [takes, setTakes] = useState([]),
    [total, setTotal] = useState(0),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(''),
    [refresh, setRefresh] = useState(0),
    [status, setStatus] = useState('all'),
    [starred, setStarred] = useState(false),
    [shown, setShown] = useState(80),
    [jumping, setJumping] = useState(false);
  const account = state.account,
    sessionId = state.session?.id;
  useEffect(() => {
    const epoch = ++generation.current,
      abort = new AbortController();
    setTakes([]);
    setTotal(0);
    setError('');
    setLoading(false);
    if (!active || !account || !sessionId) return () => abort.abort();
    const current = () => {
      const live = controller.getState();
      return (
        !abort.signal.aborted &&
        epoch === generation.current &&
        live.account === account &&
        live.session?.id === sessionId
      );
    };
    setLoading(true);
    void (async () => {
      try {
        await readSessionTakes(controller.client, sessionId, {
          signal: abort.signal,
          isCurrent: current,
          onPage: (page) => {
            setTotal(page.total_count);
            setTakes((old) => [
              ...new Map([...old, ...page.takes].map((row) => [row.id, row])).values(),
            ]);
          },
        });
      } catch {
        if (current()) setError('sessionTakes.failed');
      } finally {
        if (current()) setLoading(false);
      }
    })();
    return () => {
      abort.abort();
      generation.current++;
    };
  }, [active, account, sessionId, controller, refresh]);
  useEffect(() => {
    setShown(80);
  }, [status, starred, account, sessionId]);
  const rows = sessionTakeRows(takes, { status, starred });
  async function jump(row) {
    const epoch = generation.current,
      live = controller.getState();
    if (jumping || live.account !== account || live.session?.id !== sessionId) return;
    setJumping(true);
    setError('');
    try {
      await onJump(row);
      if (epoch === generation.current) onClose();
    } catch {
      if (epoch === generation.current) setError('sessionTakes.jumpFailed');
    } finally {
      if (epoch === generation.current) setJumping(false);
    }
  }
  return (
    <ChatDialog title={t('sessionTakes.title')} onClose={onClose} wide className="qc-session-takes">
      <div className="session-takes-tools">
        <label>
          <span>{t('sessionTakes.status')}</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            {[
              'all',
              'succeeded',
              'running',
              'queued',
              'failed',
              'draft',
              'cancelled',
              'unknown',
            ].map((value) => (
              <option key={value} value={value}>
                {t('sessionTakes.status.' + value)}
              </option>
            ))}
          </select>
        </label>
        <label className="session-takes-star">
          <input
            type="checkbox"
            checked={starred}
            onChange={(event) => setStarred(event.target.checked)}
          />
          <Star size={14} />
          {t('sessionTakes.starred')}
        </label>
        <button
          type="button"
          disabled={loading || jumping}
          onClick={() => setRefresh((value) => value + 1)}
        >
          <RefreshCw size={14} />
          {t('takes.refresh')}
        </button>
      </div>
      <p role="status" className="session-takes-progress">
        {loading
          ? t('sessionTakes.loading', { loaded: takes.length, total })
          : t(rows.length === 1 ? 'sessionTakes.countOne' : 'sessionTakes.count', {
              count: rows.length,
            })}
      </p>
      {error && (
        <p role="alert">
          {t(error)}{' '}
          <button type="button" disabled={loading} onClick={() => setRefresh((value) => value + 1)}>
            {t('workspace.copy60')}
          </button>
        </p>
      )}
      <div className="session-takes-grid">
        {rows.slice(0, shown).map((row) => {
          const media = row.artifacts.find(
              (item) => item.kind === 'video' || item.kind === 'image',
            ),
            label = row.title || t('takes.source', { number: row.group_number });
          return (
            <button
              type="button"
              key={row.id}
              className="session-take-tile"
              data-ui-surface="card"
              disabled={jumping}
              onClick={() => jump(row)}
              title={t('sessionTakes.openCard')}
            >
              <span className="session-take-thumb">
                {media ? (
                  <LibraryMedia
                    row={{ ...media, artifact_id: media.id, type: media.kind, title: label }}
                    api={controller.client}
                  />
                ) : (
                  <span>{t('sessionTakes.status.' + takeStatusGroup(row.status))}</span>
                )}
                {row.annotation.starred && (
                  <Star
                    size={15}
                    className="session-take-star"
                    aria-label={t('sessionTakes.starredTake')}
                  />
                )}
              </span>
              <strong>{label}</strong>
              <span className="session-take-caption">
                <span>{t('sessionTakes.status.' + takeStatusGroup(row.status))}</span>
                <ArrowUpRight size={14} />
              </span>
            </button>
          );
        })}
      </div>
      {!loading && !error && !rows.length && <p>{t('sessionTakes.empty')}</p>}
      {shown < rows.length && (
        <button
          type="button"
          className="session-takes-more"
          onClick={() => setShown((value) => value + 80)}
        >
          {t('sessionTakes.more', { shown: Math.min(shown, rows.length), total: rows.length })}
        </button>
      )}
    </ChatDialog>
  );
}
