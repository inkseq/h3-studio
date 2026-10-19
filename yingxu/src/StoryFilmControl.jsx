import React, { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { Clapperboard, Save } from 'lucide-react';
import { ChatDialog } from './QuickChatControls.jsx';
import { StoryboardVideo } from './StoryboardMedia.jsx';
import { useAppLocation, usePageActive } from './AppRouteCache.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import { download } from './media.js';
import {
  createFilmClient,
  createFilmController,
  filmLinkRun,
  filmReady,
  filmTerminal,
} from './story-film.js';
import { FilmRunView, filmProblem } from './StoryFilmView.jsx';
import './v2/story-film.css';

function browserStorage() {
  try {
    if (globalThis.localStorage) return globalThis.localStorage;
  } catch {
    /* Closed below. */
  }
  return {
    getItem() {
      throw Error('film.pendingCorrupt');
    },
    setItem() {
      throw Error('film.pendingCorrupt');
    },
    removeItem() {
      throw Error('film.pendingCorrupt');
    },
  };
}

export default function StoryFilmControl({
  cloudState,
  workspaceState,
  onSave,
  otherBusy = false,
}) {
  const { t } = useI18n(),
    active = usePageActive(),
    location = useAppLocation();
  const [open, setOpen] = useState(false),
    [confirmed, setConfirmed] = useState(false),
    [resolution, setResolution] = useState('720P'),
    [captions, setCaptions] = useState(false),
    [notice, setNotice] = useState(''),
    [readError, setReadError] = useState('');
  const live = useRef();
  live.current = {
    account: cloudState.account,
    projectId: workspaceState.project.id,
    epoch: workspaceState.workspaceEpoch,
    mode: workspaceState.workspace.mode,
    workspaceAccount: workspaceState.workspace.account,
    serverVersion: workspaceState.workspace.serverVersion,
    dirty: workspaceState.workspace.dirty,
    enabled: cloudState.capabilities?.story_film?.enabled,
    workflowVersion: cloudState.capabilities?.story_film?.workflow_version,
    externalBusy: cloudState.busy || otherBusy,
  };
  const controller = useMemo(
    () =>
      createFilmController({
        api: createFilmClient(),
        storage: browserStorage(),
        getContext: () => live.current,
      }),
    [],
  );
  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getState,
    controller.getState,
  );
  const { account, projectId, epoch, mode, workspaceAccount, serverVersion, dirty } = live.current;
  useEffect(() => {
    setOpen(false);
    setConfirmed(false);
    setNotice('');
    setReadError('');
    controller.activate(live.current);
    return () => controller.deactivate();
  }, [controller, account, projectId, epoch, mode, workspaceAccount]);
  useEffect(() => {
    setConfirmed(false);
  }, [state.run?.id, serverVersion, dirty, resolution, captions]);
  const linkedRun = filmLinkRun(location.href, projectId);
  useEffect(() => {
    if (!active || !account || mode !== 'cloud' || workspaceAccount !== account || !linkedRun)
      return;
    let stopped = false;
    setOpen(true);
    controller.openRun(linkedRun).catch((error) => {
      if (!stopped)
        setReadError(
          error?.detail?.code || error?.detail?.detail || error?.message || 'film.unknown',
        );
    });
    return () => {
      stopped = true;
    };
  }, [controller, active, account, projectId, epoch, mode, workspaceAccount, linkedRun]);
  useEffect(() => {
    if (!active || !account || mode !== 'cloud') return;
    let stopped = false,
      timer;
    const read = async () => {
      try {
        await controller.refresh();
        if (!stopped) setReadError('');
      } catch (error) {
        if (!stopped) setReadError(error?.detail?.code || error?.detail?.detail || 'film.unknown');
      }
      const current = controller.getState();
      if (
        !stopped &&
        current.run &&
        current.run.phase !== 'preflight' &&
        !filmTerminal(current.run) &&
        !current.pending
      )
        timer = setTimeout(read, 10000);
    };
    read();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [
    controller,
    active,
    account,
    projectId,
    epoch,
    mode,
    state.run?.id,
    state.run?.accepted_at,
    state.pending,
  ]);
  const scoped =
    state.context &&
    ['account', 'projectId', 'epoch', 'mode', 'workspaceAccount'].every(
      (key) => state.context[key] === live.current[key],
    );
  const view = scoped ? state : { run: null, pending: null, busy: false, error: '' };
  const run = view.run,
    ready = scoped && filmReady(live.current),
    externalBusy = cloudState.busy || otherBusy;
  const pending = !!view.pending,
    newAllowed =
      ready &&
      !externalBusy &&
      !view.busy &&
      !pending &&
      (!run || run.phase === 'preflight' || filmTerminal(run));
  const optionsMatch =
    !!run && run.options?.resolution === resolution && run.options?.burn_subtitles === captions;
  const call = (fn) => {
    const scope = live.current;
    setNotice('');
    setReadError('');
    return Promise.resolve()
      .then(fn)
      .catch((error) => {
        if (['account', 'projectId', 'epoch'].every((key) => scope[key] === live.current[key]))
          setReadError(
            error?.detail?.code || error?.detail?.detail || error?.message || 'film.unknown',
          );
      });
  };
  const completed = run?.phase === 'completed' && run.export?.artifact_id;
  const preview =
    completed && active ? (
      <StoryboardVideo
        key={account + ':' + epoch + ':' + run.export.artifact_id}
        asset={{
          kind: 'video',
          name: t('film.title'),
          fileId: 'cloud_artifact_' + run.export.artifact_id,
        }}
        controls
        aria-label={t('film.completedNotice')}
      />
    ) : null;
  return (
    <span className="story-film-control">
      <button
        type="button"
        disabled={!scoped || (!ready && !state.saved && !run && !pending)}
        title={mode !== 'cloud' ? t('film.local') : !ready ? t('film.unavailable') : undefined}
        onClick={() => setOpen(true)}
      >
        <Clapperboard size={15} />
        {run && run.phase !== 'preflight' ? t('film.view') : t('film.open')}
      </button>
      {completed && (
        <span className="film-complete-notice" role="status">
          {t('film.completedNotice')}
        </span>
      )}
      {open && scoped && (
        <ChatDialog
          title={t('film.title')}
          wide
          className="film-dialog"
          onClose={() => setOpen(false)}
        >
          <p>{t('film.intro')}</p>
          {!ready && <p role="status">{t('film.unavailable')}</p>}
          {dirty && !run?.accepted_at && (
            <p className="film-save">
              <span>{t('film.saveFirst')}</span>
              <button
                type="button"
                disabled={state.busy || externalBusy || pending}
                onClick={() => call(onSave)}
              >
                <Save size={14} />
                {t('film.save')}
              </button>
            </p>
          )}
          {(!run || run.phase === 'preflight' || filmTerminal(run)) && (
            <div className="film-options">
              <label>
                {t('film.resolution')}
                <select
                  value={resolution}
                  disabled={!newAllowed}
                  onChange={(event) => setResolution(event.target.value)}
                >
                  <option value="480P">480P</option>
                  <option value="720P">720P</option>
                </select>
              </label>
              <label className="film-check">
                <input
                  type="checkbox"
                  checked={captions}
                  disabled={!newAllowed}
                  onChange={(event) => setCaptions(event.target.checked)}
                />
                {t('film.captions')}
              </label>
              <small>{t('film.captionHint')}</small>
              <button
                type="button"
                disabled={!newAllowed || dirty || !Number.isInteger(serverVersion)}
                onClick={() =>
                  call(() => controller.preflight({ resolution, burn_subtitles: captions }))
                }
              >
                {t('film.preflight')}
              </button>
            </div>
          )}
          {(state.error || readError) && (
            <p role="alert">{t(filmProblem(state.error || readError))}</p>
          )}
          {state.pending && (
            <div className="film-pending" role="status">
              <p>{t('film.pending')}</p>
              <button
                type="button"
                disabled={state.busy}
                onClick={() => call(() => controller.recover())}
              >
                {t('film.recover')}
              </button>
            </div>
          )}
          <FilmRunView
            state={{ ...view, busy: view.busy || externalBusy }}
            t={t}
            dirty={dirty}
            currentVersion={serverVersion}
            optionsMatch={optionsMatch}
            confirmed={confirmed}
            onConfirmed={setConfirmed}
            onConfirm={() => call(() => controller.confirm(confirmed))}
            onRefresh={() => call(() => controller.refresh())}
            onRecover={() => call(() => controller.recover())}
            onResume={() => call(() => controller.change('resume'))}
            onCancel={() => call(() => controller.change('cancel'))}
            onDownload={() =>
              call(async () => {
                const blob = await controller.download();
                download(blob, 'inkseq-film-' + run.id.slice(-8) + '.mp4');
                setNotice(t('film.downloadNotice'));
              })
            }
            preview={preview}
          />
          {state.busy && <p role="status">{t('film.busy')}</p>}
          {notice && <p role="status">{notice}</p>}
        </ChatDialog>
      )}
    </span>
  );
}
