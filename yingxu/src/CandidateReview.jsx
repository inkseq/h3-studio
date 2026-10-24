import { useI18n } from './i18n/I18nProvider.jsx';
import { FloatingMessage } from './floating-notice.js';
import GeneratedSoundAction from './GeneratedSoundAction.jsx';
import { reviewProgress, shotReviewState, reviewLabels } from './production-model.js';
import React, { useState } from 'react';
import { store } from './store.js';
import { getFile, download } from './media.js';
import AssetPreview from './AssetPreview.jsx';
import { storyboardUrl } from './storyboard.js';
import './v6-journey.css';
const issues = ['人物或造型', '动作与表演', '构图与机位', '跨镜连续性', '节奏', '其他'];
export default function CandidateReview({
  project: p,
  shots,
  currentShot: shot,
  onChooseShot,
  onInspect,
  onReferences,
}) {
  const { t, locale } = useI18n();
  const [addId, setAddId] = useState('');
  if (!shot)
    return (
      <div className="journey-empty">
        <h3>{t('h3more.copy99')}</h3>
        <button onClick={onReferences}>{t('h3more.copy100')}</button>
      </div>
    );
  const cloudMode = store.getState().workspace.mode === 'cloud';
  const progress = reviewProgress(p, shots, shot.id, { cloud: cloudMode }),
    at = shots.findIndex((s) => s.id === shot.id);
  const review = shot.data.uxReview || {},
    asset = p.entities.find((e) => e.id === shot.data.selectedAssetId),
    chosen = asset ? null : review.chosen;
  const candidates = [
    ...new Set([...(shot.data.candidateIds || []), shot.data.selectedAssetId].filter(Boolean)),
  ]
    .map((id) => p.entities.find((e) => e.id === id))
    .filter(Boolean);
  const update = (patch) =>
    store.updateEntity(
      shot.id,
      {
        data: {
          uxReview: {
            ...store.getState().project.entities.find((e) => e.id === shot.id).data.uxReview,
            ...patch,
          },
        },
      },
      { coalesceKey: patch.note !== undefined ? 'review:' + shot.id + ':note' : null },
    );
  const choose = (kind, id) =>
    store.updateEntity(shot.id, {
      status: 'draft',
      data: {
        acceptPlaceholder: false,
        selectedAssetId: kind === 'asset' ? id : '',
        uxReview: {
          ...review,
          chosen: kind === 'demo' ? id : null,
          status: 'selected',
          allRejected: false,
        },
      },
    });
  const names = {
    placeholder: t('h3more.copy101'),
    selected: t('h3more.copy102'),
    revision: t('h3more.copy103'),
    failed: t('h3more.copy104'),
    retry: t('h3more.copy105'),
    processing: t('h3more.copy106'),
    cancelled: t('h3more.copy107'),
  };
  return (
    <div className="candidate-review">
      <nav className="review-navigator" aria-label={t('h3more.copy108')}>
        <p role="status">
          {t('h3more.copy109')} {progress.done} / {progress.total} {t('h3more.copy110')}{' '}
          {progress.remaining} {t('h3more.copy111')}
        </p>
        <div className="cloud-actions">
          <button disabled={at <= 0} onClick={() => onChooseShot(shots[at - 1].id)}>
            {t('h3more.copy112')}
          </button>
          <button disabled={at >= shots.length - 1} onClick={() => onChooseShot(shots[at + 1].id)}>
            {t('h3more.copy113')}
          </button>
          <button disabled={!progress.next} onClick={() => onChooseShot(progress.next)}>
            {t('h3more.copy114')}
          </button>
        </div>
        {!progress.next && progress.remaining > 0 && (
          <p>
            {progress.total === 1 ? t('h3more.copy115') : t('h3more.copy116')}
            {t('h3more.copy117')}
          </p>
        )}
        {!progress.remaining && <p>{t('h3more.copy118')}</p>}
      </nav>
      <label className="journey-field">
        <span>{t('h3more.copy119')}</span>
        <select
          aria-label={t('h3more.copy119')}
          value={shot.id}
          onChange={(e) => onChooseShot(e.target.value)}
        >
          {shots.map((s) => (
            <option key={s.id} value={s.id}>
              {s.title} · {t('h3more.review.' + shotReviewState(p, s, { cloud: cloudMode }))}
            </option>
          ))}
        </select>
      </label>
      <p className="journey-review-brief">
        {(cloudMode ? shot.data.prompt : shot.description) ||
          shot.description ||
          t('h3more.copy120')}
      </p>
      <div className="journey-soft-panel">
        <strong>
          {asset
            ? t('h3more.copy121', { p0: asset.title })
            : chosen
              ? t('h3more.copy122', { p0: chosen })
              : t('h3more.copy123')}
        </strong>
        <p>{t('h3more.copy124')}</p>
        <button onClick={() => onInspect(shot.id)}>{t('h3more.copy125')}</button>
      </div>
      {asset?.type === 'video' && (
        <GeneratedSoundAction key={shot.id + ':' + asset.id} project={p} shot={shot} />
      )}
      <div className="v6-inline-form">
        <label>
          {t('h3more.copy126')}
          <select
            aria-label={t('h3more.copy126')}
            value={addId}
            onChange={(e) => setAddId(e.target.value)}
          >
            <option value="">{t('h3more.copy127')}</option>
            {p.entities
              .filter(
                (e) =>
                  ['image', 'video'].includes(e.type) &&
                  e.data.fileId &&
                  !candidates.some((c) => c.id === e.id),
              )
              .map((e) => (
                <option key={e.id} value={e.id}>
                  {e.title}
                </option>
              ))}
          </select>
        </label>
        <button
          disabled={!addId}
          onClick={() => {
            store.updateEntity(shot.id, {
              data: { candidateIds: [...new Set([...candidates.map((c) => c.id), addId])] },
            });
            setAddId('');
          }}
        >
          {t('h3more.copy128')}
        </button>
      </div>
      {!!candidates.length && (
        <div className="journey-candidates">
          {candidates.map((c) => (
            <article
              className={'journey-candidate ' + (asset?.id === c.id ? 'chosen' : '')}
              key={c.id}
            >
              <div className="v6-asset">
                <AssetPreview entity={c} />
              </div>
              <div>
                <h3>{c.title}</h3>
                {c.data.simulation && <p className="cloud-warning">{t('h3more.copy129')}</p>}
                {(c.data.oldVersion ||
                  (c.data.sourceShotVersion && c.data.sourceShotVersion !== shot.version)) && (
                  <p className="cloud-warning">{t('h3more.copy130')}</p>
                )}
                <button
                  disabled={c.data.missingFile || !c.data.fileId}
                  aria-pressed={asset?.id === c.id}
                  onClick={() => choose('asset', c.id)}
                >
                  {t('h3more.copy131')} {c.title}
                </button>
                <button
                  className="journey-minor-button"
                  disabled={!c.data.fileId || c.data.missingFile}
                  onClick={async () => {
                    try {
                      const file = await getFile(c.data.fileId);
                      if (!file) throw Error(t('h3more.copy132'));
                      download(file, c.data.fileName || c.title);
                    } catch (error) {
                      store.notify(t('h3more.copy133') + error.message);
                    }
                  }}
                >
                  {t('h3more.copy134')}
                </button>
                <button
                  className="journey-minor-button"
                  disabled={asset?.id === c.id}
                  onClick={() =>
                    store.updateEntity(shot.id, {
                      data: {
                        candidateIds: candidates.filter((a) => a.id !== c.id).map((a) => a.id),
                      },
                    })
                  }
                >
                  {t('h3more.copy135')}
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
      {!cloudMode && (
        <details className="v6-demo-candidates" open={!candidates.length}>
          <summary>{t('h3more.copy136')}</summary>
          <p className="journey-demo-note">{t('h3more.copy137')}</p>
          <div className="journey-candidates">
            {['A', 'B'].map((c) => (
              <article key={c} className={'journey-candidate ' + (chosen === c ? 'chosen' : '')}>
                <div className="journey-candidate-image">
                  <img src={storyboardUrl(shot, c)} alt={t('h3more.copy138', { p0: c })} />
                </div>
                <div>
                  <h3>{c === 'A' ? t('h3more.copy139') : t('h3more.copy140')}</h3>
                  <button aria-pressed={chosen === c} onClick={() => choose('demo', c)}>
                    {t('h3more.copy141')} {c}
                  </button>
                </div>
              </article>
            ))}
          </div>
        </details>
      )}
      <div className="journey-review-state" data-testid="journey-review-state" role="status">
        <span>
          {asset
            ? t('h3more.copy142', { p0: asset.title })
            : chosen
              ? t('h3more.copy143', { p0: chosen })
              : t('h3more.copy144')}{' '}
          ·{' '}
          <strong data-testid="journey-review-status">
            {names[review.status] || t('h3more.copy145')}
          </strong>
        </span>
      </div>
      {shot.status === 'review' && (asset || chosen) && (
        <div className="journey-review-warning">
          <p>{t('h3more.copy146')}</p>
          <button onClick={() => choose(asset ? 'asset' : 'demo', asset?.id || chosen)}>
            {t('h3more.copy147')}
          </button>
        </div>
      )}
      <div className="journey-soft-panel">
        <h3>{t('h3more.copy148')}</h3>
        <fieldset className="v6-tags">
          <legend>{t('h3more.copy149')}</legend>
          {issues.map((issue, issueIndex) => (
            <label key={issue}>
              <input
                type="checkbox"
                checked={(review.issues || []).includes(issue)}
                onChange={(e) =>
                  update({
                    issues: e.target.checked
                      ? [...(review.issues || []), issue]
                      : (review.issues || []).filter((x) => x !== issue),
                  })
                }
              />
              {t('h3more.issue' + issueIndex)}
            </label>
          ))}
        </fieldset>
        <label className="journey-field">
          <span>{t('h3more.copy150')}</span>
          <textarea
            value={review.note || ''}
            rows={3}
            maxLength={12000}
            onChange={(e) => update({ note: e.target.value })}
            placeholder={t('h3more.copy151')}
          />
        </label>
        <div className="v6-actions">
          <button disabled={!review.note?.trim()} onClick={() => update({ status: 'revision' })}>
            {t('h3more.copy152')}
          </button>
          {cloudMode && (
            <button
              disabled={!review.note?.trim()}
              onClick={() => {
                const prompt = (shot.data.prompt || shot.description || '').trim();
                store.updateEntity(shot.id, {
                  data: {
                    prompt: prompt + '\n\n返修要求：' + review.note.trim(),
                    uxReview: { ...review, status: 'revision' },
                  },
                });
                store.notify(t('h3more.copy153'));
                onInspect(shot.id);
              }}
            >
              {t('h3more.copy154')}
            </button>
          )}
          <button
            onClick={() => {
              update({ status: 'revision', allRejected: true });
              store.notify(t('h3more.copy155'));
            }}
          >
            {t('h3more.copy156')}
          </button>
        </div>
        {review.allRejected && <p role="status">{t('h3more.copy157')}</p>}
      </div>
      {review.status === 'failed' && (
        <div className="journey-failure" role="alert">
          <strong>{t('h3more.copy158')}</strong>
          <p>{t('h3more.copy159')}</p>
          <button onClick={onReferences}>{t('h3more.copy160')}</button>
        </div>
      )}
      {!cloudMode && (
        <details className="journey-demo-states">
          <summary>{t('h3more.copy161')}</summary>
          <p>{t('h3more.copy162')}</p>
          <div>
            <button onClick={() => update({ status: 'processing' })}>{t('h3more.copy163')}</button>
            <button onClick={() => update({ status: 'failed' })}>{t('h3more.copy164')}</button>
            <button
              disabled={!['failed', 'cancelled', 'revision'].includes(review.status)}
              onClick={() => update({ status: 'retry' })}
            >
              {t('h3more.copy165')}
            </button>
            <button onClick={() => update({ status: 'cancelled' })}>{t('h3more.copy166')}</button>
          </div>
        </details>
      )}
    </div>
  );
}
