import { WorkbenchBulk, WorkbenchFork } from './StoryboardWorkbench.jsx';
import './v2/story-view.css';
import { selectShot, emptySelection } from './storyboard-selection.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import React, { useEffect, useRef, useState } from 'react';
import {
  Plus,
  PanelLeftClose,
  PanelLeftOpen,
  Trash2,
  Maximize2,
  Minimize2,
  X,
  Scissors,
  ChevronDown,
  ChevronRight,
} from 'lucide-react';
import { RenameDialog, useTrash } from './UniversalTrash.jsx';
import ItemMenu from './ItemMenu.jsx';
import { usePageActive, useAppLocation } from './AppRouteCache.jsx';
import { navigateApp } from './app-navigation.js';
import { store } from './store.js';
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  KeyboardSensor,
  closestCenter,
  pointerWithin,
  useSensor,
  useSensors,
} from '@dnd-kit/core';
import {
  SortableContext,
  rectSortingStrategy,
  sortableKeyboardCoordinates,
} from '@dnd-kit/sortable';
import { StoryboardUIProvider, useStoryboardUI } from './storyboard-ui-context.jsx';
import {
  AssetLibrary,
  AssetThumb,
  ProfileEditor,
  ShotCard,
  ShotEditor,
  Screen,
  Timeline,
  Transport,
  usePlayer,
} from './StoryboardComponents.jsx';
import { StoryboardSplitter, useStoryboardPanelSize } from './StoryboardPanels.jsx';
import {
  coverOf,
  fmt,
  planDrop,
  totalDuration,
  variantOf,
  storyboardDockLayout,
} from './storyboard-ui-model.js';

function collision(args) {
  const id = String(args.active.id);
  if (id.startsWith('asset:') || id.startsWith('profile:')) {
    const hits = pointerWithin(args).filter(
      (hit) => !(id.startsWith('profile:') && /^(pdrop|vdrop|voicedrop):/.test(String(hit.id))),
    );
    const best = hits.find((hit) => /^(slot|vdrop|voicedrop):/.test(String(hit.id)));
    return best ? [best] : hits;
  }
  return closestCenter({
    ...args,
    droppableContainers: args.droppableContainers.filter((container) =>
      String(container.id).startsWith('shot:'),
    ),
  });
}
function QueueSummary() {
  const { t, locale } = useI18n();
  const { state } = useStoryboardUI(),
    queues = state.queue;
  const rows = ['FL', 'REF']
    .map((mode) => {
      const row = Array.isArray(queues)
        ? queues.find((item) => String(item.mode).toUpperCase() === mode)
        : queues?.[mode];
      const versions = state.shots
        .filter((shot) => shot.mode === mode)
        .flatMap((shot) => shot.versions);
      const waiting = versions.filter((version) =>
        ['queued', 'waiting_capacity', 'claimed', 'submitting', 'submission_unknown'].includes(
          version.status,
        ),
      ).length;
      const running = versions.filter((version) =>
        ['generating', 'running', 'collecting', 'checking', 'cancel_requested'].includes(
          version.status,
        ),
      ).length;
      const observed = row?.waiting ?? row?.waiting_jobs ?? row?.queued;
      return {
        mode,
        row,
        waiting: Math.max(Number.isFinite(observed) ? observed : 0, waiting),
        running,
      };
    })
    .filter((row) => row.waiting > 0 || row.running > 0);
  if (!rows.length) return null;
  return (
    <div className="sb-queue" aria-label={t('storyboard.copy14')}>
      {rows.map(({ mode, row, waiting, running }) => (
        <span key={mode}>
          {mode === 'FL' ? t('storyboard.copy15') : t('storyboard.copy16')}：
          {t('storyboard.copy34', { count: waiting })}
          {running > 0 && t('storyboard.copy32', { count: running })}
          {waiting > 0 &&
            Number.isFinite(row?.roughWaitSeconds) &&
            row.roughWaitSeconds > 0 &&
            t('storyboard.copy33', { minutes: Math.ceil(row.roughWaitSeconds / 60) })}
        </span>
      ))}
    </div>
  );
}
const FilmEditor = React.lazy(() => import('./FilmEditor.jsx'));
// Remotion loads only when the editor opens, keeping it out of the storyboard bundle.
// Chapter divider on the continuous storyboard: title, totals, fold, edit film and a menu.
function ChapterHeader({ chapter, index, count, shots, folded, onFold, onNewScene }) {
  const { t } = useI18n();
  const { state, dispatch } = useStoryboardUI();
  const trash = useTrash();
  const [renaming, setRenaming] = useState(false);
  const locked = state.busy || !!state.workbenchPending;
  const busyShots = shots.some((shot) =>
    shot.versions.some((v) => !['done', 'failed', 'cancelled'].includes(v.status)),
  );
  const canDelete =
    count > 1 &&
    !busyShots &&
    state.workspaceMode === 'cloud' &&
    !!trash?.account &&
    !trash.busy &&
    !trash.pending;
  return (
    <header className="sb-chapter-head">
      <button
        type="button"
        className="ghost small sb-chapter-fold"
        aria-expanded={!folded}
        aria-label={t(folded ? 'chapter.unfold' : 'chapter.fold')}
        onClick={onFold}
      >
        {folded ? <ChevronRight size={16} /> : <ChevronDown size={16} />}
      </button>
      <h2>
        <small>{t('chapter.number', { n: index + 1 })}</small>
        {chapter.title}
      </h2>
      <span className="muted sb-chapter-meta">
        {t('chapter.meta', { count: shots.length, duration: fmt(totalDuration(shots)) })}
      </span>
      <div className="spacer" />
      <FilmEditorButton chapterId={chapter.id} />
      <ItemMenu label={t('chapter.menu')} disabled={locked}>
        {(close) => (
          <>
            <button role="menuitem" onClick={() => (close(), setRenaming(true))}>
              {t('chapter.rename')}
            </button>
            {onNewScene && (
              <button
                role="menuitem"
                onClick={() => {
                  close();
                  store.setScope(chapter.id);
                  onNewScene();
                }}
              >
                {t('chapter.addScene')}
              </button>
            )}
            <button
              role="menuitem"
              disabled={index === 0}
              onClick={() => (close(), dispatch({ type: 'moveChapter', id: chapter.id, dir: -1 }))}
            >
              {t('chapter.moveUp')}
            </button>
            <button
              role="menuitem"
              disabled={index === count - 1}
              onClick={() => (close(), dispatch({ type: 'moveChapter', id: chapter.id, dir: 1 }))}
            >
              {t('chapter.moveDown')}
            </button>
            <button
              role="menuitem"
              className="danger-text"
              disabled={!canDelete}
              title={
                count <= 1
                  ? t('chapter.lastChapter')
                  : busyShots
                    ? t('chapter.busy')
                    : state.workspaceMode !== 'cloud'
                      ? t('chapter.cloudOnly')
                      : undefined
              }
              onClick={() => {
                close();
                trash.request('trash', [
                  { kind: 'entity', project_id: state.project.id, id: chapter.id },
                ]);
              }}
            >
              {t('chapter.delete')}
            </button>
          </>
        )}
      </ItemMenu>
      {renaming && (
        <RenameDialog
          title={chapter.title}
          onClose={() => setRenaming(false)}
          onSave={async (title) => {
            if (title.trim() === chapter.title) return;
            const result = await dispatch({ type: 'renameChapter', id: chapter.id, title });
            if (result === false || result?.ok === false) throw Error('chapter.renameFailed');
          }}
        />
      )}
    </header>
  );
}

function AddChapter({ count }) {
  const { t } = useI18n();
  const { state, dispatch } = useStoryboardUI();
  return (
    <button
      type="button"
      className="ghost sb-add-chapter"
      disabled={state.busy || !!state.workbenchPending}
      onClick={() =>
        dispatch({
          type: 'addChapter',
          title: t('chapter.newTitle', { n: count + 1 }),
          sceneTitle: t('storyboard.newScene'),
        })
      }
    >
      <Plus size={14} />
      {t('chapter.add')}
    </button>
  );
}

function FilmEditorButton({ chapterId }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  if (!chapterId) return null;
  return (
    <>
      <button onClick={() => setOpen(true)}>
        <Scissors size={14} />
        {t('editor.open')}
      </button>
      {open && (
        <React.Suspense fallback={<p role="status">{t('settings.loading')}</p>}>
          <FilmEditor chapterId={chapterId} onClose={() => setOpen(false)} />
        </React.Suspense>
      )}
    </>
  );
}

function StoryboardLayoutA({ selected, setSelected, pickAsset, onNewScene, filmControl }) {
  const { t, locale } = useI18n();
  const { state, dispatch } = useStoryboardUI(),
    player = usePlayer(state.shots),
    [selection, setSelection] = useState(emptySelection),
    [expanded, setExpanded] = useState(false),
    [libraryCollapsed, setLibraryCollapsed] = useState(
      () => globalThis.matchMedia?.('(max-width:760px)').matches ?? false,
    );
  const trash = useTrash(),
    pageActive = usePageActive(),
    column = useRef(null),
    main = useRef(null),
    [room, setRoom] = useState({ height: 0, content: 0 }),
    [theater, setTheater] = useState(false);
  const [folded, setFolded] = useState(() => new Set());
  useEffect(() => {
    const measure = () => {
      const col = column.current,
        body = main.current;
      if (!col || !body) return;
      const last = body.lastElementChild,
        height = col.clientHeight,
        content = last
          ? last.getBoundingClientRect().bottom -
            body.getBoundingClientRect().top +
            body.scrollTop +
            16
          : 0;
      setRoom((old) =>
        Math.abs(old.height - height) < 2 && Math.abs(old.content - content) < 2
          ? old
          : { height, content },
      );
    };
    measure();
    if (!globalThis.ResizeObserver) return;
    const observer = new ResizeObserver(measure);
    if (column.current) observer.observe(column.current);
    if (main.current) {
      observer.observe(main.current);
      [...main.current.children].forEach((child) => observer.observe(child));
    }
    return () => observer.disconnect();
  });
  useEffect(() => {
    if (!pageActive) setTheater(false);
  }, [pageActive]);
  useEffect(() => {
    if (theater)
      column.current?.parentElement
        ?.querySelectorAll('.drawer video,.drawer audio')
        .forEach((media) => media.pause());
  }, [theater]);
  useEffect(() => {
    if (!theater) return;
    const token = crypto.randomUUID(),
      key = 'inkseqStoryTheater';
    history.pushState({ ...history.state, [key]: token }, '', window.location.href);
    const pop = () => {
      if (history.state?.[key] !== token) setTheater(false);
    };
    window.addEventListener('popstate', pop);
    return () => {
      window.removeEventListener('popstate', pop);
      if (history.state?.[key] === token) history.back();
    };
  }, [theater]);
  useEffect(() => {
    const media = globalThis.matchMedia?.('(max-width:760px)');
    if (!media) return;
    const change = () => setLibraryCollapsed(media.matches);
    media.addEventListener('change', change);
    return () => media.removeEventListener('change', change);
  }, []);
  useEffect(() => {
    if (libraryCollapsed || theater) return;
    const close = (event) => {
      if (event.key === 'Escape') setLibraryCollapsed(true);
    };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [libraryCollapsed, theater]);
  useEffect(() => {
    setExpanded(false);
  }, [selected]);
  useEffect(() => {
    const close = (event) => {
      if (event.key !== 'Escape' || document.querySelector('dialog[open]')) return;
      if (theater) {
        event.preventDefault();
        event.stopImmediatePropagation();
        setTheater(false);
      } else if (expanded) {
        event.preventDefault();
        event.stopImmediatePropagation();
        setExpanded(false);
      } else if (selected) setSelected(null);
    };
    window.addEventListener('keydown', close, true);
    return () => window.removeEventListener('keydown', close, true);
  }, [theater, expanded, selected]);
  useEffect(() => {
    setSelection(emptySelection());
  }, [state.contextId]);
  useEffect(() => {
    setSelection((old) => ({
      ...old,
      ids: old.ids.filter((id) => state.shots.some((s) => s.id === id)),
    }));
  }, [state.shots.map((s) => s.id).join('|')]);
  useEffect(() => {
    const clear = (e) => {
      if (
        e.key === 'Escape' &&
        !theater &&
        !expanded &&
        !selected &&
        !document.querySelector('dialog[open]')
      )
        setSelection(emptySelection());
    };
    window.addEventListener('keydown', clear);
    return () => window.removeEventListener('keydown', clear);
  }, [theater, expanded, selected]);
  const library = useStoryboardPanelSize('library', 260, 140, 560),
    dock = useStoryboardPanelSize('dock', 190, 110, 900),
    drawer = useStoryboardPanelSize('drawer', 440, 340, 820),
    dockLayout = storyboardDockLayout({ ...room, manual: dock.custom ? dock.size : null });
  const toggleDock = () =>
    dock.set(
      dockLayout.size > (dockLayout.compact + dockLayout.large) / 2
        ? dockLayout.compact
        : dockLayout.large,
    );
  // One continuous storyboard: every chapter's scenes in reading order (owner, 2026-10-21).
  const chapters = state.project.chapters || [],
    multi = chapters.length > 1,
    chapterIndex = new Map(chapters.map((chapter, index) => [chapter.id, index]));
  const groups = (state.scenes || [])
    .filter((scene) => chapterIndex.has(scene.parentId))
    .sort(
      (a, b) => chapterIndex.get(a.parentId) - chapterIndex.get(b.parentId) || a.order - b.order,
    )
    .map((scene) => ({
      id: scene.id,
      title: scene.title,
      chapterId: scene.parentId,
      shots: state.shots.filter((shot) => shot.entity?.parentId === scene.id),
    }));
  for (const shot of state.shots) {
    if (!groups.some((group) => group.id === shot.entity?.parentId)) {
      const id = shot.entity?.parentId || null;
      let group = groups.find((row) => row.id === id);
      if (!group) {
        group = { id, title: state.project.chapter, shots: [] };
        groups.push(group);
      }
      group.shots.push(shot);
    }
  }
  const count = state.shots.filter((shot) => shot.chosen).length,
    pending = state.shots.filter((shot) => !shot.versions.length);
  return (
    <div
      className={'layout A' + (libraryCollapsed ? ' library-collapsed' : '')}
      data-testid="storyboard-layout-a"
      style={{
        gridTemplateColumns: libraryCollapsed
          ? '44px minmax(0,1fr)'
          : `${library.size}px 8px minmax(0,1fr)`,
        '--libw': (libraryCollapsed ? 44 : library.size) + 'px',
      }}
    >
      {!libraryCollapsed && (
        <button
          type="button"
          className="sb-library-backdrop"
          aria-label={t('storyboard.detail15')}
          onClick={() => setLibraryCollapsed(true)}
        />
      )}
      <AssetLibrary
        onPick={pickAsset}
        selectedShot={state.shots.find((shot) => shot.id === selected)}
        collapsed={libraryCollapsed}
        onToggle={() => setLibraryCollapsed((value) => !value)}
      />
      {!libraryCollapsed && <StoryboardSplitter panel={library} label={t('storyboard.copy17')} />}
      <div className="col" ref={column}>
        <main className="main" ref={main}>
          <div className="chapter-head">
            <div className="row tight chapter-summary">
              <button
                className="small ghost"
                title={t(libraryCollapsed ? 'storyboard.detail14' : 'storyboard.detail15')}
                onClick={() => setLibraryCollapsed((v) => !v)}
              >
                {libraryCollapsed ? (
                  <>
                    <PanelLeftOpen size={16} />
                    {t('library.title')}
                  </>
                ) : (
                  <PanelLeftClose size={16} />
                )}
              </button>
              <h1>{multi ? state.project.title : state.project.chapter}</h1>
              <small className="muted">
                {t(state.shots.length === 1 ? 'storyboard.oneShotSummary' : 'storyboard.copy36', {
                  shots: state.shots.length,
                  duration: fmt(totalDuration(state.shots)),
                  chosen: count,
                  aspect: state.project.aspect,
                })}
              </small>
            </div>
            <div className="row tight chapter-actions">
              {!multi && <FilmEditorButton chapterId={state.project.chapterId} />}
              {filmControl}
              <QueueSummary />
              {!groups.length && (
                <button
                  disabled={state.busy || !!state.workbenchPending}
                  onClick={() =>
                    dispatch({
                      type: 'add',
                      title: t('storyboard.newShot'),
                      sceneTitle: t('storyboard.newScene'),
                    })
                  }
                >
                  <Plus size={14} />
                  {t('storyboard.addShot')}
                </button>
              )}
              {onNewScene && (
                <button onClick={onNewScene}>
                  <Plus size={14} />
                  {t('v2.newScene')}
                </button>
              )}
              <button
                className="primary"
                disabled={state.busy || !pending.length || !state.copiesMax}
                onClick={() =>
                  dispatch({
                    type: 'workbenchGenerate',
                    ids: pending.map((shot) => shot.id),
                    copies: 1,
                  })
                }
              >
                {t('storyboard.copy35', { count: pending.length })}
              </button>
            </div>
          </div>
          <WorkbenchBulk
            ids={selection.ids}
            onClear={() => setSelection(emptySelection())}
            onSelectAll={() =>
              setSelection({ ids: state.shots.map((s) => s.id), anchor: state.shots[0]?.id })
            }
          />
          <WorkbenchFork ids={selection.ids} />
          <SortableContext
            items={state.shots.map((shot) => 'shot:' + shot.id)}
            strategy={rectSortingStrategy}
          >
            <div className="sb-scene-groups">
              {(() => {
                const renderGroup = (group) => {
                  const processing = group.shots.some((shot) =>
                      shot.versions.some((v) =>
                        [
                          'planned',
                          'queued',
                          'waiting_capacity',
                          'claimed',
                          'submitting',
                          'submission_unknown',
                          'generating',
                          'running',
                          'collecting',
                          'checking',
                          'cancel_requested',
                        ].includes(v.status),
                      ),
                    ),
                    lastScene = groups.filter((row) => row.id).length <= 1,
                    locked = state.busy || !!state.workbenchPending;
                  return (
                    <section className="scene" key={group.id || 'unparented'}>
                      <div className="scene-head">
                        <b>{group.title}</b>
                        <small>
                          {t(
                            group.shots.length === 1
                              ? 'storyboard.sceneOneShot'
                              : 'storyboard.sceneShots',
                            { count: group.shots.length },
                          )}{' '}
                          · {fmt(totalDuration(group.shots))}
                        </small>
                        <div className="spacer" />
                        <button
                          className="small ghost"
                          disabled={locked || !group.id}
                          onClick={() =>
                            dispatch({
                              type: 'add',
                              sceneId: group.id,
                              title: t('storyboard.newShot'),
                              sceneTitle: t('storyboard.newScene'),
                            })
                          }
                        >
                          <Plus size={14} />
                          {t('storyboard.addShot')}
                        </button>
                        <button
                          className="small ghost"
                          aria-label={t('storyboard.deleteScene')}
                          title={t(
                            lastScene
                              ? 'storyboard.lastScene'
                              : processing
                                ? 'storyboard.sceneBusy'
                                : 'storyboard.deleteScene',
                          )}
                          disabled={
                            locked ||
                            lastScene ||
                            processing ||
                            !group.id ||
                            state.workspaceMode !== 'cloud' ||
                            !trash?.account ||
                            trash.busy ||
                            !!trash.pending
                          }
                          onClick={() =>
                            trash.request('trash', [
                              { kind: 'entity', project_id: state.project.id, id: group.id },
                            ])
                          }
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                      {group.shots.length ? (
                        <div className="wall">
                          {group.shots.map((shot) => (
                            <ShotCard
                              key={shot.id}
                              shot={shot}
                              index={state.shots.indexOf(shot)}
                              selected={selected === shot.id}
                              checked={selection.ids.includes(shot.id)}
                              picking={selection.ids.length > 0}
                              onSelect={(event) =>
                                setSelection((old) =>
                                  selectShot(
                                    old,
                                    state.shots.map((s) => s.id),
                                    shot.id,
                                    { range: event.shiftKey, toggle: !event.shiftKey },
                                  ),
                                )
                              }
                              onOpen={(id, event) =>
                                event?.ctrlKey || event?.metaKey || event?.shiftKey
                                  ? setSelection((old) =>
                                      selectShot(
                                        old,
                                        state.shots.map((s) => s.id),
                                        id,
                                        { range: event.shiftKey, toggle: !event.shiftKey },
                                      ),
                                    )
                                  : setSelected(id)
                              }
                            />
                          ))}
                        </div>
                      ) : (
                        <p className="scene-empty muted">{t('storyboard.sceneEmpty')}</p>
                      )}
                    </section>
                  );
                };
                const orphans = groups.filter((group) => !group.chapterId);
                if (!multi)
                  return (
                    <>
                      {groups.map(renderGroup)}
                      <AddChapter count={chapters.length} />
                    </>
                  );
                return (
                  <>
                    {chapters.map((chapter, index) => {
                      const own = groups.filter((group) => group.chapterId === chapter.id),
                        shots = own.flatMap((group) => group.shots);
                      return (
                        <div className="sb-chapter" key={chapter.id}>
                          <ChapterHeader
                            chapter={chapter}
                            index={index}
                            count={chapters.length}
                            shots={shots}
                            folded={folded.has(chapter.id)}
                            onFold={() =>
                              setFolded((old) => {
                                const next = new Set(old);
                                next.has(chapter.id)
                                  ? next.delete(chapter.id)
                                  : next.add(chapter.id);
                                return next;
                              })
                            }
                            onNewScene={onNewScene}
                          />
                          {!folded.has(chapter.id) && own.map(renderGroup)}
                        </div>
                      );
                    })}
                    {orphans.map(renderGroup)}
                    <AddChapter count={chapters.length} />
                  </>
                );
              })()}
            </div>
          </SortableContext>
        </main>
        {theater && (
          <section className="sb-theater" role="dialog" aria-label={t('storyboard.theater')}>
            <div className="sb-theater-stage">
              <Screen player={player} />
            </div>
            <button className="small sb-theater-close" onClick={() => setTheater(false)}>
              <Minimize2 size={14} />
              {t('storyboard.exitTheater')}
            </button>
          </section>
        )}
        {!theater && (
          <StoryboardSplitter
            panel={dock}
            axis="y"
            sign={-1}
            current={dockLayout.size}
            onDouble={toggleDock}
            title={t('storyboard.dockResize')}
            label={t('storyboard.copy20')}
          />
        )}
        <div
          className={'dock' + (theater ? ' in-theater' : dockLayout.size > 300 ? ' tall' : '')}
          style={{ height: theater ? 130 : dockLayout.size }}
        >
          {!theater && (
            <div className="dock-screen">
              <Screen player={player} />
              <button
                className="small screen-expand"
                aria-label={t('storyboard.theater')}
                title={t('storyboard.theater')}
                onClick={() => setTheater(true)}
              >
                <Maximize2 size={14} />
              </button>
            </div>
          )}
          <div className="dock-right">
            <div className="dock-transport">
              <Transport player={player} />
              <button
                className="small ghost dock-theater-toggle"
                aria-label={t(theater ? 'storyboard.exitTheater' : 'storyboard.theater')}
                onClick={() => setTheater((value) => !value)}
              >
                {theater ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
              </button>
            </div>
            <Timeline
              player={player}
              selectedId={theater ? player.seg?.shot.id : selected}
              onPick={theater ? undefined : setSelected}
            />
            <small className="muted sb-timeline-hint">{t('storyboard.copy21')}</small>
          </div>
        </div>
      </div>
      {selected && (
        <aside
          hidden={theater}
          className={'drawer' + (expanded ? ' wide workbench-expanded' : '')}
          aria-label={t('storyboard.copy22')}
          style={{ width: drawer.size }}
        >
          <StoryboardSplitter
            panel={drawer}
            sign={-1}
            className="edge"
            label={t('storyboard.copy23')}
          />
          <ShotEditor
            shotId={selected}
            wide={expanded}
            onWide={() => setExpanded((v) => !v)}
            onClose={() => (expanded ? setExpanded(false) : setSelected(null))}
          />
        </aside>
      )}
      <ProfileEditor />
    </div>
  );
}
function StoryboardShell({ onLegacyGuide, onCanvas, onNewScene, filmControl }) {
  const { t, locale } = useI18n();
  const { state, dispatch, db, clearError } = useStoryboardUI(),
    [selected, setSelected] = useState(null),
    [dragging, setDragging] = useState(null),
    [toast, setToast] = useState('');
  const pageActive = usePageActive(),
    location = useAppLocation();
  const showSelected = (id) => {
    setSelected(id);
    if (!pageActive) return;
    const url = new URL(window.location.href);
    url.searchParams.set('view', 'workspace');
    if (state.workspaceMode === 'cloud' && state.project.id)
      url.searchParams.set('project', state.project.id);
    else url.searchParams.delete('project');
    if (id) url.searchParams.set('entity', id);
    else url.searchParams.delete('entity');
    navigateApp(url.pathname + url.search, { restore: false });
  };
  useEffect(() => {
    if (!pageActive || location.path !== '/') return;
    const params = new URLSearchParams(location.search),
      id = params.get('entity');
    if (id && state.shots.some((s) => s.id === id)) setSelected(id);
    else if (!id) setSelected(null);
  }, [pageActive, location.href, state.project.id, state.shots.map((s) => s.id).join('|')]);
  const container = useRef(),
    sensors = useSensors(
      useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
      useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
    );
  useEffect(() => {
    if (state.lastAdded) setSelected(state.lastAdded);
  }, [state.lastAdded]);
  useEffect(() => {
    if (selected && !state.shots.some((shot) => shot.id === selected)) setSelected(null);
  }, [state.shots, selected]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(''), 4000);
    return () => clearTimeout(timer);
  }, [toast]);
  const dropOnShot = async (item, shotId, slot = 'smart') => {
    const shot = state.shots.find((s) => s.id === shotId),
      object = item.profile
        ? { profile: db.profile(item.profile) }
        : { asset: db.asset(item.asset) };
    if (!shot || !(object.profile || object.asset)) return;
    const plan = planDrop(shot, object, slot, db);
    if (!plan.patch) {
      setToast(plan.msg);
      return;
    }
    const receipt = await dispatch({ type: 'drop', ...item, shot: shotId, slot });
    if (receipt?.ok !== false) setToast(plan.msg || t('storyboard.copy24'));
  };
  const pick = (item) => {
    if (!selected) {
      if (item.profile) return dispatch({ type: 'openProfile', id: item.profile });
      setToast(t('storyboard.copy25'));
      return;
    }
    return dropOnShot(item, selected);
  };
  const onDragStart = (event) => {
    const id = String(event.active.id);
    setDragging(
      id.startsWith('asset:')
        ? { asset: id.slice(6) }
        : id.startsWith('profile:')
          ? { profile: id.slice(8) }
          : null,
    );
  };
  const onDragEnd = async ({ active, over }) => {
    setDragging(null);
    if (!over) return;
    const a = String(active.id),
      o = String(over.id);
    if (a.startsWith('shot:') && o.startsWith('shot:'))
      return dispatch({ type: 'reorder', from: a.slice(5), to: o.slice(5) });
    const item = a.startsWith('asset:')
      ? { asset: a.slice(6) }
      : a.startsWith('profile:')
        ? { profile: a.slice(8) }
        : null;
    if (!item) return;
    if (o.startsWith('shot:')) return dropOnShot(item, o.slice(5));
    if (o.startsWith('slot:')) {
      const [, slot, shot] = o.split(':');
      return dropOnShot(item, shot, slot === 'cast' ? 'smart' : slot);
    }
    if (item.asset && /^(pdrop|vdrop|voicedrop):/.test(o)) {
      const asset = db.asset(item.asset),
        [kind, pid, vid] = o.split(':'),
        profile = db.profile(pid);
      if (!asset || !profile) return;
      if (asset.kind === 'audio') {
        if (profile.type === 'character')
          return dispatch({ type: 'addVoice', pid, asset: asset.id });
        if (profile.type === 'location')
          return dispatch({ type: 'profile', id: pid, patch: { ambient: asset.id } });
        setToast(t('storyboard.copy26'));
        return;
      }
      if (asset.kind !== 'image') {
        setToast(t('storyboard.copy27'));
        return;
      }
      if (kind === 'voicedrop') {
        setToast(t('storyboard.copy28'));
        return;
      }
      const variant = variantOf(profile, vid || profile.def);
      if (!variant) {
        setToast(t('storyboard.copy29'));
        return;
      }
      return dispatch({ type: 'fileImage', asset: item.asset, pid, vid: variant.id });
    }
  };
  const dragged =
      dragging && (dragging.asset ? db.asset(dragging.asset) : db.profile(dragging.profile)),
    thumb = dragging?.profile ? coverOf(db, dragged, dragged?.def) : dragged;
  return (
    <DndContext
      sensors={sensors}
      collisionDetection={collision}
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onDragCancel={() => setDragging(null)}
    >
      <section ref={container} className="sb-workspace" aria-label={t('storyboard.copy30')}>
        {state.error && (
          <div className="sb-error" role="alert">
            <span>{state.error}</span>
            <button className="x" aria-label={t('storyboard.copy31')} onClick={clearError}>
              <X size={14} />
            </button>
          </div>
        )}
        {state.workbenchPending && (
          <div className="sb-error">
            <span>{t('workbench.pending')}</span>
            <button disabled={state.busy} onClick={() => dispatch({ type: 'workbenchRecover' })}>
              {t('workbench.recover')}
            </button>
          </div>
        )}
        <StoryboardLayoutA
          filmControl={filmControl}
          onNewScene={onNewScene}
          selected={selected}
          setSelected={showSelected}
          pickAsset={pick}
        />
        <DragOverlay dropAnimation={null}>
          {dragged ? (
            <div className="sb-portal achip overlay">
              <AssetThumb asset={thumb} />
              <b>{dragged.name}</b>
            </div>
          ) : null}
        </DragOverlay>
        {toast && (
          <div className="sb-toast" role="status" aria-live="polite">
            {toast}
          </div>
        )}
      </section>
    </DndContext>
  );
}
export function StoryboardWorkspace({ adapter, onLegacyGuide, onCanvas, onNewScene, filmControl }) {
  const { t, locale } = useI18n();
  return (
    <StoryboardUIProvider adapter={adapter}>
      <StoryboardShell
        onNewScene={onNewScene}
        onLegacyGuide={onLegacyGuide}
        onCanvas={onCanvas}
        filmControl={filmControl}
      />
    </StoryboardUIProvider>
  );
}
export default StoryboardWorkspace;
