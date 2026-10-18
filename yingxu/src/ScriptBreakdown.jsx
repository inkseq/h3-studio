import { legacyReason } from './i18n/legacyReasons.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import React, { useRef, useState } from 'react';
import {
  Check,
  FileText,
  RotateCcw,
  Scissors,
  ArrowDown,
  Trash2,
  Plus,
  AlertCircle,
} from 'lucide-react';
import { store } from './store.js';
import {
  applyBreakdown,
  breakdownIssues,
  createBreakdown,
  importUndoIssues,
  mergeBreakdownSegment,
  replaceBreakdown,
  sourceLocation,
  splitBreakdownSegment,
  undoBreakdownImport,
} from './script-breakdown-model.js';
import './script-breakdown.css';

function ImportReceipt({ project, record, onInspect }) {
  const { t: lt, locale } = useI18n();
  const [asked, setAsked] = useState(false),
    receipt = record.receipt;
  if (!receipt) return null;
  const issues = importUndoIssues(project, receipt, locale),
    scenes = receipt.entities.filter((e) => e.type === 'scene');
  return (
    <section className="breakdown-receipt" aria-label={lt('legacy.panel32')}>
      <strong>
        {receipt.undone ? lt('script.ui26') : lt('script.imported', { count: scenes.length })}
      </strong>
      <p>{receipt.undone ? lt('script.ui33') : lt('script.ui24')}</p>
      <details className="breakdown-original">
        <summary>{lt('legacy.panel1')}</summary>
        <pre>{record.source}</pre>
      </details>
      {!receipt.undone && (
        <>
          <div className="breakdown-actions">
            {scenes.map(
              (scene) =>
                project.entities.some((e) => e.id === scene.id) && (
                  <button key={scene.id} onClick={() => onInspect?.(scene.id)}>
                    {lt('script.open', {
                      title: project.entities.find((e) => e.id === scene.id)?.title || scene.title,
                    })}
                  </button>
                ),
            )}
            <button onClick={() => setAsked(!asked)}>
              <RotateCcw size={14} />
              {lt('legacy.panel2')}
            </button>
          </div>
          {asked &&
            (issues.length ? (
              <div className="breakdown-warning" role="alert">
                <strong>{lt('legacy.panel3')}</strong>
                <p>{lt('legacy.panel4')}</p>
                <ul>
                  {issues.map((issue, index) => (
                    <li key={index}>
                      {issue.title}：{issue.reason}
                      {issue.id && (
                        <button onClick={() => onInspect?.(issue.id)}>{lt('legacy.panel5')}</button>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <div className="breakdown-warning">
                <p>
                  {lt('script.removeImport', {
                    count: scenes.length,
                    chapter: receipt.createdChapter ? lt('script.newChapterToo') : '',
                  })}
                </p>
                <button
                  onClick={() => {
                    if (store.editProject((p) => undoBreakdownImport(p, record.batchId)))
                      setAsked(false);
                  }}
                >
                  {lt('legacy.panel6')}
                </button>
                <button onClick={() => setAsked(false)}>{lt('legacy.panel7')}</button>
              </div>
            ))}
        </>
      )}
    </section>
  );
}

export default function ScriptBreakdown({ project, onInspect }) {
  const { t: lt, locale } = useI18n();
  const [reanalyse, setReanalyse] = useState(false),
    [staleAck, setStaleAck] = useState(''),
    [message, setMessage] = useState('');
  const cursors = useRef({});
  const source = String(project.journey?.brief?.sourceScript || ''),
    draft = project.journey?.breakdown;
  const active = draft?.segments?.filter((s) => !s.omitted) || [],
    omitted = draft?.segments?.filter((s) => s.omitted) || [],
    chapters = project.entities
      .filter((e) => e.type === 'chapter')
      .sort((a, b) => a.order - b.order);
  const editable = draft?.status === 'draft',
    stale = !!draft && source !== draft.source,
    issues = editable ? breakdownIssues(project, draft, locale) : [];
  const edit = (mutator, key) =>
    store.editProject(
      (p) => {
        const current = p.journey?.breakdown;
        if (!current || current.batchId !== draft.batchId || current.status !== 'draft')
          throw Error(lt('script.ui35'));
        mutator(current);
      },
      { coalesceKey: key || null },
    );
  const analyse = () => {
    try {
      const next = createBreakdown(source, {
        chapterTitle: draft?.chapterTitle || '剧本拆解',
        targetChapterId: chapters.some((c) => c.id === draft?.targetChapterId)
          ? draft.targetChapterId
          : '',
      });
      if (store.editProject((p) => replaceBreakdown(p, next))) {
        setReanalyse(false);
        setStaleAck('');
        setMessage(lt('script.prepared', { count: next.segments.length }));
      }
    } catch (error) {
      setMessage(legacyReason(error.message, locale));
    }
  };
  const apply = () => {
    if (stale && staleAck !== draft.batchId) return;
    let receipt;
    const done = store.editProject((p) => {
      receipt = applyBreakdown(p, draft.batchId);
    });
    if (done) {
      store.setScope(receipt.chapterId);
      store.select(null);
      setMessage(lt('script.confirmed', { count: active.length }));
    }
  };
  return (
    <section
      className="script-breakdown"
      data-testid="script-breakdown"
      aria-label={lt('legacy.panel33')}
    >
      <header>
        <FileText size={22} />
        <div>
          <h3>{lt('legacy.panel8')}</h3>
          <p>{lt('legacy.panel9')}</p>
        </div>
      </header>
      {!draft ? (
        <div className="breakdown-start">
          <p>{lt('legacy.panel10')}</p>
          <button
            className="primary"
            disabled={!source.trim()}
            onClick={analyse}
            data-testid="breakdown-analyse"
          >
            {lt('legacy.panel11')}
          </button>
          {!source.trim() && <p>{lt('legacy.panel12')}</p>}
        </div>
      ) : (
        <>
          <div className="breakdown-summary">
            <span>{lt('script.summary', { active: active.length, omitted: omitted.length })}</span>
            <span>
              {editable
                ? lt('script.ui29')
                : draft.status === 'undone'
                  ? lt('script.ui32')
                  : lt('script.ui27')}
            </span>
            <button onClick={() => setReanalyse(!reanalyse)}>{lt('legacy.panel13')}</button>
          </div>
          {reanalyse && (
            <div className="breakdown-warning">
              <strong>{lt('legacy.panel14')}</strong>
              <p>{editable ? lt('script.ui28') : lt('script.ui23')}</p>
              <button onClick={analyse}>{lt('legacy.panel15')}</button>
              <button onClick={() => setReanalyse(false)}>{lt('legacy.panel16')}</button>
            </div>
          )}
          {stale && (
            <div className="breakdown-warning">
              <strong>{lt('legacy.panel17')}</strong>
              <p>{lt('legacy.panel18')}</p>
              {editable && (
                <label>
                  <input
                    type="checkbox"
                    checked={staleAck === draft.batchId}
                    onChange={(e) => setStaleAck(e.target.checked ? draft.batchId : '')}
                  />
                  {lt('legacy.panel19')}
                </label>
              )}
            </div>
          )}
          <details className="breakdown-original">
            <summary>{lt('legacy.panel20')}</summary>
            <pre>{draft.source}</pre>
          </details>
          <div className="breakdown-scene-list">
            {active.map((segment, index) => (
              <article className="breakdown-scene" key={segment.id} data-testid="breakdown-scene">
                <div className="breakdown-scene-heading">
                  <span>{lt('script.scene', { number: index + 1 })}</span>
                  {editable && (
                    <button
                      onClick={() =>
                        edit((current) => {
                          current.segments.find((s) => s.id === segment.id).omitted = true;
                        })
                      }
                      aria-label={lt('script.omitScene', { number: index + 1 })}
                    >
                      <Trash2 size={14} />
                      {lt('legacy.panel21')}
                    </button>
                  )}
                </div>
                <label>
                  {lt('script.sceneName', { number: index + 1 })}
                  <input
                    aria-label={lt('script.sceneName', { number: index + 1 })}
                    value={segment.title}
                    maxLength={160}
                    readOnly={!editable}
                    onChange={(e) =>
                      edit((current) => {
                        current.segments.find((s) => s.id === segment.id).title = e.target.value;
                      }, `breakdown:title:${segment.id}`)
                    }
                  />
                </label>
                <label>
                  {lt('script.sceneText', { number: index + 1 })}
                  <textarea
                    aria-label={lt('script.sceneText', { number: index + 1 })}
                    rows={4}
                    value={segment.script}
                    maxLength={120000}
                    readOnly={!editable}
                    onSelect={(e) => {
                      cursors.current[segment.id] = e.currentTarget.selectionStart;
                    }}
                    onClick={(e) => {
                      cursors.current[segment.id] = e.currentTarget.selectionStart;
                    }}
                    onChange={(e) => {
                      const value = e.target.value;
                      edit((current) => {
                        const item = current.segments.find((s) => s.id === segment.id);
                        item.script = value;
                        item.mapping =
                          item.ranges.length === 1 &&
                          current.source.slice(item.ranges[0].start, item.ranges[0].end) === value
                            ? 'exact'
                            : 'shared';
                      }, `breakdown:script:${segment.id}`);
                    }}
                  />
                </label>
                <details className="breakdown-source">
                  <summary>
                    {lt('script.original', {
                      location: segment.ranges
                        .map((range) => sourceLocation(draft.source, range, locale))
                        .join('; '),
                    })}
                  </summary>
                  {segment.ranges.map((range, i) => (
                    <div key={i}>
                      <small>{sourceLocation(draft.source, range, locale)}</small>
                      <blockquote>{draft.source.slice(range.start, range.end)}</blockquote>
                    </div>
                  ))}
                  <p>{segment.mapping === 'shared' ? lt('script.ui36') : lt('script.ui34')}</p>
                </details>
                {editable && (
                  <div className="breakdown-split">
                    <p>{lt('legacy.panel22')}</p>
                    <div className="breakdown-actions">
                      <button
                        onClick={() =>
                          edit((current) =>
                            splitBreakdownSegment(current, segment.id, cursors.current[segment.id]),
                          )
                        }
                      >
                        <Scissors size={14} />
                        {lt('legacy.panel23')}
                      </button>
                      <button
                        disabled={index === active.length - 1}
                        onClick={() =>
                          edit((current) => mergeBreakdownSegment(current, segment.id))
                        }
                      >
                        <ArrowDown size={14} />
                        {lt('legacy.panel24')}
                      </button>
                    </div>
                  </div>
                )}
              </article>
            ))}
          </div>
          {omitted.length > 0 && (
            <details className="breakdown-omitted" open>
              <summary>{lt('script.omitted', { count: omitted.length })}</summary>
              {omitted.map((segment) => (
                <div key={segment.id}>
                  <div>
                    <strong>{segment.title || lt('script.ui31')}</strong>
                    <p>
                      {segment.script.slice(0, 160)}
                      {segment.script.length > 160 ? '…' : ''}
                    </p>
                  </div>
                  {editable && (
                    <button
                      onClick={() =>
                        edit((current) => {
                          current.segments.find((s) => s.id === segment.id).omitted = false;
                        })
                      }
                    >
                      <Plus size={14} />
                      {lt('legacy.panel25')}
                    </button>
                  )}
                </div>
              ))}
            </details>
          )}
          {editable && (
            <div className="breakdown-confirm">
              <h4>{lt('legacy.panel26')}</h4>
              <label>
                {lt('legacy.panel27')}
                <select
                  aria-label={lt('legacy.panel27')}
                  value={draft.targetChapterId || ''}
                  onChange={(e) =>
                    edit((current) => {
                      current.targetChapterId = e.target.value;
                    })
                  }
                >
                  <option value="">{lt('legacy.panel28')}</option>
                  {chapters.map((chapter) => (
                    <option key={chapter.id} value={chapter.id}>
                      {lt('script.appendTo', { title: chapter.title })}
                    </option>
                  ))}
                  {draft.targetChapterId &&
                    !chapters.some((c) => c.id === draft.targetChapterId) && (
                      <option value={draft.targetChapterId}>{lt('legacy.panel29')}</option>
                    )}
                </select>
              </label>
              {!draft.targetChapterId && (
                <label>
                  {lt('legacy.panel30')}
                  <input
                    aria-label={lt('legacy.panel30')}
                    value={draft.chapterTitle}
                    maxLength={160}
                    onChange={(e) =>
                      edit((current) => {
                        current.chapterTitle = e.target.value;
                      }, 'breakdown:chapter-title')
                    }
                  />
                </label>
              )}
              <p>
                {lt('script.applySummary', {
                  action: lt(draft.targetChapterId ? 'script.ui25' : 'script.ui30'),
                  count: active.length,
                  omitted: omitted.length
                    ? lt('script.omittedClause', { count: omitted.length })
                    : '',
                })}
              </p>
              {issues.length > 0 && (
                <ul className="breakdown-issues">
                  {issues.map((issue) => (
                    <li key={issue}>
                      <AlertCircle size={14} />
                      {issue}
                    </li>
                  ))}
                </ul>
              )}
              <button
                className="primary"
                disabled={issues.length > 0 || (stale && staleAck !== draft.batchId)}
                onClick={apply}
                data-testid="breakdown-apply"
              >
                <Check size={16} />
                {lt('script.confirmImport', { count: active.length })}
              </button>
              <small>{lt('legacy.panel31')}</small>
            </div>
          )}
          {draft.receipt && (
            <ImportReceipt
              key={draft.batchId}
              project={project}
              record={draft}
              onInspect={onInspect}
            />
          )}
        </>
      )}
      {message && (
        <p role="status" className="breakdown-message">
          {message}
        </p>
      )}
      {!!project.journey?.breakdownImports?.length && (
        <details className="breakdown-history">
          <summary>
            {lt('script.history', { count: project.journey.breakdownImports.length })}
          </summary>
          {project.journey.breakdownImports.map((record) => (
            <ImportReceipt
              key={record.batchId}
              project={project}
              record={record}
              onInspect={onInspect}
            />
          ))}
        </details>
      )}
    </section>
  );
}
