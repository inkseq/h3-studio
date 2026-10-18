import { useI18n } from './i18n/I18nProvider.jsx';
import React, { useEffect, useRef, useState } from 'react';
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
  useEffect(() => {
    alive.current = true;
    read();
    const timer = setInterval(() => {
      if (document.visibilityState !== 'hidden') read();
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
        <button className="primary" onClick={onOpenCloud}>
          {at('adjunct.projectActivity.copy3')}
        </button>
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
        <button
          disabled={busy}
          onClick={() => {
            read();
            cloud.refresh().catch(() => {});
            cloud.checkRemoteVersion();
          }}
        >
          <RefreshCw size={15} />
          {at('adjunct.projectActivity.copy10')}
        </button>
      </header>
      <div className="activity-intro">
        <Activity size={22} />
        <span>{at('adjunct.projectActivity.copy11')}</span>
        <button onClick={onConnect}>{at('adjunct.projectActivity.copy12')}</button>
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
        <p className="activity-empty">
          {at('adjunct.projectActivity.copy16')}
          {quick ? at('adjunct.projectActivity.copy17') : at('adjunct.projectActivity.copy18')}
        </p>
      )}
      {c.moreJobs && (
        <button disabled={c.busy} onClick={() => cloud.loadMoreJobs().catch(() => {})}>
          {at('adjunct.projectActivity.copy19')}
        </button>
      )}
      {c.error && (
        <p role="alert" className="cloud-error">
          {c.error}
        </p>
      )}
      <h2>{at('adjunct.projectActivity.copy20')}</h2>
      {error && (
        <p role="alert" className="cloud-error">
          {error}
        </p>
      )}
      {!loaded && !error && <p role="status">{at('adjunct.projectActivity.copy21')}</p>}
      {windowReset && (
        <p role="status" className="cloud-warning">
          {at('adjunct.projectActivity.copy22')}
        </p>
      )}
      {loaded && !events.length && (
        <p className="activity-empty">{at('adjunct.projectActivity.copy23')}</p>
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
                    <button key={id} onClick={() => onLocate(id)}>
                      {e.title || at('adjunct.projectActivity.copy26')}
                      <ChevronRight size={13} />
                    </button>
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
        <button disabled={busy} onClick={() => read(true)}>
          {at('adjunct.projectActivity.copy31')}
        </button>
      )}
    </section>
  );
}
