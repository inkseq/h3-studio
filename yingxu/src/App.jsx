import { ChatDialog } from './QuickChatControls.jsx';
import InkLoader from './InkLoader.jsx';
import { cloudSystemMessage } from './cloud-model.js';
import { exportSrt } from './caption-model.js';
import StudioHeader from './StudioHeader.jsx';
import AccountMenu, { MenuItem } from './AccountMenu.jsx';
import { LowCreditsDialog } from './CreditsUI.jsx';
import { LegalPage, PricingPage } from './PublicPages.jsx';
import Landing from './Landing.jsx';
import { CloudSaveStatus, useCloudAutosave } from './CloudAutosave.jsx';
import Library from './Library.jsx';
import UniversalTrash, {
  TrashProvider,
  useTrash,
  TrashMenu,
  StoryItemManager,
} from './UniversalTrash.jsx';
import { deletionImpact } from './entity-references.js';
import { storyboardUrl } from './storyboard.js';
import React, { useState, useEffect, useRef, useSyncExternalStore } from 'react';
import {
  Plus,
  LayoutGrid,
  Workflow,
  ChevronRight,
  ChevronLeft,
  Search,
  Undo2,
  Redo2,
  Download,
  Upload,
  X,
  Check,
  ArrowUp,
  ArrowDown,
  Copy,
  Trash2,
  Link2,
  FolderOpen,
  BookOpen,
  Users,
  MapPin,
  Image,
  Clapperboard,
  Settings2,
  HelpCircle,
  PanelRightClose,
  Bot,
  Activity,
} from 'lucide-react';
const ChapterRoughCut = React.lazy(() => import('./ChapterRoughCut.jsx'));
const Freestyle = React.lazy(() => import('./Freestyle.jsx'));
const StoryChat = React.lazy(() => import('./StoryChat.jsx'));
const QuickCreateEntry = React.lazy(() => import('./QuickCreateEntry.jsx'));
import OperatorConsole from './P4OperatorConsole.jsx';
import QuickChatWorkspace from './QuickChatWorkspace.jsx';
import { navigateWorkspace } from './workspace-navigation.js';
import { CloudProjects, useCloud } from './CloudStudio.jsx';
import AgentConnect from './AgentConnect.jsx';
import AgentLink from './AgentLink.jsx';
import ProjectActivity from './ProjectActivity.jsx';
import { locateEntity } from './agent-navigation.js';
import { cloudController as cloud } from './cloud-controller.js';
import StoryboardConnected from './StoryboardConnected.jsx';
import { StoryLibraryHome, RecentStoriesDropdown } from './StoryNavigation.jsx';
import { readAgentLink } from './agent-navigation.js';
import { readLocalStoryLibrary, archiveLocalStory } from './story-navigation.js';
import {
  store,
  typeLabels,
  roleLabels,
  children,
  ancestors,
  readiness,
  validateLink,
} from './store.js';
import {
  getFile,
  putFile,
  uploadFile,
  exportBundle,
  readBundle,
  download,
  findMissingFileIds,
} from './media.js';
import { createEntityWithMedia } from './media-operations.js';
import PersistenceWarning from './PersistenceWarning.jsx';
import { installPersistenceGuard } from './persistence-guard.js';
import SpendingSettings from './SpendingSettings.jsx';
import { I18nProvider, LanguageSwitcher, useI18n } from './i18n/I18nProvider.jsx';
import AppRouteCache, { useAppLocation, usePageActive } from './AppRouteCache.jsx';
import MaintenanceBanner from './MaintenanceBanner.jsx';
import { installAppNavigation, navigateApp } from './app-navigation.js';
import StoryOpenNotice, { PreservedStoryDrafts } from './StoryOpenNotice.jsx';
import { createStoryRouteLoader, openedStoryHref } from './story-open.js';

const icons = {
  chapter: BookOpen,
  scene: Clapperboard,
  shot: Clapperboard,
  character: Users,
  location: MapPin,
  image: Image,
  audio: Workflow,
  video: Clapperboard,
  note: BookOpen,
  generation: Workflow,
};
const mediaKinds = ['image', 'audio', 'video'];
const byOrder = (a, b) => a.order - b.order;
const find = (p, id) => p.entities.find((e) => e.id === id);
const getChapter = (p, e) => {
  let n = e;
  const visited = new Set();
  while (n && !visited.has(n.id)) {
    if (n.type === 'chapter') return n;
    visited.add(n.id);
    n = find(p, n.parentId);
  }
  return null;
};
const statuses = {
  draft: 'story.status.draft',
  review: 'story.status.review',
  ready: 'story.status.ready',
};
const readLocalLibrary = readLocalStoryLibrary;
function Icon({ type, size = 18 }) {
  const { t, locale } = useI18n();
  const I = icons[type] || BookOpen;
  return <I size={size} />;
}
function Field({ label, hint, children }) {
  const { t, locale } = useI18n();
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
function Modal({ title, children, onClose, wide = false }) {
  return (
    <ChatDialog title={title} onClose={onClose} wide={wide} className="story-dialog">
      {children}
    </ChatDialog>
  );
}
function SeriesApp({ sidebarView, onSidebarView }) {
  const { t, locale } = useI18n();
  const pageActive = usePageActive();
  useEffect(() => {
    if (pageActive)
      document.title = t(sidebarView === 'library' ? 'app.libraryTitle' : 'app.storyTitle');
  }, [sidebarView, t, pageActive]);
  useEffect(() => {
    if (!pageActive) return;
    return cloud.startPolling();
  }, [pageActive]);
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState),
    p = state.project;
  useCloudAutosave();
  const [modal, setModal] = useState(null),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [search, setSearch] = useState(''),
    [legacyGuide, setLegacyGuide] = useState(false);
  const c = useCloud(),
    trash = useTrash();
  const storyboardActive = true;
  const importRef = useRef(),
    selected = find(p, state.selectedId),
    chapter = find(p, state.scopeId);
  const mediaSignature = p.entities
    .filter((e) => mediaKinds.includes(e.type) && e.data.fileId)
    .map((e) => e.id + ':' + e.data.fileId + ':' + !!e.data.missingFile)
    .join('|');
  useEffect(() => {
    let alive = true,
      checking = false;
    const audit = async () => {
      if (checking) return;
      checking = true;
      try {
        const snapshot = store.getState().project;
        if (snapshot.id !== p.id) return;
        const missing = new Set(await findMissingFileIds(snapshot));
        if (!alive || store.getState().project.id !== snapshot.id) return;
        for (const e of snapshot.entities) {
          if (!mediaKinds.includes(e.type) || !e.data.fileId) continue;
          const current = find(store.getState().project, e.id);
          if (current?.data.fileId !== e.data.fileId) continue;
          const absent = missing.has(e.data.fileId);
          if (!!current.data.missingFile !== absent)
            store.updateEntity(
              e.id,
              { data: { missingFile: absent } },
              { history: false, observation: true },
            );
        }
      } catch {
        if (alive) store.notify(t('story.copy42'));
      } finally {
        checking = false;
      }
    };
    audit();
    window.addEventListener('focus', audit);
    return () => {
      alive = false;
      window.removeEventListener('focus', audit);
    };
  }, [p.id, mediaSignature]);
  const close = () => {
    setModal(null);
    setError('');
  };
  const open = (next) => {
    setError('');
    setModal(next);
  };
  const add = (type) => open({ type: 'add', kind: typeof type === 'string' ? type : 'chapter' });
  const inspect = (id) => {
    store.select(id);
    const live = store.getState().project,
      e = find(live, id),
      c = getChapter(live, e);
    if (c) store.setScope(c.id);
    if (
      e &&
      ['character', 'location', ...mediaKinds].includes(e.type) &&
      state.mode === 'guide' &&
      state.section !== 'journey'
    )
      store.setSection(
        e.type === 'character' ? 'characters' : e.type === 'location' ? 'locations' : 'assets',
      );
  };
  const showActivity = () => open({ type: 'activity' });
  const locate = (id) => {
    const target = locateEntity(store.getState().project, id);
    if (!target) return false;
    store.setMode('guide');
    store.setSection(target.section);
    store.setScope(target.chapterId);
    store.select(id);
    onSidebarView('story');
    return true;
  };
  const review = (id) => {
    const live = store.getState().project;
    if (!locate(id)) return;
    store.updateProject({ journey: { ...live.journey, stage: 5, reviewShotId: id } });
    store.setSection('journey');
  };
  const remove = (id) => {
    const e = find(p, id);
    if (e && state.workspace.mode === 'cloud')
      void trash?.request('trash', [{ kind: 'entity', project_id: p.id, id }]);
    else store.notify(t('trash.cloudOnly'));
  };
  useEffect(() => {
    if (!state.notice) return;
    const t = setTimeout(() => store.notify(''), 5500);
    return () => clearTimeout(t);
  }, [state.notice]);
  useEffect(() => {
    const handler = (e) => {
      const editing = /INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || e.target.isContentEditable;
      if (e.key === 'Escape' && !modal) store.select(null);
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setSearch('');
        open({ type: 'search' });
      }
      if (!editing && !modal && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
        e.preventDefault();
        e.shiftKey ? store.redo() : store.undo();
      }
      if (!editing && !modal && e.key === '?') open({ type: 'help' });
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [modal]);
  function archive() {
    archiveLocalStory(store);
  }
  async function backup() {
    setBusy(true);
    try {
      const { blob, missing } = await exportBundle(store.exportProject());
      download(blob, p.title + '.inkseq.zip');
      store.notify(
        missing.length ? t('story.backupMissing', { count: missing.length }) : t('story.copy43'),
      );
    } catch (e) {
      store.notify(t('story.copy44') + e.message);
    } finally {
      setBusy(false);
    }
  }
  async function importFile(file) {
    if (!file) return;
    setBusy(true);
    try {
      const pack = await readBundle(file);
      open({ type: 'import', pack });
    } catch (e) {
      store.notify(t('story.copy45') + e.message);
    } finally {
      setBusy(false);
      if (importRef.current) importRef.current.value = '';
    }
  }
  function openStoryboard() {
    setLegacyGuide(false);
    store.setMode('guide');
    if (c.storyboardEnabled) store.setSection('journey');
    store.select(null);
  }
  function focusChapter(id) {
    store.setScope(id);
    store.select(null);
    store.setSection(c.storyboardEnabled && !legacyGuide ? 'journey' : 'story');
  }
  const linkTarget = readAgentLink(window.location.search),
    linkHere =
      linkTarget &&
      state.workspace.mode === 'cloud' &&
      state.workspace.account === c.account &&
      state.project.id === linkTarget.projectId;
  const chapterList = p.entities.filter((e) => e.type === 'chapter').sort(byOrder);
  const accountMenu = (
    <AccountMenu
      account={c.account}
      onSignIn={() => open({ type: 'cloud' })}
      items={
        <>
          <MenuItem icon={Download} disabled={busy} onClick={backup}>
            {t('story.copy53')}
          </MenuItem>
          <MenuItem icon={Upload} onClick={() => open({ type: 'projects' })}>
            {t('story.copy54')}
          </MenuItem>
          <MenuItem icon={Bot} onClick={() => open({ type: 'agent' })}>
            {t('story.copy31')}
          </MenuItem>
          {state.workspace.mode === 'cloud' && (
            <>
              <MenuItem onClick={() => open({ type: 'manage-items' })}>
                {t('trash.manage')}
              </MenuItem>
              <TrashMenu
                target={{ kind: 'project', project_id: p.id, id: p.id }}
                onRename={() => open({ type: 'project-settings' })}
              />
            </>
          )}
        </>
      }
    />
  );
  return (
    <>
      <div className="home" hidden={sidebarView !== 'library'}>
        {/* The same account menu as every other page; the story actions (ZIP backup, local
            import, connect AI, trash) stay in the open story's own header below (#358). */}
        <StudioHeader account={c.account} active="works" onSignIn={() => open({ type: 'cloud' })} />
        <main id={sidebarView === 'library' ? 'workspace' : undefined} tabIndex={-1}>
          <StoryLibraryHome
            state={state}
            onOpenCloud={() => open({ type: 'cloud' })}
            onOpenLocal={() => open({ type: 'projects' })}
            onSelect={(selection) => {
              openStoryboard();
              onSidebarView('story', { selected: true, projectId: selection?.projectId });
            }}
          />
        </main>
      </div>
      <div className="workspace v2-story-view" hidden={sidebarView === 'library'}>
        <header className="wbar">
          <button className="ghost back story-home-back" onClick={() => onSidebarView('library')}>
            <ChevronLeft size={16} />
            {t('v2.works')}
          </button>
          <RecentStoriesDropdown
            state={state}
            onOpenCloud={() => open({ type: 'cloud' })}
            onOpenLocal={() => open({ type: 'projects' })}
            onSelect={(selection) => {
              openStoryboard();
              onSidebarView('story', { selected: true, projectId: selection?.projectId });
            }}
          />
          {/* Chapters are sections on the one storyboard; no chapter switcher (owner, 2026-10-21). */}
          {state.workspace.mode === 'cloud' ? (
            <details className="v2-cloud-status">
              <summary className="v2-autosave">
                <CloudSaveStatus plain />
              </summary>
              <div className="card-box">
                <button disabled={c.busy} onClick={() => void cloud.save().catch(() => {})}>
                  {t('account.copy64')}
                </button>
                <button disabled={c.busy} onClick={() => void cloud.syncAll().catch(() => {})}>
                  {t('account.copy65')}
                </button>
                <button
                  disabled={c.busy}
                  onClick={() => void cloud.checkRemoteVersion().catch(() => {})}
                >
                  {t('account.copy66')}
                </button>
                <button
                  disabled={c.busy}
                  onClick={() => void cloud.reloadCapabilities().catch(() => {})}
                >
                  {t('account.copy72')}
                </button>
                {c.remoteUpdate && (
                  <button disabled={c.busy} onClick={() => void cloud.open(p.id).catch(() => {})}>
                    {t('account.copy68')}
                  </button>
                )}
                {c.conflict && (
                  <>
                    <p role="alert">{t('account.copy69')}</p>
                    <button disabled={c.busy} onClick={() => void cloud.saveCopy().catch(() => {})}>
                      {t('account.copy70')}
                    </button>
                    <button
                      onClick={() =>
                        download(
                          new Blob([JSON.stringify(store.exportProject(), null, 2)], {
                            type: 'application/json',
                          }),
                          p.title + '.draft.json',
                        )
                      }
                    >
                      {t('account.copy71')}
                    </button>
                  </>
                )}
                {c.error && <p role="alert">{c.error}</p>}
                {c.message && <p role="status">{cloudSystemMessage(c.message, locale)}</p>}
                <PreservedStoryDrafts account={c.account} projectId={p.id} />
                <button
                  disabled={c.busy}
                  onClick={() => {
                    cloud.leave();
                    onSidebarView('story', { selected: true });
                  }}
                >
                  {t('account.copy67')}
                </button>
              </div>
            </details>
          ) : (
            <span className="badge local">{t('story.copy9')}</span>
          )}
          <div className="spacer" />
          <button
            className="ghost icon-only"
            disabled={!state.undoCount}
            onClick={() => store.undo()}
            aria-label={t('story.copy2')}
            title={t('story.copy2')}
          >
            <Undo2 size={15} />
          </button>
          <button
            className="ghost icon-only"
            disabled={!state.redoCount}
            onClick={() => store.redo()}
            aria-label={t('story.copy49')}
            title={t('story.copy49')}
          >
            <Redo2 size={15} />
          </button>
          <details className="v2-export-menu">
            <summary>
              {t('v2.export')} <ChevronRight size={12} />
            </summary>
            <div className="card-box">
              <button onClick={() => open({ type: 'roughcut' })}>
                <Clapperboard size={15} />
                <span>
                  {t('v2.roughCut')}
                  <small className="muted export-hint">{t('v2.roughCutHint')}</small>
                </span>
              </button>
              {(() => {
                // Subtitles are offered only when this chapter has some (owner, 2026-10-21).
                let srt = '';
                try {
                  srt = exportSrt(p, chapter?.id || chapterList[0]?.id);
                } catch {
                  srt = '';
                }
                return srt ? (
                  <button
                    onClick={() =>
                      download(new Blob([srt], { type: 'application/x-subrip' }), p.title + '.srt')
                    }
                  >
                    {t('v2.subtitles')}
                  </button>
                ) : null;
              })()}
              <button disabled={busy} onClick={backup}>
                {t('story.copy53')}
              </button>
            </div>
          </details>
          <LanguageSwitcher />
          <button
            className="ghost"
            title={t('story.copy31')}
            aria-label={t('story.copy31')}
            onClick={() => open({ type: 'agent' })}
          >
            <Bot size={15} />
            <span className="bar-label">{t('story.copy31')}</span>
          </button>
          <button
            className="ghost"
            title={t('settings.title')}
            aria-label={t('settings.title')}
            onClick={() => open({ type: 'project-settings' })}
          >
            <Settings2 size={15} />
            <span className="bar-label">{t('settings.title')}</span>
          </button>
          {accountMenu}
        </header>
        {store.getRecoveryRaw?.() && (
          <div role="alert">
            <span>{t('story.copy65')}</span>
            <button
              onClick={() =>
                download(
                  new Blob([store.getRecoveryRaw()], { type: 'text/plain' }),
                  'Inkseq-recovery.txt',
                )
              }
            >
              {t('story.copy66')}
            </button>
          </div>
        )}
        {sidebarView === 'story' &&
          (!linkHere ||
            linkTarget?.activity ||
            (linkTarget?.entityId && find(p, linkTarget.entityId)?.type !== 'shot')) && (
            <AgentLink
              state={state}
              onOpenCloud={() => open({ type: 'cloud' })}
              onLocate={locate}
              onActivity={showActivity}
            />
          )}
        {sidebarView === 'story' && (
          <StoryOpenNotice onOpened={() => onSidebarView('story', { selected: true })} />
        )}
        <StoryboardConnected
          key={c.account + ':' + p.id + ':' + state.workspaceEpoch}
          onNewScene={() => add('scene')}
        />
      </div>
      <input
        ref={importRef}
        className="visually-hidden"
        type="file"
        accept=".zip,.json"
        aria-label={t('story.copy69')}
        onChange={(e) => importFile(e.target.files[0])}
      />
      {state.notice && (
        <div className="toast" role="status" aria-live="polite">
          {cloudSystemMessage(state.notice, locale)}
        </div>
      )}
      {modal?.type === 'manage-items' && (
        <StoryItemManager
          state={state}
          onClose={close}
          onEdit={(id) => {
            close();
            setLegacyGuide(true);
            store.setMode('guide');
            store.setSection('assets');
            inspect(id);
          }}
        />
      )}
      {modal && modal.type !== 'manage-items' && (
        <Modal
          title={
            modal.type === 'roughcut'
              ? t('v2.roughCut')
              : modal.type === 'agent'
                ? t('story.copy70')
                : modal.type === 'cloud'
                  ? t('story.copy71')
                  : modal.type === 'add'
                    ? t('story.copy72')
                    : modal.type === 'search'
                      ? t('story.copy75')
                      : modal.type === 'help'
                        ? t('story.copy76')
                        : modal.type === 'import'
                          ? t('story.copy77')
                          : modal.type === 'project-settings'
                            ? t('story.copy78')
                            : t('story.copy80')
          }
          onClose={close}
          wide={['help', 'agent'].includes(modal.type)}
        >
          {modal.type === 'roughcut' && (
            <React.Suspense fallback={<InkLoader label={t('story.copy41')} />}>
              <ChapterRoughCut
                project={p}
                chapterId={chapter?.id || chapterList[0]?.id}
                onFix={() => {
                  close();
                  setLegacyGuide(true);
                  store.setSection('journey');
                }}
              />
            </React.Suspense>
          )}
          {modal.type === 'activity' && (
            <ProjectActivity
              state={state}
              onLocate={locate}
              onReview={review}
              onOpenCloud={() => open({ type: 'cloud' })}
              onConnect={() => open({ type: 'agent' })}
            />
          )}
          {modal.type === 'agent' && (
            <AgentConnect
              account={c.account}
              workspace={state.workspace}
              projectId={p.id}
              projectTitle={p.title}
              entityId={selected?.id}
              entityTitle={selected?.title}
              capabilities={c.capabilities}
              busy={c.busy}
              onOpenCloud={() => open({ type: 'cloud' })}
              onSaveCloud={() => cloud.save()}
              onManageKeys={(grant) => open({ type: 'cloud', grant })}
              onOpenActivity={() => {
                close();
                showActivity();
              }}
            />
          )}
          {modal.type === 'cloud' && (
            <CloudProjects
              initialGrant={modal.grant}
              onClose={() => {
                close();
                onSidebarView('story', { selected: true });
              }}
            />
          )}
          {modal.type === 'add' && (
            <AddForm
              project={p}
              state={state}
              initialKind={modal.kind}
              onDone={(id) => {
                close();
                const live = store.getState().project,
                  entity = find(live, id);
                if (
                  state.section === 'journey' &&
                  ['character', 'location'].includes(entity?.type)
                ) {
                  store.updateProject({
                    journey: {
                      ...live.journey,
                      ...(entity.type === 'character'
                        ? { focusCharacterId: id }
                        : { focusLocationId: id }),
                    },
                  });
                  store.select(null);
                  requestAnimationFrame(() =>
                    document
                      .querySelector(
                        entity.type === 'character' ? '.character-studio' : '.location-studio',
                      )
                      ?.scrollIntoView({ behavior: 'smooth', block: 'start' }),
                  );
                } else inspect(id);
              }}
            />
          )}

          {modal.type === 'search' && (
            <>
              <Field label={t('story.copy90')}>
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder={t('story.copy91')}
                />
              </Field>
              <div className="search-results">
                {p.entities
                  .filter((e) =>
                    (e.title + ' ' + e.description + ' ' + (e.data.script || ''))
                      .toLowerCase()
                      .includes(search.toLowerCase()),
                  )
                  .slice(0, 30)
                  .map((e) => (
                    <button
                      key={e.id}
                      onClick={() => {
                        inspect(e.id);
                        close();
                      }}
                    >
                      <Icon type={e.type} />
                      <span>
                        <b>{e.title}</b>
                        <small>
                          {t('story.type.' + e.type)} ·{' '}
                          {getChapter(p, e)?.title || t('story.copy92')}
                        </small>
                      </span>
                      <ChevronRight size={15} />
                    </button>
                  ))}
                {!p.entities.some((e) =>
                  (e.title + ' ' + e.description + ' ' + (e.data.script || ''))
                    .toLowerCase()
                    .includes(search.toLowerCase()),
                ) && <p>{t('story.copy93')}</p>}
              </div>
            </>
          )}
          {modal.type === 'project-settings' && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const f = new FormData(e.currentTarget),
                  title = String(f.get('title')).trim();
                if (!title) {
                  setError(t('story.copy94'));
                  return;
                }
                const next = { title, logline: String(f.get('logline')).trim() };
                if (store.updateProject(next) === false) {
                  setError(store.getState().notice);
                  return;
                }
                close();
              }}
            >
              <Field label={t('story.copy7')}>
                <input name="title" maxLength={80} required defaultValue={p.title} />
              </Field>
              <Field label={t('story.copy95')}>
                <textarea name="logline" rows={4} maxLength={2000} defaultValue={p.logline} />
              </Field>
              {error && (
                <p role="alert" className="inline-error">
                  {error}
                </p>
              )}
              <div className="modal-actions">
                <button type="button" onClick={close}>
                  {t('story.copy24')}
                </button>
                <button className="primary">{t('story.copy96')}</button>
              </div>
            </form>
          )}
          {modal.type === 'projects' && (
            <>
              <p className="muted">{t('story.copy97')}</p>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  const title = new FormData(e.currentTarget).get('title').trim();
                  if (!title) {
                    setError(t('story.copy94'));
                    return;
                  }
                  try {
                    archive();
                    if (store.newProject(title) === false) {
                      setError(store.getState().notice);
                      return;
                    }
                    store.setSection('journey');
                    store.setMode('guide');
                    close();
                    onSidebarView('story');
                    store.notify(t('story.copy98'));
                  } catch {
                    setError(t('story.copy99'));
                  }
                }}
              >
                <Field label={t('story.copy100')}>
                  <input name="title" maxLength={80} required placeholder={t('story.copy101')} />
                </Field>
                <button className="primary">
                  <Plus size={16} />
                  {t('story.copy102')}
                </button>
              </form>
              <div className="project-tools">
                <button
                  onClick={() => {
                    try {
                      archive();
                      if (store.loadDemo() === false) {
                        setError(store.getState().notice);
                        return;
                      }
                      store.setSection('story');
                      store.setMode('guide');
                      close();
                      onSidebarView('story');
                    } catch {
                      setError(t('story.copy103'));
                    }
                  }}
                >
                  {t('story.copy104')}
                </button>
                <button disabled={busy} onClick={() => importRef.current.click()}>
                  <Upload size={16} />
                  {t('story.copy105')}
                </button>
                <button disabled={busy} onClick={backup}>
                  <Download size={16} />
                  {t('story.copy106')}
                </button>
              </div>
              <button
                className="text-button"
                onClick={() =>
                  download(
                    new Blob([JSON.stringify(store.exportProject(), null, 2)], {
                      type: 'application/json',
                    }),
                    p.title + '.structure.json',
                  )
                }
              >
                <Download size={15} />
                {t('story.copy107')}
              </button>
              <h3>{t('story.copy108')}</h3>
              <ProjectList
                currentId={p.id}
                onSelect={(project) => {
                  try {
                    archive();
                    if (store.replaceProject(project) === false) {
                      setError(store.getState().notice);
                      return;
                    }
                    store.setSection('story');
                    close();
                    onSidebarView('story');
                  } catch {
                    setError(t('story.copy109'));
                  }
                }}
              />
              {error && (
                <p className="inline-error" role="alert">
                  {error}
                </p>
              )}
            </>
          )}
          {modal.type === 'import' && (
            <>
              <p>
                {t('story.importSummary', {
                  title: modal.pack.project.title,
                  items: modal.pack.project.entities.length,
                  files: modal.pack.files.length,
                })}
              </p>
              {modal.pack.missing.length > 0 && (
                <p className="inline-error">
                  {t('story.importMissing', { files: modal.pack.missing.join(', ') })}
                </p>
              )}
              {error && (
                <p className="inline-error" role="alert">
                  {error}
                </p>
              )}
              <div className="modal-actions">
                <button onClick={close}>{t('story.copy24')}</button>
                <button
                  className="primary"
                  disabled={busy}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      archive();
                      for (const [id, file] of modal.pack.files) await putFile(id, file);
                      const result = store.replaceProject(modal.pack.project);
                      if (result === false) throw Error(t('story.copy110'));
                      close();
                      onSidebarView('story');
                      store.notify(t('story.copy111'));
                    } catch (e) {
                      setError(e.message);
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  {t('story.copy112')}
                </button>
              </div>
            </>
          )}
          {modal.type === 'help' && (
            <>
              <div className="help-grid">
                <section>
                  <h3>{t('story.copy113')}</h3>
                  <ol>
                    <li>{t('story.copy114')}</li>
                    <li>{t('story.copy115')}</li>
                    <li>{t('story.copy116')}</li>
                    <li>{t('story.copy117')}</li>
                  </ol>
                </section>
                <section>
                  <h3>{t('story.copy118')}</h3>
                  <p>{t('story.copy119')}</p>
                  <p>{t('story.copy120')}</p>
                </section>
              </div>
              <div className="impact-box">
                {t('story.copy121')}
                <br />
                {t('story.copy122')}
              </div>
              <p>{t('story.helpCapabilities')}</p>
              <p>{t('story.copy123')}</p>
              <button className="text-link" onClick={() => open({ type: 'cloud' })}>
                {t('story.copy124')}
              </button>
            </>
          )}
        </Modal>
      )}
    </>
  );
}

function descendants(p, id) {
  const result = [];
  const visit = (x) =>
    p.entities
      .filter((e) => e.parentId === x)
      .forEach((e) => {
        if (!result.includes(e.id)) {
          result.push(e.id);
          visit(e.id);
        }
      });
  visit(id);
  return result;
}
function ProjectList({ currentId, onSelect }) {
  const { t, locale } = useI18n();
  const { projects, raw, error } = readLocalLibrary();
  return (
    <div className="project-list">
      {error && (
        <div role="alert" className="inline-error">
          <p>{t('story.copy125')}</p>
          <button
            onClick={() =>
              download(new Blob([raw], { type: 'text/plain' }), '映序-项目列表恢复.txt')
            }
          >
            {t('story.copy126')}
          </button>
        </div>
      )}
      {projects
        .filter((x) => x.id !== currentId)
        .map((p) => (
          <button key={p.id} onClick={() => onSelect(p)}>
            <FolderOpen size={17} />
            <span>
              {p.title}
              <small>{t('story.itemCount', { count: p.entities.length })}</small>
            </span>
            <ChevronRight size={16} />
          </button>
        ))}
      {!error && !projects.some((x) => x.id !== currentId) && (
        <p className="muted">{t('story.copy127')}</p>
      )}
    </div>
  );
}

function AddForm({ project: p, state, initialKind, onDone }) {
  const { t, locale } = useI18n();
  const [kind, setKind] = useState(initialKind || 'chapter'),
    [title, setTitle] = useState(''),
    [parent, setParent] = useState(''),
    [error, setError] = useState(''),
    [file, setFile] = useState(null),
    [busy, setBusy] = useState(false),
    input = useRef(),
    selected = find(p, state.selectedId);
  const possible = p.entities.filter((e) =>
      kind === 'scene'
        ? e.type === 'chapter'
        : kind === 'shot'
          ? e.type === 'scene'
          : ['chapter', 'scene', 'shot'].includes(e.type),
    ),
    hasUpload = ['character', 'location', ...mediaKinds].includes(kind),
    expected = ['character', 'location'].includes(kind) ? 'image' : kind;
  useEffect(() => {
    let id = '';
    if (kind === 'scene')
      id = state.scopeId || p.entities.find((e) => e.type === 'chapter')?.id || '';
    else if (kind === 'shot')
      id =
        (selected?.type === 'scene'
          ? selected.id
          : selected?.type === 'shot'
            ? selected.parentId
            : null) ||
        p.entities.find(
          (e) => e.type === 'scene' && (!state.scopeId || e.parentId === state.scopeId),
        )?.id ||
        '';
    else if (['generation', 'note'].includes(kind))
      id =
        selected && ['scene', 'shot'].includes(selected.type) ? selected.id : state.scopeId || '';
    setParent(id);
    setFile(null);
  }, [kind, state.selectedId, state.scopeId]);
  const descriptions = {
    chapter: t('story.copy128'),
    scene: t('story.copy129'),
    shot: t('story.copy130'),
    character: t('story.copy131'),
    location: t('story.copy132'),
    image: t('story.copy133'),
    video: t('story.copy134'),
    audio: t('story.copy135'),
    note: t('story.copy136'),
    generation: t('story.copy137'),
  };
  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    const name = title.trim() || file?.name || '';
    if (!name) {
      setError(t('story.copy138'));
      return;
    }
    if (['scene', 'shot'].includes(kind) && !parent) {
      setError(kind === 'scene' ? t('story.copy139') : t('story.copy140'));
      return;
    }
    setBusy(true);
    setError('');
    try {
      const id = file
        ? await createEntityWithMedia({ store, kind, title: name, file, upload: uploadFile })
        : store.addEntity(kind, parent || null, { title: name });
      if (id) onDone(id);
      else setError(store.getState().notice || t('story.copy141'));
    } catch (error) {
      setError(error.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit}>
      {initialKind !== 'scene' && (
        <div className="node-palette" role="group" aria-label={t('story.copy142')}>
          {Object.keys(typeLabels)
            .map((key) => [key, t('story.type.' + key)])
            .map(([key, label]) => (
              <button
                type="button"
                key={key}
                disabled={busy}
                aria-pressed={kind === key}
                onClick={() => {
                  setKind(key);
                  setError('');
                }}
              >
                <Icon type={key} />
                <span>{label}</span>
              </button>
            ))}
        </div>
      )}
      <p className="muted">{descriptions[kind]}</p>
      <Field label={t('story.nameOf', { type: t('story.type.' + kind) })}>
        <input
          value={title}
          disabled={busy}
          onChange={(event) => setTitle(event.target.value)}
          placeholder={file?.name || t('story.copy144')}
          maxLength={100}
        />
      </Field>
      {!['chapter', 'character', 'location', ...mediaKinds].includes(kind) && (
        <Field label={t('story.copy145')}>
          <select
            value={parent}
            disabled={busy}
            onChange={(event) => setParent(event.target.value)}
          >
            <option value="">
              {['scene', 'shot'].includes(kind) ? t('story.copy146') : t('story.copy92')}
            </option>
            {possible.map((entity) => (
              <option value={entity.id} key={entity.id}>
                {t('story.type.' + entity.type)} · {entity.title}
              </option>
            ))}
          </select>
        </Field>
      )}
      {hasUpload && (
        <section
          className="add-form-upload"
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            if (!busy) setFile(event.dataTransfer.files?.[0] || null);
          }}
        >
          <input
            ref={input}
            type="file"
            accept={expected + '/*'}
            className="visually-hidden"
            aria-label={t('story.copy147') + t('story.type.' + kind) + t('story.copy148')}
            disabled={busy}
            onChange={(event) => setFile(event.target.files?.[0] || null)}
          />
          <button type="button" disabled={busy} onClick={() => input.current.click()}>
            <Upload size={18} />
            {file
              ? t('story.copy149')
              : t('story.copy150') +
                {
                  image: t('story.type.image'),
                  video: t('story.type.video'),
                  audio: t('story.type.audio'),
                }[expected] +
                t('story.copy151')}
          </button>
          <p>
            {file
              ? file.name + ' · ' + (file.size / 1024 / 1024).toFixed(2) + ' MB'
              : mediaKinds.includes(kind)
                ? t('story.copy152')
                : t('story.copy153')}
          </p>
          {file && (
            <button type="button" disabled={busy} onClick={() => setFile(null)}>
              {t('story.copy154')}
            </button>
          )}
        </section>
      )}
      {error && (
        <p role="alert" className="inline-error">
          {error}
        </p>
      )}
      <div className="modal-actions">
        <button className="primary" disabled={busy}>
          <Plus size={16} />
          {busy
            ? t('story.copy155')
            : t('story.copy156') + t('story.type.' + kind) + (file ? t('story.copy157') : '')}
        </button>
      </div>
    </form>
  );
}

const storyPageFromUrl = (search = window.location.search) =>
  readAgentLink(search) || new URLSearchParams(search).get('view') === 'workspace'
    ? 'story'
    : 'library';
function AppContent() {
  const { t, locale } = useI18n(),
    c = useCloud(),
    account = c.account;
  useEffect(() => installPersistenceGuard(store), []);
  const workspace = useSyncExternalStore(store.subscribe, store.getState, store.getState),
    workspaceKey = [
      workspace.workspace.mode,
      workspace.workspace.account || '',
      workspace.project.id,
      workspace.workspaceEpoch,
    ].join(':');
  const location = useAppLocation(),
    path = location.path,
    sidebarView = storyPageFromUrl(location.search);
  const target = readAgentLink(location.search),
    routeLoader = useRef(null);
  if (!routeLoader.current) routeLoader.current = createStoryRouteLoader({ cloud, store });
  useEffect(() => {
    void routeLoader.current.update({
      account,
      projectId: target?.projectId,
      active: path === '/',
      busy: c.busy || !c.sessionReady,
    });
  }, [account, path, location.search, c.busy, c.sessionReady, workspaceKey]);
  useEffect(() => () => routeLoader.current.destroy(), []);
  useEffect(() => installAppNavigation(), []);
  useEffect(() => {
    void cloud.initialize();
  }, []);
  useEffect(() => {
    const opened = (event) => {
      const href = openedStoryHref(
        window.location.href,
        event.detail,
        store.getState(),
        cloud.getState().account,
      );
      if (href) navigateApp(href, { restore: false });
    };
    window.addEventListener('inkseq-cloud-project-opened', opened);
    return () => window.removeEventListener('inkseq-cloud-project-opened', opened);
  }, []);
  const changePage = (next, { selected = false, projectId } = {}) => {
    const url = new URL(window.location.href);
    if (next === 'library') {
      for (const key of ['view', 'project', 'entity', 'panel']) url.searchParams.delete(key);
    } else {
      url.searchParams.set('view', 'workspace');
      if (selected) {
        const live = store.getState();
        url.searchParams.delete('entity');
        url.searchParams.delete('panel');
        if (projectId) url.searchParams.set('project', projectId);
        else if (live.workspace.mode === 'cloud') url.searchParams.set('project', live.project.id);
        else url.searchParams.delete('project');
      }
    }
    navigateApp('/' + url.search, { restore: false });
  };
  const pageFocusMounted = useRef(false);
  useEffect(() => {
    if (!pageFocusMounted.current) {
      pageFocusMounted.current = true;
      return;
    }
    if (path === '/')
      document
        .querySelector(sidebarView === 'library' ? '#story-library-heading' : '.story-home-back')
        ?.focus({ preventScroll: true });
  }, [path, sidebarView]);
  const routes = {
    '/': ({ location: page }) => {
      // Creating needs an account: signed-out visitors see the landing page.
      if (!account) return c.sessionReady ? <Landing /> : <InkLoader label={t('app.name')} />;
      const destination = readAgentLink(page.search),
        here =
          destination &&
          account &&
          workspace.workspace.mode === 'cloud' &&
          workspace.workspace.account === account &&
          workspace.project.id === destination.projectId;
      return destination && !here ? (
        <div className="home story-open-destination">
          <StudioHeader account={account} active="works" />
          <main>
            <button type="button" className="ghost" onClick={() => changePage('library')}>
              {t('storyOpen.back')}
            </button>
            <StoryOpenNotice
              projectId={destination.projectId}
              waiting
              onClose={() => changePage('library')}
            />
          </main>
        </div>
      ) : (
        <SeriesApp
          key={workspaceKey}
          sidebarView={storyPageFromUrl(page.search)}
          onSidebarView={changePage}
        />
      );
    },
    '/library': ({ active }) => (
      <Library key={account || 'signed-out'} account={account} active={active} />
    ),
    '/trash': ({ active }) => <UniversalTrash active={active} />,
    '/pricing': () => <PricingPage account={account} />,
    '/connect/telegram': ({ active }) => (
      <React.Suspense fallback={<InkLoader label={t('telegram.pageTitle')} />}>
        <ConnectTelegramPage account={account} active={active} />
      </React.Suspense>
    ),
    '/terms': () => <LegalPage account={account} doc="terms" />,
    '/privacy': () => <LegalPage account={account} doc="privacy" />,
    '/content': () => <LegalPage account={account} doc="content" />,
    '/settings': ({ active }) => (
      <SpendingSettings key={account || 'signed-out'} account={account} active={active} />
    ),
    '/operator': ({ active }) => (
      <React.Suspense fallback={<InkLoader label={t('loading.operator')} />}>
        <OperatorConsole active={active} />
      </React.Suspense>
    ),
    '/quick-chat': ({ active, location: page }) => (
      <React.Suspense fallback={<InkLoader label={t('loading.quickCreate')} />}>
        {/* ?story=preview: sample replies. ?mode=direct: the original Quick Create.
            Otherwise the chat story builder when the server has it on (#422). */}
        {(() => {
          const params = new URLSearchParams(page.search);
          const story = params.get('story');
          if (story === 'preview') return <StoryChat account={account} />;
          // Links into the original Quick Create (a conversation, card, revision, submission
          // or its preferences) keep opening it; the story chat would show an empty story
          // instead (h3-studio#477).
          if (
            params.get('mode') === 'direct' ||
            ['session', 'card', 'revision', 'submission', 'preferences'].some((key) =>
              params.has(key),
            )
          )
            return <QuickChatWorkspace active={active} />;
          return (
            <QuickCreateEntry
              account={account}
              story={story}
              direct={<QuickChatWorkspace active={active} />}
            />
          );
        })()}
      </React.Suspense>
    ),
    '/freestyle': ({ active }) => (
      <React.Suspense
        fallback={
          <div className="home">
            <StudioHeader account={account} />
          </div>
        }
      >
        <Freestyle key={workspaceKey} active={active} />
      </React.Suspense>
    ),
  };
  return (
    <div className="app">
      <TrashProvider account={account}>
        <PersistenceWarning key={'persistence:' + workspaceKey} />
        <MaintenanceBanner />
        <AppRouteCache location={location} routes={routes} />
        <LowCreditsDialog />
      </TrashProvider>
    </div>
  );
}

const ConnectTelegramPage = React.lazy(() => import('./ConnectTelegramPage.jsx'));
export default function App() {
  const account = useCloud().account;
  return (
    <I18nProvider account={account || ''}>
      <AppContent />
    </I18nProvider>
  );
}
