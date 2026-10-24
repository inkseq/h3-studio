import { useI18n } from './i18n/I18nProvider.jsx';
import { FloatingMessage } from './floating-notice.js';
import React, { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { store } from './store.js';
import { cloudController as cloud } from './cloud-controller.js';
import { useCloud, JobList } from './CloudStudio.jsx';
import OperatorBudgetPanel from './OperatorBudgetPanel.jsx';
import { estimateLabel, capacityWaitMessage } from './cloud-model.js';
import {
  productionRows,
  filterProductionRows,
  selectedProductionRows,
  batchSummary,
  queueFilters,
  reviewLabels,
} from './production-model.js';
import './production-queue.css';

const run = (promise) => promise.catch(() => {});
const issue = (item, lt) =>
  typeof item === 'string' ? item : item?.message || item?.detail || lt('queue.input');
export default function ProductionQueue({ project, onInspect }) {
  const { t: lt, locale } = useI18n();
  const c = useCloud(),
    [selected, setSelected] = useState([]),
    [chapterId, setChapterId] = useState(''),
    [status, setStatus] = useState('all'),
    [query, setQuery] = useState(''),
    [confirmed, setConfirmed] = useState('');
  const rows = productionRows(project, c, Date.now(), locale),
    visible = filterProductionRows(rows, { chapterId, status, query }),
    chosen = selectedProductionRows(rows, selected),
    summary = batchSummary(chosen),
    visibleIds = new Set(visible.map((row) => row.id)),
    hidden = chosen.filter((row) => !visibleIds.has(row.id)).length;
  const chapterOptions = project.entities
      .filter((e) => e.type === 'chapter')
      .sort((a, b) => a.order - b.order),
    ready = chosen.filter((row) => row.canSubmit),
    submitReady = summary.total > 0 && summary.blocked === 0 && summary.total <= 100;
  const shownJobs = c.jobs.filter(
    (job) => job.recipe_id !== 'chapter-roughcut-v1' && visibleIds.has(job.client_ref?.shot_id),
  );
  useEffect(() => {
    setSelected([]);
    setChapterId('');
    setStatus('all');
    setQuery('');
    setConfirmed('');
  }, [project.id]);
  useEffect(
    () => setSelected((ids) => ids.filter((id) => rows.some((row) => row.id === id))),
    [project.id, rows.length],
  );
  const changeSelection = (next) => {
    setSelected(next);
    setConfirmed('');
  };
  const openReview = (id) => {
    const row = rows.find((r) => r.id === id);
    if (!row) return;
    store.setScope(row.chapter.id);
    store.setSection('journey');
    store.updateProject({
      journey: { ...store.getState().project.journey, stage: 5, reviewShotId: id },
    });
    store.select(null);
  };
  const preflight = () => {
    setConfirmed('');
    run(cloud.prepareBatch(chosen.map((row) => row.id)));
  };
  return (
    <section className="cloud-production" aria-label={lt('legacy.route91')}>
      <div className="cloud-section-head">
        <h2>{lt('legacy.route68')}</h2>
        <button disabled={c.busy} onClick={() => run(cloud.refresh())}>
          <RefreshCw size={14} />
          {lt('legacy.route69')}
        </button>
      </div>
      <p>{lt('legacy.explain21')}</p>
      {c.capabilities?.simulation && <p className="cloud-warning">{lt('legacy.explain18')}</p>}
      {!c.capabilities?.execution_enabled && (
        <p className="cloud-warning">{lt('legacy.explain19')}</p>
      )}
      {c.activity ? (
        <div className="queue-activity" aria-label={lt('legacy.route92')}>
          <b>{lt('queue.project', { count: c.activity.total })}</b>
          <span>{lt('queue.succeeded', { count: c.activity.counts?.succeeded || 0 })}</span>
          <span>{lt('queue.failed', { count: c.activity.counts?.failed || 0 })}</span>
          <span>{lt('queue.waiting', { count: c.activity.counts?.waiting_capacity || 0 })}</span>
          <span>{lt('queue.queued', { count: c.activity.counts?.queued || 0 })}</span>
          <span>{lt('queue.hold', { count: c.activity.counts?.recovery_hold || 0 })}</span>
          <span>{lt('queue.unknown', { count: c.activity.counts?.submission_unknown || 0 })}</span>
          {c.activity.billing_pending > 0 && (
            <span>{lt('queue.billing', { count: c.activity.billing_pending })}</span>
          )}
          <small>
            {lt('queue.aggregate', { count: c.jobs.length })}
            {c.jobs.length < c.activity.total ? lt('queue.moreHistory') : ''}
          </small>
        </div>
      ) : (
        <p className="muted">{c.activityError || lt('queue.noTotal')}</p>
      )}
      <div className="queue-filters">
        <label>
          {lt('legacy.route70')}
          <select
            aria-label={lt('legacy.route93')}
            value={chapterId}
            onChange={(e) => setChapterId(e.target.value)}
          >
            <option value="">{lt('legacy.route71')}</option>
            {chapterOptions.map((chapter) => (
              <option key={chapter.id} value={chapter.id}>
                {chapter.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          {lt('legacy.route72')}
          <select
            aria-label={lt('legacy.route94')}
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            {Object.entries(queueFilters).map(([value, label]) => (
              <option key={value} value={value}>
                {lt('queue.filter.' + value)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {lt('legacy.route73')}
          <input
            type="search"
            aria-label={lt('legacy.route95')}
            placeholder={lt('legacy.route96')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
      </div>
      {(chapterId || status !== 'all' || query) && (
        <button
          onClick={() => {
            setChapterId('');
            setStatus('all');
            setQuery('');
          }}
        >
          {lt('legacy.route74')}
        </button>
      )}
      <div className="cloud-actions queue-select">
        <span role="status">
          {lt('queue.selection', {
            visible: visible.length,
            total: rows.length,
            chosen: chosen.length,
          })}
        </span>
        <button
          disabled={c.busy || !visible.some((row) => row.canSelect)}
          onClick={() =>
            changeSelection([
              ...new Set([
                ...selected,
                ...visible.filter((row) => row.canSelect).map((row) => row.id),
              ]),
            ])
          }
        >
          {lt('legacy.route75')}
        </button>
        <button
          disabled={c.busy || !visible.some((row) => selected.includes(row.id))}
          onClick={() => changeSelection(selected.filter((id) => !visibleIds.has(id)))}
        >
          {lt('legacy.route76')}
        </button>
        <button disabled={c.busy || !chosen.length} onClick={() => changeSelection([])}>
          {lt('legacy.route77')}
        </button>
      </div>
      {hidden > 0 && (
        <p className="cloud-warning">
          {lt('queue.hidden', { count: hidden })}
          <button
            disabled={c.busy}
            onClick={() => changeSelection(selected.filter((id) => visibleIds.has(id)))}
          >
            {lt('legacy.route78')}
          </button>
        </p>
      )}
      {!visible.length && <p className="ui-empty ui-empty-compact">{lt('legacy.explain20')}</p>}
      <div className="queue-rows">
        {visible.map((row) => (
          <article key={row.id} data-testid={'queue-shot-' + row.id}>
            <div className="queue-row-main">
              <label>
                <input
                  type="checkbox"
                  aria-label={lt('queue.select', { title: row.shot.title })}
                  disabled={c.busy || !row.canSelect}
                  checked={selected.includes(row.id)}
                  onChange={(e) =>
                    changeSelection(
                      e.target.checked
                        ? [...selected, row.id]
                        : selected.filter((id) => id !== row.id),
                    )
                  }
                />
                <span>
                  <b>{row.shot.title}</b>
                  <small>{row.path}</small>
                </span>
              </label>
              <span>{row.label}</span>
            </div>
            <p>
              {lt('queue.shotSummary', {
                version: row.shot.version,
                seconds: row.shot.data.seconds,
              })}{' '}
              · {lt('queue.review.' + row.review)}
              {row.plan ? ` · ${estimateLabel(row.plan.estimate, locale)}` : ''}
            </p>
            {row.phase === 'waiting_capacity' && (
              <p className="cloud-warning">
                {capacityWaitMessage(
                  row.jobs.find((job) => job.status === 'waiting_capacity'),
                  locale,
                )}
              </p>
            )}
            {row.phase === 'recovery_hold' && (
              <p className="cloud-warning">{lt('legacy.explain24')}</p>
            )}
            {row.error && (
              <p role="alert" className="cloud-error">
                {lt('queue.error', { error: row.error })}
              </p>
            )}
            {row.current &&
              row.plan?.blockers?.map((item, i) => (
                <p key={i} className="cloud-error">
                  {issue(item, lt)}
                </p>
              ))}
            <div className="cloud-actions">
              <button onClick={() => onInspect(row.id)}>
                {['failed', 'error', 'blocked', 'stale', 'expired'].includes(row.phase)
                  ? lt('queue.revise')
                  : lt('queue.configure')}
              </button>
              <button onClick={() => openReview(row.id)}>{lt('legacy.route79')}</button>
              {c.unknown?.[row.id] && (
                <button disabled={c.busy} onClick={() => run(cloud.submit(row.id))}>
                  {lt('legacy.route80')}
                </button>
              )}
            </div>
          </article>
        ))}
      </div>
      {c.batchChecking?.projectId === project.id && (
        <p role="status">
          {lt('queue.checking', {
            title:
              rows.find((row) => row.id === c.batchChecking.shotId)?.shot.title || lt('queue.shot'),
            done: c.batchChecking.completed,
            total: c.batchChecking.total,
          })}
        </p>
      )}
      {chosen.length > 0 && (
        <section className="queue-summary" aria-label={lt('legacy.route97')}>
          <h3>{lt('queue.chosen', { count: summary.total })}</h3>
          <p>{lt('queue.ready', { ready: summary.ready, blocked: summary.blocked })}</p>
          <ol>
            {chosen.map((row) => (
              <li key={row.id}>
                <span>
                  <b>{row.shot.title}</b>
                  <small>
                    {row.path} · {row.label}
                  </small>
                </span>
                <button onClick={() => onInspect(row.id)}>{lt('legacy.route81')}</button>
              </li>
            ))}
          </ol>
          <div className="cloud-actions">
            <button
              disabled={c.busy || !chosen.length || chosen.some((row) => !row.canSelect)}
              onClick={preflight}
            >
              {lt('queue.preflight', { count: chosen.length })}
            </button>
            {summary.blocked > 0 && ready.length > 0 && (
              <button disabled={c.busy} onClick={() => changeSelection(ready.map((row) => row.id))}>
                {lt('queue.keep', { count: ready.length })}
              </button>
            )}
          </div>
          {summary.estimates.map((estimate, i) => (
            <p key={i}>{estimateLabel(estimate, locale)}</p>
          ))}
          {summary.unknown > 0 && (
            <p className="cloud-warning">{lt('queue.costUnknown', { count: summary.unknown })}</p>
          )}
          {summary.waitingCapacity > 0 && (
            <p className="cloud-warning">
              {lt('queue.capacity', { count: summary.waitingCapacity })}
            </p>
          )}
          <OperatorBudgetPanel account={c.account} compact />
          <label className="queue-confirm">
            <input
              type="checkbox"
              disabled={c.busy || !submitReady}
              checked={submitReady && confirmed === summary.signature}
              onChange={(e) => setConfirmed(e.target.checked ? summary.signature : '')}
            />
            {lt('legacy.route82')}
          </label>
          <button
            className="primary"
            disabled={
              c.busy ||
              !submitReady ||
              confirmed !== summary.signature ||
              !c.capabilities?.execution_enabled ||
              !!c.batchPending
            }
            onClick={() =>
              run(cloud.submitBatch(chosen.map((row) => row.id)).then(() => changeSelection([])))
            }
          >
            {lt('queue.submit', { count: chosen.length })}
          </button>
          <p>{lt('legacy.explain25')}</p>
        </section>
      )}
      {c.batchPending && (
        <div className="cloud-warning" role="alert">
          <p>
            {c.busy
              ? lt('queue.confirming')
              : lt('queue.pending', { count: c.batchPending.shotIds.length })}
          </p>
          <button disabled={c.busy} onClick={() => run(cloud.recoverBatch())}>
            {lt('legacy.route83')}
          </button>
        </div>
      )}
      <details className="queue-batches">
        <summary>{lt('queue.batches', { count: c.batches.length })}</summary>
        {c.batches.map((batch) => (
          <article key={batch.id}>
            <b>
              {([
                'active',
                'admitting',
                'completed',
                'finished_with_issues',
                'cancel_requested',
                'blocked',
              ].includes(batch.status)
                ? lt('queue.batch.' + batch.status)
                : null) || batch.status}
            </b>
            <small>{lt('queue.batch', { id: batch.id })}</small>
            {batch.items
              ?.filter((item) => item.error_code)
              .map((item) => {
                const row = rows.find(
                  (row) =>
                    row.plan?.plan_id === item.plan_id ||
                    row.jobs.some((job) => job.plan_id === item.plan_id),
                );
                return (
                  <div key={item.plan_id}>
                    <p className="cloud-error">
                      {lt('queue.notQueued', { title: row?.shot.title || lt('queue.historical') })}
                    </p>
                    <details>
                      <summary>{lt('legacy.route84')}</summary>
                      <code>{item.error_code}</code>
                    </details>
                    {row && (
                      <button onClick={() => onInspect(row.id)}>{lt('legacy.route85')}</button>
                    )}
                  </div>
                );
              })}
            {['active', 'admitting', 'cancel_requested'].includes(batch.status) && (
              <button disabled={c.busy} onClick={() => run(cloud.cancelBatch(batch.id))}>
                {lt('legacy.route86')}
              </button>
            )}
          </article>
        ))}
        {c.moreBatches && (
          <div className="queue-history">
            <button disabled={c.busy} onClick={() => run(cloud.loadMoreBatches())}>
              {lt('legacy.route87')}
            </button>
            <p>{lt('legacy.explain17')}</p>
          </div>
        )}
      </details>
      <h3>{lt('legacy.route88')}</h3>
      <p>{lt('legacy.explain23')}</p>
      <JobList jobs={shownJobs} project={project} onInspect={onInspect} onReview={openReview} />
      {!shownJobs.length && <p className="muted">{lt('legacy.route89')}</p>}
      {c.moreJobs && (
        <div className="queue-history">
          <button disabled={c.busy} onClick={() => run(cloud.loadMoreJobs())}>
            {lt('legacy.route90')}
          </button>
          <p>{lt('legacy.explain22')}</p>
        </div>
      )}
    </section>
  );
}
