import React from 'react';
import { useCloud } from './CloudStudio.jsx';
import { cloudController as cloud } from './cloud-controller.js';
import { store } from './store.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import { download } from './media.js';
import { storyTitle } from './story-open.js';
import './story-open.css';

const jsonDownload = (value, name) =>
  download(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }), name);

// Mounted once in the active story view. Open failures never fan out through
// every hidden route, status panel and shot editor sharing the cloud controller.
export default function StoryOpenNotice({ onOpened, onClose, projectId, waiting = false }) {
  const { t } = useI18n(),
    c = useCloud(),
    opening = c.projectOpen;
  const matching =
    opening?.account === c.account && (!projectId || opening.projectId === projectId);
  const offer = matching ? c.draftOffer : null,
    legacy = matching ? c.legacyOffer?.record : null;
  const retained =
    matching && opening?.status === 'error'
      ? store.getCloudDraft(c.account, opening.projectId)
      : null;
  const open = (options = {}) =>
    void cloud
      .open(projectId || opening?.projectId, options)
      .then((done) => {
        if (done) onOpened?.();
      })
      .catch(() => {});
  const close = () => {
    cloud.dismissProjectOpen();
    onClose?.();
  };
  if (!matching && !waiting) return null;
  if (!c.account)
    return (
      <section className="story-open-notice" role="status">
        <p>{t(c.sessionReady ? 'storyOpen.signIn' : 'storyOpen.checkingAccount')}</p>
        {c.sessionReady && <a href="/settings">{t('storyOpen.signInLink')}</a>}
      </section>
    );
  return (
    <section
      className="story-open-notice"
      aria-label={
        legacy
          ? t('storyOpen.legacyTitle')
          : offer
            ? t('storyOpen.draftTitle')
            : t('storyOpen.loading')
      }
    >
      <div className="story-open-heading">
        <h2>
          {legacy
            ? storyTitle(legacy.project, t('legacy.visible24'))
            : offer
              ? storyTitle(offer.record.project, t('legacy.visible24'))
              : t(opening?.status === 'error' ? 'storyOpen.errorHeading' : 'storyOpen.loading')}
        </h2>
        {!waiting && (
          <button type="button" onClick={close}>
            {t('storyOpen.close')}
          </button>
        )}
      </div>
      {legacy ? (
        <>
          <b>{t('storyOpen.legacyTitle')}</b>
          <p>{t('storyOpen.legacyBody')}</p>
          <small>
            {t('storyOpen.legacyVersion', {
              version: legacy.version,
              format: legacy.project.schemaVersion ?? t('storyOpen.unknownFormat'),
            })}
          </small>
          <div className="story-open-actions">
            <button
              type="button"
              onClick={() => jsonDownload(legacy.project, legacy.id + '.original.json')}
            >
              {t('storyOpen.downloadOriginal')}
            </button>
          </div>
          <details>
            <summary>{t('storyOpen.original')}</summary>
            <pre>{JSON.stringify(legacy.project, null, 2)}</pre>
          </details>
        </>
      ) : offer ? (
        <>
          <b>{t('storyOpen.draftTitle')}</b>
          <p>{t('storyOpen.draftBody')}</p>
          {offer.draft.serverVersion !== offer.record.version && (
            <p>{t('storyOpen.versionConflict')}</p>
          )}
          <div className="story-open-actions">
            <button
              className="primary"
              type="button"
              disabled={c.busy || offer.draft.serverVersion !== offer.record.version}
              onClick={() => open({ restoreDraft: true })}
            >
              {t('storyOpen.continueDraft')}
            </button>
            <button type="button" disabled={c.busy} onClick={() => open({ keepDraft: true })}>
              {t('storyOpen.openCloud')}
            </button>
            <button
              type="button"
              onClick={() => jsonDownload(offer.draft.project, offer.record.id + '.unsynced.json')}
            >
              {t('storyOpen.downloadDraft')}
            </button>
          </div>
        </>
      ) : opening?.status === 'error' ? (
        <>
          <p role="alert">
            {t(opening.key, { status: opening.statusCode || opening.statusLabel || 'read' })}
          </p>
          <div className="story-open-actions">
            {opening.key !== 'storyOpen.removed' && (
              <button type="button" disabled={c.busy} onClick={() => open()}>
                {t('storyOpen.retry')}
              </button>
            )}
            {retained?.dirty && (
              <button
                type="button"
                onClick={() => jsonDownload(retained.project, opening.projectId + '.unsynced.json')}
              >
                {t('storyOpen.downloadDraft')}
              </button>
            )}
          </div>
        </>
      ) : (
        <p role="status">{t('storyOpen.loading')}</p>
      )}
    </section>
  );
}

export function PreservedStoryDrafts({ account, projectId }) {
  const { t } = useI18n();
  if (!account || cloud.getState().account !== account) return null;
  const drafts = store.getPreservedCloudDrafts(account, projectId);
  return drafts.length ? (
    <button
      type="button"
      onClick={() => jsonDownload(drafts, projectId + '.preserved-drafts.json')}
    >
      {t('storyOpen.downloadKept')}
    </button>
  ) : null;
}
