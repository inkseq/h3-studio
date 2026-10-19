import React from 'react';
import { Download, RefreshCw, Square, Play, Check } from 'lucide-react';
import { formatMoneyMicros } from './money.js';
import { filmTerminal } from './story-film.js';

export function filmProblem(code) {
  const own = [
    'busy',
    'pending',
    'saveFirst',
    'unavailable',
    'contextChanged',
    'receiptChanged',
    'pendingCorrupt',
    'confirmChanged',
    'active',
    'notResumable',
    'notComplete',
    'unknown',
  ];
  if (own.some((key) => code === 'film.' + key)) return code;
  return (
    {
      film_original_job_failed: 'film.originalFailed',
      film_captions_require_confirmation: 'film.captionRequired',
      film_captions_invalid: 'film.captionInvalid',
      film_sound_plan_required: 'film.soundRequired',
      film_selected_recipe_unknown: 'film.selectedUnknown',
      film_selected_take_outdated: 'film.selectedOutdated',
      film_generation_preflight_blocked: 'film.inputsBlocked',
      film_render_input_blocked: 'film.inputsBlocked',
      film_shot_duration_invalid: 'film.inputsBlocked',
      document_version_conflict: 'film.changed',
      film_source_changed: 'film.changed',
      film_transport_unavailable: 'film.unavailable',
      film_actor_mismatch: 'film.actorBlocked',
      film_not_resumable: 'film.notResumable',
    }[code] || 'film.blockedMessage'
  );
}

export function FilmRunView({
  state,
  t,
  dirty,
  currentVersion,
  optionsMatch,
  confirmed,
  onConfirmed,
  onConfirm,
  onRefresh,
  onRecover,
  onResume,
  onCancel,
  onDownload,
  preview,
}) {
  const run = state.run;
  if (!run) return state.pending ? <p role="status">{t('film.pending')}</p> : null;
  const count = run.counts,
    preflight = run.phase === 'preflight',
    terminal = filmTerminal(run),
    stale = dirty || currentVersion !== run.project_version || !optionsMatch;
  const quoted =
    run.estimate?.quote_known === true &&
    Number.isSafeInteger(run.estimate.cost_microusd) &&
    run.estimate.cost_microusd >= 0;
  const locked = state.busy || !!state.pending;
  const steps = [
    ['generate', count.generated_completed, count.generate],
    [
      run.options?.burn_subtitles ? 'captions' : 'chapters',
      count.chapters_completed,
      run.chapters.length,
    ],
    ['export', count.export_completed, 1],
  ];
  return (
    <section className="film-run">
      <div className="film-run-title">
        <strong role="status">{t('film.phase.' + run.phase)}</strong>
        <span>{t('film.revision', { version: run.project_version })}</span>
      </div>
      <p>{t('film.frozen', { version: run.project_version })}</p>
      <p>{t('film.planCounts', count)}</p>
      {!preflight && (
        <ol className="film-steps">
          {steps.map(([name, done, total]) => (
            <li key={name} className={done === total ? 'complete' : ''}>
              <span>
                {done === total && <Check size={14} />}
                {t('film.step.' + name)}
              </span>
              <strong>
                {done} / {total}
              </strong>
              <progress value={done} max={Math.max(1, total)} aria-label={t('film.step.' + name)} />
            </li>
          ))}
        </ol>
      )}
      {preflight && (
        <>
          <strong className="film-estimate">
            {quoted
              ? t('film.estimate', { amount: formatMoneyMicros(run.estimate.cost_microusd) })
              : t('film.estimateUnknown')}
          </strong>
          <p className="muted">{t('film.estimateNote')}</p>
          {stale && <p role="alert">{t('film.changed')}</p>}
          <label className="film-check">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={locked || stale || !quoted}
              onChange={(event) => onConfirmed(event.target.checked)}
            />
            {t('film.confirmLabel')}
          </label>
          <button
            type="button"
            className="primary"
            disabled={locked || stale || !quoted || !confirmed}
            onClick={onConfirm}
          >
            <Play size={14} />
            {t('film.confirm')}
          </button>
        </>
      )}
      {run.phase === 'admission_blocked' && <p role="status">{t('film.waiting')}</p>}
      {run.phase === 'authorization_required' && <p role="status">{t('film.authorization')}</p>}
      {run.error_code && <p role="alert">{t(filmProblem(run.error_code))}</p>}
      {run.phase === 'completed' && run.export.artifact_id && (
        <div className="film-result">
          {preview}
          <button type="button" disabled={locked} onClick={onDownload}>
            <Download size={14} />
            {t('film.download')}
          </button>
        </div>
      )}
      <div className="film-actions">
        <button type="button" disabled={state.busy || !!state.pending} onClick={onRefresh}>
          <RefreshCw size={14} />
          {t('film.refresh')}
        </button>
        {!terminal && !preflight && !run.cancel_requested && (
          <>
            {['blocked', 'admission_blocked', 'authorization_required'].includes(run.phase) && (
              <button type="button" disabled={locked} onClick={onResume}>
                <Play size={14} />
                {t('film.resume')}
              </button>
            )}
            <button type="button" disabled={locked} onClick={onCancel}>
              <Square size={14} />
              {t('film.cancel')}
            </button>
          </>
        )}
        {state.pending && (
          <button type="button" disabled={state.busy} onClick={onRecover}>
            {t('film.recover')}
          </button>
        )}
      </div>
      {!terminal && !preflight && <p className="muted">{t('film.cancelNote')}</p>}
      {run.phase === 'blocked' && <p className="muted">{t('film.resumeNote')}</p>}
      <details>
        <summary>{t('film.details')}</summary>
        <p>{t('film.runId', { id: run.id })}</p>
        <p>{t('film.sourceHash', { hash: run.snapshot_hash })}</p>
        {run.error_code && <code>{run.error_code}</code>}
        {run.warnings?.map((code) => (
          <p key={code}>{code}</p>
        ))}
      </details>
    </section>
  );
}
