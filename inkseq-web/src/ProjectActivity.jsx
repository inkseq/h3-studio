import { useI18n } from './i18n/I18nProvider.jsx';
import { Button } from './ui/Button.jsx';
import { FloatingMessage } from './floating-notice.js';
import React, { useEffect, useRef, useState, useEffectEvent } from 'react';
import { Activity, RefreshCw, ChevronRight } from 'lucide-react';
import { cloudController as cloud } from './cloud-controller.js';
import { JobList, useCloud } from './CloudStudio.jsx';
import { emptyActivityWindow, updateActivityWindow } from './activity-window.js';
import './project-activity.css';

export default function ProjectActivity({ state, onLocate, onReview, onOpenCloud, onConnect }) {
  const { t: at } = useI18n();
  const c = useCloud(),
    [history, setHistory] = useState(emptyActivityWindow),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  const alive = useRef(false),
    pending = useRef(false),
    historyRef = useRef(history),
    p = state.project,
    quick = p.journey?.workspace === 'freestyle';
  const { events, cursor, loaded, windowReset } = history;
  const connected =
    state.workspace.mode === 'cloud' && c.account && state.workspace.account === c.account;
  async function read(older = false) {
    if (pending.current || !connected) return;
    pending.current = true;
    setBusy(true);
    try {
      const result = await cloud.readProjectActivity(
        older ? { beforeVersion: historyRef.current.cursor } : {},
      );
      if (!alive.current) return;
      const next = updateActivityWindow(historyRef.current, result, { older });
      historyRef.current = next;
      setHistory(next);
      setError('');
    } catch {
      if (alive.current) setError(at('adjunct.projectActivity.copy0'));
    } finally {
      pending.current = false;
      if (alive.current) setBusy(false);
    }
  }
  const readEvent = useEffectEvent((...args) => read(...args));
  useEffect(() => {
    alive.current = true;
    readEvent();
    const timer = setInterval(() => {
      if (document.visibilityState !== 'hidden') readEvent();
    }, 15000);
    return () => {
      alive.current = false;
      clearInterval(timer);
    };
  }, [connected]);
  if (!connected)
    return (
      <section className="project-activity">
        <h1>{at('adjunct.projectActivity.copy1')}</h1>
        <p>{at('adjunct.projectActivity.copy2')}</p>
        <Button variant="primary" onClick={onOpenCloud}>
          {at('adjunct.projectActivity.copy3')}
        </Button>
        <p>{at('adjunct.projectActivity.copy4')}</p>
      </section>
    );
  return (
    <section
      className="project-activity"
      aria-label={quick ? at('adjunct.projectActivity.copy5') : at('adjunct.projectActivity.copy6')}
    >
      <header>
        <div>
          <small>ACTIVITY</small>
          <h1>
            {quick ? at('adjunct.projectActivity.copy7') : at('adjunct.projectActivity.copy8')}
          </h1>
          <p>{at('adjunct.projectActivity.copy9')}</p>
        </div>
        <Button
          disabled={busy}
          onClick={() => {
            read();
            cloud.refresh().catch(() => {});
            cloud.checkRemoteVersion();
          }}
        >
          <RefreshCw size={15} />
          {at('adjunct.projectActivity.copy10')}
        </Button>
      </header>
      <div className="activity-intro">
        <Activity size={22} />
        <span>{at('adjunct.projectActivity.copy11')}</span>
        <Button onClick={onConnect}>{at('adjunct.projectActivity.copy12')}</Button>
      </div>
      {state.workspace.dirty && (
        <p className="cloud-warning">{at('adjunct.projectActivity.copy13')}</p>
      )}
      <h2>
        {at('adjunct.projectActivity.copy14')}
        <span>{c.jobs.length}</span>
      </h2>
      <p>{at('adjunct.projectActivity.copy15')}</p>
      {c.jobs.length ? (
        <JobList jobs={c.jobs} project={p} onInspect={onLocate} onReview={onReview} />
      ) : (
        <p className="ui-empty ui-empty-compact">
          {at('adjunct.projectActivity.copy16')}
          {quick ? at('adjunct.projectActivity.copy17') : at('adjunct.projectActivity.copy18')}
        </p>
      )}
      {c.moreJobs && (
        <Button disabled={c.busy} onClick={() => cloud.loadMoreJobs().catch(() => {})}>
          {at('adjunct.projectActivity.copy19')}
        </Button>
      )}
      <FloatingMessage tone="error" text={c.error ? c.error : null} />
      <h2>{at('adjunct.projectActivity.copy20')}</h2>
      <FloatingMessage tone="error" text={error ? error : null} />
      {!loaded && !error && <p role="status">{at('adjunct.projectActivity.copy21')}</p>}
      {windowReset && (
        <p role="status" className="cloud-warning">
          {at('adjunct.projectActivity.copy22')}
        </p>
      )}
      {loaded && !events.length && (
        <p className="ui-empty ui-empty-compact">{at('adjunct.projectActivity.copy23')}</p>
      )}
      <ol className="activity-events">
        {events.map((event) => (
          <li key={event.id}>
            <article>
              <div className="activity-event-head">
                <b>{event.summary}</b>
                <span>
                  {event.actor_kind === 'api_key'
                    ? 'API Key'
                    : at('adjunct.projectActivity.copy24')}{' '}
                  · {event.actor_label}
                </span>
              </div>
              <small>
                {at('adjunct.projectActivity.copy25')}
                {event.project_version} ·{' '}
                {new Date(
                  typeof event.occurred_at === 'number'
                    ? event.occurred_at * 1000
                    : event.occurred_at,
                ).toLocaleString()}
              </small>
              <div className="activity-targets">
                {(event.target_entity_ids || []).slice(0, 12).map((id) => {
                  const e = p.entities.find((item) => item.id === id);
                  return e ? (
                    <Button key={id} onClick={() => onLocate(id)}>
                      {e.title || at('adjunct.projectActivity.copy26')}
                      <ChevronRight size={13} />
                    </Button>
                  ) : (
                    <span key={id}>{at('adjunct.projectActivity.copy27')}</span>
                  );
                })}
                {(event.target_count || 0) > 12 && (
                  <span>
                    {at('adjunct.projectActivity.copy28')}
                    {event.target_count}
                    {at('adjunct.projectActivity.copy29')}
                  </span>
                )}
              </div>
              {event.project_version > state.workspace.serverVersion && (
                <p className="muted">{at('adjunct.projectActivity.copy30')}</p>
              )}
            </article>
          </li>
        ))}
      </ol>
      {cursor !== null && (
        <Button disabled={busy} onClick={() => read(true)}>
          {at('adjunct.projectActivity.copy31')}
        </Button>
      )}
    </section>
  );
}
