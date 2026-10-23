import './v2/works-adapter.css';
import { TrashMenu, useTrash } from './UniversalTrash.jsx';
import { translate } from './i18n/locale.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  BookOpen,
  Check,
  ChevronDown,
  Clapperboard,
  Cloud,
  HardDrive,
  LayoutGrid,
  MessageSquare,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  X,
} from 'lucide-react';
import { ChatDialog } from './QuickChatControls.jsx';
import { store } from './store.js';
import { cloudController as cloud } from './cloud-controller.js';
import { useThumbnailSource } from './use-thumbnail-source.js';
import { useCloud } from './CloudStudio.jsx';
import { useAccountLabel } from './StudioHeader.jsx';
import {
  createLocalStory,
  openLocalStory,
  readLocalStoryLibrary,
  storyCoverFileId,
  storyEditedTimestamp,
  storyLibrarySummary,
} from './story-navigation.js';
import { createCloudClient } from './cloud-client.js';
import { checkedProjectRecord } from './cloud-model.js';
import { getFile } from './media.js';
import StoryOpenNotice from './StoryOpenNotice.jsx';
import { storyTitle } from './story-open.js';

export default function StoryNavigation({ state, onOpenCloud, onOpenLocal, onSelect }) {
  const { t, locale } = useI18n();
  const c = useCloud(),
    [creating, setCreating] = useState(false),
    [title, setTitle] = useState(''),
    [source, setSource] = useState('local'),
    [query, setQuery] = useState(''),
    [error, setError] = useState(''),
    [pending, setPending] = useState(''),
    [library, setLibrary] = useState(() => readLocalStoryLibrary()),
    inputRef = useRef();
  const refreshLocal = () => setLibrary(readLocalStoryLibrary());
  useEffect(() => {
    refreshLocal();
  }, [state.project.id, state.project.title, state.workspace.mode]);
  useEffect(() => {
    const refresh = (e) => {
      if (!e.key || e.key === 'yingxu-project-library-v1') refreshLocal();
    };
    window.addEventListener('storage', refresh);
    window.addEventListener('focus', refreshLocal);
    return () => {
      window.removeEventListener('storage', refresh);
      window.removeEventListener('focus', refreshLocal);
    };
  }, []);
  useEffect(() => {
    setQuery('');
    setError('');
    setCreating(false);
    setTitle('');
  }, [c.account]);
  useEffect(() => {
    if (creating) inputRef.current?.focus();
  }, [creating]);
  const local = store.getLocalProjectSummary(),
    locals = [...(local ? [local] : []), ...library.projects.filter((p) => p.id !== local?.id)];
  const cloudProjects = c.account ? [...c.projects] : [];
  if (c.account && state.workspace.mode === 'cloud') {
    const index = cloudProjects.findIndex((p) => p.id === state.project.id),
      current = { id: state.project.id, title: state.project.title };
    if (index < 0) cloudProjects.unshift(current);
    else cloudProjects[index] = { ...cloudProjects[index], ...current };
  }
  const matches = (p) =>
      String(p.title ?? '')
        .toLocaleLowerCase()
        .includes(query.trim().toLocaleLowerCase()),
    visibleCloud = cloudProjects.filter(matches),
    visibleLocal = locals.filter(matches),
    working = c.busy || !!pending;
  async function act(label, fn) {
    setPending(label);
    setError('');
    try {
      return await fn();
    } catch (e) {
      setError(e.message);
      return false;
    } finally {
      setPending('');
    }
  }
  const openCloud = (id) =>
    act(t('legacy.visible1'), async () => {
      const done = await cloud.open(id).catch(() => false);
      if (done) onSelect();
      return done;
    });
  const openLocal = (id) =>
    act(t('legacy.visible1'), () => {
      const done = openLocalStory(id, { store, cloud });
      if (done) onSelect();
      return done;
    });
  const begin = () => {
    setTitle('');
    setSource(c.account ? 'cloud' : 'local');
    setError('');
    setCreating(true);
  };
  const create = (e) => {
    e.preventDefault();
    void act(t('legacy.visible2'), async () => {
      const done =
        source === 'cloud'
          ? await cloud.create(title.trim())
          : createLocalStory(title, { store, cloud });
      refreshLocal();
      if (done) {
        setCreating(false);
        setTitle('');
        setQuery('');
        store.notify(t('legacy.visible3'));
        onSelect();
      } else if (cloud.getState().draftOffer) onOpenCloud();
      return done;
    });
  };
  const item = (p, kind) => {
    const active = state.workspace.mode === kind && state.project.id === p.id,
      I = kind === 'cloud' ? Cloud : HardDrive;
    return (
      <button
        key={kind + ':' + p.id}
        type="button"
        className="story-nav-item"
        aria-current={active ? 'page' : undefined}
        disabled={working}
        onClick={() => (active ? onSelect() : kind === 'cloud' ? openCloud(p.id) : openLocal(p.id))}
        title={storyTitle(p, t('legacy.visible24'))}
      >
        <I size={14} />
        <span>
          <b>{storyTitle(p, t('legacy.visible24'))}</b>
          <small>
            {kind === 'cloud' ? t('legacy.visible4') : t('legacy.visible5')}
            {active ? t('legacy.visible6') : ''}
          </small>
        </span>
        {active && <Check size={13} />}
      </button>
    );
  };
  return (
    <section className="story-navigation" aria-label={t('account.copy1')}>
      <div className="story-nav-heading">
        <h2 id="story-library-title" tabIndex={-1}>
          <BookOpen size={17} />
          {t('account.copy1')}
        </h2>
        <button
          type="button"
          className="story-nav-refresh"
          disabled={working}
          aria-label={t('account.accessible1')}
          title={t('account.accessible1')}
          onClick={() =>
            act(t('legacy.visible7'), async () => {
              refreshLocal();
              if (c.account) await cloud.list();
            })
          }
        >
          <RefreshCw size={13} />
        </button>
      </div>
      <button type="button" className="story-nav-create" disabled={working} onClick={begin}>
        <Plus size={15} />
        {t('account.copy2')}
      </button>
      {creating && (
        <form className="story-nav-form" onSubmit={create}>
          <div>
            <label htmlFor="new-sidebar-story-title">{t('account.copy3')}</label>
            <button
              type="button"
              disabled={working}
              aria-label={t('account.accessible2')}
              onClick={() => setCreating(false)}
            >
              <X size={14} />
            </button>
          </div>
          <input
            ref={inputRef}
            id="new-sidebar-story-title"
            required
            maxLength={80}
            placeholder={t('account.accessible3')}
            value={title}
            disabled={working}
            onChange={(e) => setTitle(e.target.value)}
          />
          <label htmlFor="new-sidebar-story-source">{t('account.copy4')}</label>
          <select
            id="new-sidebar-story-source"
            value={source}
            disabled={working}
            onChange={(e) => setSource(e.target.value)}
          >
            {c.account && (
              <option value="cloud">{t('legacy.cloudAccount', { account: c.account })}</option>
            )}
            <option value="local">{t('account.copy5')}</option>
          </select>
          <small>{source === 'cloud' ? t('legacy.visible8') : t('legacy.visible9')}</small>
          <button type="submit" disabled={working || !title.trim()}>
            {t('account.copy6')}
          </button>
        </form>
      )}
      <div id="sidebar-story-list">
        <label className="story-nav-search">
          <span className="visually-hidden">{t('account.copy7')}</span>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('account.copy7')}
          />
        </label>
        <nav className="story-nav-list" aria-label={t('account.accessible4')}>
          {c.account && (
            <>
              <p className="story-nav-group">{t('legacy.cloudAccount', { account: c.account })}</p>
              {visibleCloud.map((p) => item(p, 'cloud'))}
              {!c.projectsLoaded && <p className="story-nav-note">{t('account.copy8')}</p>}
              {c.projectsLoaded && !cloudProjects.length && (
                <p className="story-nav-note">{t('account.copy9')}</p>
              )}
            </>
          )}
          <p className="story-nav-group">{t('account.copy10')}</p>
          {visibleLocal.map((p) => item(p, 'local'))}
          {query && !visibleCloud.length && !visibleLocal.length && (
            <p className="story-nav-note">{t('account.copy11')}</p>
          )}
        </nav>
        {c.moreProjects && (
          <button
            type="button"
            className="story-nav-more"
            disabled={working}
            onClick={() => act(t('legacy.visible10'), () => cloud.loadMoreProjects())}
          >
            {t('account.copy12')}
          </button>
        )}
        {query && c.moreProjects && <p className="story-nav-note">{t('account.copy13')}</p>}
        {library.error && (
          <p className="story-nav-error" role="alert">
            {t('account.copy14')}
            <button type="button" onClick={onOpenLocal}>
              {t('account.copy15')}
            </button>
          </p>
        )}
        {!c.account && (
          <button type="button" className="story-nav-more" onClick={onOpenCloud}>
            {t('account.copy16')}
          </button>
        )}
      </div>
      {state.workspace.mode === 'cloud' && state.workspace.dirty && (
        <p className="story-nav-note">
          {state.saveState.startsWith('保存失败') ? t('legacy.visible11') : t('legacy.visible12')}
        </p>
      )}
      {pending && (
        <p className="story-nav-note" role="status">
          {pending}
        </p>
      )}
      {error && (
        <p className="story-nav-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

function useStoryLibrary(state) {
  const { t } = useI18n();
  const c = useCloud(),
    [library, setLibrary] = useState(() => readLocalStoryLibrary()),
    [pending, setPending] = useState(''),
    [error, setError] = useState('');
  const pendingRef = useRef(false),
    refreshLocal = () => setLibrary(readLocalStoryLibrary());
  useEffect(() => {
    refreshLocal();
  }, [state.project.id, state.project.title, state.project.updatedAt, state.workspace.mode]);
  useEffect(() => {
    const refresh = (event) => {
      if (!event?.key || event.key === 'yingxu-project-library-v1') refreshLocal();
    };
    window.addEventListener('storage', refresh);
    window.addEventListener('focus', refreshLocal);
    return () => {
      window.removeEventListener('storage', refresh);
      window.removeEventListener('focus', refreshLocal);
    };
  }, []);
  useEffect(() => {
    setError('');
    setPending('');
  }, [c.account]);
  const local = store.getLocalProjectSummary(),
    currentLocal = state.workspace.mode === 'local' ? state.project : local;
  const locals = [
    ...(currentLocal ? [currentLocal] : []),
    ...library.projects.filter((project) => project.id !== currentLocal?.id),
  ];
  const cloudProjects = c.account ? [...c.projects] : [];
  if (c.account && state.workspace.mode === 'cloud' && state.workspace.account === c.account) {
    const index = cloudProjects.findIndex((project) => project.id === state.project.id);
    if (index < 0) cloudProjects.unshift(state.project);
    else cloudProjects[index] = { ...cloudProjects[index], ...state.project };
  }
  const stories = [
    ...cloudProjects.map((project) => ({ project, mode: 'cloud' })),
    ...locals.map((project) => ({ project, mode: 'local' })),
  ];
  const working = c.busy || !!pending;
  async function act(message, fn) {
    if (working || pendingRef.current) return false;
    pendingRef.current = true;
    setPending(message);
    setError('');
    try {
      return await fn();
    } catch (error) {
      setError(error.message);
      return false;
    } finally {
      pendingRef.current = false;
      setPending('');
    }
  }
  async function openStory(story, onSelect, onOpenCloud) {
    return act(t('legacy.visible1'), async () => {
      const active = state.workspace.mode === story.mode && state.project.id === story.project.id;
      const done = active
        ? true
        : story.mode === 'cloud'
          ? await cloud.open(story.project.id).catch(() => false)
          : openLocalStory(story.project.id, { store, cloud });
      if (done) onSelect?.();
      else if (cloud.getState().projectOpen?.status === 'legacy')
        onSelect?.({ projectId: story.project.id });
      return done;
    });
  }
  return { c, library, stories, working, pending, error, act, openStory, refreshLocal };
}

function editedLabel(value, locale = 'zh') {
  const stamp = storyEditedTimestamp(value);
  return stamp
    ? new Intl.DateTimeFormat(locale === 'zh' ? 'zh-CN' : 'en', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      }).format(stamp)
    : translate(locale, 'legacy.visible13');
}
function StoryLocation({ mode }) {
  const { t, locale } = useI18n();
  // Stories live in the cloud by default; only browser-only ones need a label (owner, 2026-10-21).
  if (mode === 'cloud') return null;
  return (
    <span className={'badge ' + mode}>
      {mode === 'cloud' ? <Cloud size={12} /> : <HardDrive size={12} />}{' '}
      {mode === 'cloud' ? t('account.interface5') : t('account.interface6')}
    </span>
  );
}

function useLibraryReader(account) {
  const { t } = useI18n();
  const reader = useMemo(() => {
    const value = createCloudClient({
      onUnauthorized: () => {
        void cloud.recheckSession().catch(() => {});
      },
    });
    value.setAccount(account);
    return value;
  }, [account]);
  useEffect(() => () => reader.reset(), [reader]);
  const guard = () => {
    if (!account || cloud.getState().account !== account) throw Error(t('legacy.visible14'));
  };
  return useMemo(
    () => ({
      project: async (id) => {
        guard();
        const record = checkedProjectRecord(await reader.project(id));
        guard();
        if (record.id !== id) throw Error(t('legacy.visible15'));
        return record.project;
      },
      cover: (file) => {
        guard();
        const href = reader.nativeContentURL(file.cloudPath, { rendition: 'thumb' });
        guard();
        return Object.freeze({ href, assertCurrent: guard });
      },
    }),
    [reader, account],
  );
}

function StoryLibraryCard({ story, reader, working, onOpen, picked = false, onPick }) {
  const { t, locale } = useI18n();
  const ref = useRef(),
    [visible, setVisible] = useState(false),
    [loaded, setLoaded] = useState(null),
    [detailError, setDetailError] = useState(false),
    [cover, setCover] = useState(null),
    [coverError, setCoverError] = useState(false);
  const project = Array.isArray(story.project.entities) ? story.project : loaded || story.project,
    summary = storyLibrarySummary(project, locale),
    file = storyCoverFileId(summary.cover, { mode: story.mode });
  const coverKey = file?.cloudPath || file?.fileId;
  let coverUrl =
    cover?.reader === reader && cover?.key === coverKey && !coverError ? cover.url : '';
  try {
    cover?.target?.assertCurrent();
  } catch {
    coverUrl = '';
  }
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: '160px' },
    );
    if (ref.current) observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    let alive = true;
    setLoaded(null);
    setDetailError(false);
    if (!visible || story.mode !== 'cloud' || Array.isArray(story.project.entities)) return;
    reader
      .project(story.project.id)
      .then((project) => {
        if (alive) setLoaded(project);
      })
      .catch(() => {
        if (alive) setDetailError(true);
      });
    return () => {
      alive = false;
    };
  }, [
    visible,
    reader,
    story.mode,
    story.project.id,
    story.project.version,
    story.project.updated_at,
    Array.isArray(story.project.entities),
  ]);
  useEffect(() => {
    let alive = true,
      url = '';
    setCover(null);
    setCoverError(false);
    if (!visible || !file) return;
    if (file.cloudPath) {
      try {
        const target = reader.cover(file);
        target.assertCurrent();
        setCover({ reader, key: coverKey, url: target.href, target });
      } catch {
        setCoverError(true);
      }
      return () => {
        alive = false;
      };
    }
    getFile(file.fileId)
      .then((blob) => {
        if (!alive) return;
        if (!blob?.size || (blob.type && !blob.type.startsWith(file.kind + '/'))) {
          setCoverError(true);
          return;
        }
        url = URL.createObjectURL(blob);
        setCover({ reader, key: coverKey, url });
      })
      .catch(() => {
        if (alive) setCoverError(true);
      });
    return () => {
      alive = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [visible, reader, file?.fileId, file?.kind, file?.cloudPath]);
  const small = useThumbnailSource(reader, coverUrl, { enabled: !!file?.cloudPath }),
    coverSrc = file?.cloudPath ? small.src : coverUrl;
  const coverFailed = () => {
    small.finish();
    setCoverError(true);
  };
  const known = summary.shotCount !== null,
    chapterCount = project.entities?.filter((e) => e.type === 'chapter').length ?? 0;
  return (
    <div className={'story-wrap' + (picked ? ' picked' : '')}>
      {onPick && (
        <label className="story-pick" title={t('trash.selectStory')}>
          <input
            type="checkbox"
            checked={picked}
            onChange={onPick}
            aria-label={t('trash.selectStory') + ' · ' + summary.title}
          />
        </label>
      )}
      <button
        ref={ref}
        type="button"
        className="story-card"
        data-ui-surface="card"
        aria-describedby={
          'story-logline-' + story.mode + '-' + encodeURIComponent(story.project.id)
        }
        disabled={working}
        onClick={onOpen}
        aria-label={t('legacy.openStory', {
          location: story.mode === 'cloud' ? t('account.interface5') : t('account.interface6'),
          title: summary.title,
        })}
      >
        <div className="cover">
          {coverSrc ? (
            file.kind === 'video' && !file.cloudPath ? (
              <video
                ref={small.ref}
                src={coverSrc}
                muted
                playsInline
                preload="metadata"
                aria-hidden="true"
                onError={coverFailed}
              />
            ) : (
              <img
                ref={small.ref}
                src={coverSrc}
                alt=""
                draggable={false}
                loading="lazy"
                decoding="async"
                onLoad={small.finish}
                onError={coverFailed}
              />
            )
          ) : (
            <span
              ref={small.ref}
              className="cover-empty"
              role={coverError ? 'alert' : undefined}
              title={coverError ? t('library.previewUnavailable') : undefined}
            >
              {coverError ? t('library.previewUnavailable') : <Clapperboard size={30} />}
            </span>
          )}
          <StoryLocation mode={story.mode} />
          {(project.journey?.brief?.aspect || project.journey?.storyboardDefaults?.aspect) && (
            <em className="aspect">
              {project.journey?.brief?.aspect || project.journey?.storyboardDefaults?.aspect}
            </em>
          )}
        </div>
        <b title={summary.title}>{summary.title}</b>
        <small
          id={'story-logline-' + story.mode + '-' + encodeURIComponent(story.project.id)}
          className={'clip story-logline' + (!project.logline ? ' muted' : '')}
          title={project.logline || t('v2.noLogline')}
        >
          {project.logline || t('v2.noLogline')}
        </small>
        <small className="muted story-card-meta">
          <span>
            {known ? (
              <>
                {t(chapterCount === 1 ? 'v2.storyChapter' : 'v2.storyChapters', {
                  count: chapterCount,
                })}
                {' · '}
                {t(summary.shotCount === 1 ? 'v2.storyShot' : 'v2.storyShots', {
                  count: summary.shotCount,
                })}
                {' · '}
                {t('v2.storySelected', { selected: summary.chosenCount, total: summary.shotCount })}
              </>
            ) : detailError ? (
              t('legacy.visible16')
            ) : (
              t('legacy.visible17')
            )}
          </span>
          <time>{editedLabel(summary.updatedAt, locale)}</time>
        </small>
      </button>
      {story.mode === 'cloud' && (
        <TrashMenu
          target={{ kind: 'project', project_id: story.project.id, id: story.project.id }}
          onEdit={onOpen}
        />
      )}
    </div>
  );
}

export function StoryLibraryHome({ state, onOpenCloud, onOpenLocal, onSelect }) {
  const { t, locale } = useI18n();
  const library = useStoryLibrary(state),
    { c, stories, working, act, pending } = library,
    [query, setQuery] = useState(''),
    [creating, setCreating] = useState(false),
    [title, setTitle] = useState(''),
    [source, setSource] = useState('local'),
    [visibleLimit, setVisibleLimit] = useState(12),
    input = useRef(),
    reader = useLibraryReader(c.account),
    who = useAccountLabel(c.account),
    trash = useTrash(),
    // Hover a cloud story to tick it; with any ticked, clicks tick instead of opening.
    [picked, setPicked] = useState(() => new Set());
  const selecting = picked.size > 0,
    pickable = (story) => story.mode === 'cloud' && !!trash?.account;
  const toggle = (id) =>
    setPicked((before) => {
      const next = new Set(before);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  const trashPicked = async () => {
    const ids = [...picked];
    const receipt = await trash.request(
      'trash',
      ids.map((id) => ({ kind: 'project', project_id: id, id })),
    );
    if (receipt) setPicked(new Set());
  };
  useEffect(() => {
    setPicked(new Set());
    setQuery('');
    setCreating(false);
    setTitle('');
    setVisibleLimit(12);
  }, [c.account]);
  useEffect(() => setVisibleLimit(12), [query]);
  useEffect(() => {
    if (creating) input.current?.focus();
  }, [creating]);
  const begin = () => {
    setTitle('');
    setSource(c.account ? 'cloud' : 'local');
    setCreating(true);
  };
  const create = (event) => {
    event.preventDefault();
    void act(t('legacy.visible2'), async () => {
      const done =
        source === 'cloud'
          ? await cloud.create(title.trim())
          : createLocalStory(title, { store, cloud });
      library.refreshLocal();
      if (done) {
        setCreating(false);
        setTitle('');
        store.notify(t('legacy.visible3'));
        onSelect?.();
      } else if (cloud.getState().draftOffer) onOpenCloud?.();
      return done;
    });
  };
  const visible = stories.filter((story) =>
    String(story.project.title || '')
      .toLocaleLowerCase()
      .includes(query.trim().toLocaleLowerCase()),
  );
  return (
    <section
      className={'home-main works-page' + (pending ? ' is-opening' : '')}
      aria-labelledby="story-library-heading"
      aria-busy={pending ? 'true' : undefined}
    >
      {/* Opening a story: one clear status instead of a dimmed, half-read grid. */}
      {pending && (
        <div className="story-opening" role="status">
          <span className="story-opening-spinner" aria-hidden="true" />
          {pending}
        </div>
      )}
      <header className="home-head">
        <h1 className="page-title" id="story-library-heading" tabIndex={-1}>
          <LayoutGrid size={22} />
          {t('v2.myStories')}
          <small>{stories.length}</small>
        </h1>
        <div className="home-search-row">
          <label className="v2-home-search">
            <Search size={15} aria-hidden="true" />
            <input
              type="search"
              aria-label={t('account.accessible5')}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t('account.accessible6')}
            />
          </label>
          {/* The only library action, so a direct button instead of a one-item "..." menu. */}
          <span className="item-menu">
            <button
              type="button"
              disabled={working}
              aria-label={t('account.accessible7')}
              title={t('account.accessible7')}
              onClick={() =>
                void act(t('legacy.visible18'), async () => {
                  library.refreshLocal();
                  if (c.account) await cloud.list();
                })
              }
            >
              <RefreshCw size={15} aria-hidden="true" />
            </button>
          </span>
        </div>
        <a
          className="v2-home-quick"
          href="/quick-chat"
          aria-label={t('nav.quickCreate')}
          title={t('nav.quickCreate')}
        >
          <MessageSquare size={16} />
          <span>{t('nav.quickCreate')}</span>
        </a>
        <button
          type="button"
          className="primary v2-home-new"
          disabled={working}
          onClick={begin}
          aria-label={t('account.copy2')}
          title={t('account.copy2')}
        >
          <Plus size={16} />
          <span>{t('account.copy2')}</span>
        </button>
      </header>
      {creating && (
        <ChatDialog
          title={t('account.copy2')}
          onClose={() => {
            if (!working) setCreating(false);
          }}
        >
          <form className="story-library-create-form" onSubmit={create}>
            <label>
              {t('account.copy3')}
              <input
                ref={input}
                required
                maxLength={80}
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder={t('account.accessible3')}
                disabled={working}
              />
            </label>
            {c.account ? (
              <label>
                {t('account.copy4')}
                <select
                  value={source}
                  onChange={(event) => setSource(event.target.value)}
                  disabled={working}
                >
                  <option value="cloud">
                    {t('legacy.cloudAccount', { account: who.label || c.account })}
                  </option>
                  <option value="local">{t('account.copy21')}</option>
                </select>
              </label>
            ) : (
              // Signed out there is only one place to save, so say it instead of a one-item list.
              <p className="muted">
                {t('account.copy4')} · {t('account.copy21')}
              </p>
            )}
            <button type="submit" className="primary" disabled={working || !title.trim()}>
              {t('account.copy6')}
            </button>
          </form>
        </ChatDialog>
      )}
      {!c.account && (
        <p className="story-library-note signed-out">
          {t('account.copy22')}
          <button type="button" onClick={onOpenCloud}>
            {t('account.copy23')}
          </button>
        </p>
      )}
      <StoryOpenNotice onOpened={onSelect} />
      {selecting && (
        <div className="works-select-bar" role="toolbar" aria-label={t('trash.selectStory')}>
          <b>{t('trash.selectedCount', { count: picked.size })}</b>
          <button
            type="button"
            className="linkish"
            onClick={() =>
              setPicked(new Set(visible.filter(pickable).map((story) => story.project.id)))
            }
          >
            {t('trash.selectAll')}
          </button>
          <span className="spacer" />
          <button type="button" className="ghost" onClick={() => setPicked(new Set())}>
            {t('library.cancel')}
          </button>
          <button
            type="button"
            className="danger"
            disabled={trash?.busy || !!trash?.pending}
            onClick={() => void trashPicked()}
          >
            <Trash2 size={14} aria-hidden="true" />
            {t('trash.moveSelected', { count: picked.size })}
          </button>
        </div>
      )}
      <div className={'story-grid' + (selecting ? ' selecting' : '')}>
        {visible.slice(0, visibleLimit).map((story) => (
          <StoryLibraryCard
            key={
              (story.mode === 'cloud' ? c.account : 'local') +
              ':' +
              story.mode +
              ':' +
              story.project.id
            }
            story={story}
            reader={reader}
            working={working}
            picked={picked.has(story.project.id)}
            onPick={pickable(story) ? () => toggle(story.project.id) : undefined}
            onOpen={() =>
              selecting && pickable(story)
                ? toggle(story.project.id)
                : library.openStory(story, onSelect, onOpenCloud)
            }
          />
        ))}
        <button
          className="story-card new"
          data-ui-surface="card"
          disabled={working}
          onClick={begin}
        >
          <Plus size={40} strokeWidth={1.5} />
          <b>{t('account.copy2')}</b>
          <small>{t('v2.newStoryHint')}</small>
        </button>
      </div>
      <footer className="home-foot">
        <a href="/trash">
          <Trash2 size={14} />
          {t('trash.title')}
        </a>
      </footer>
      {!visible.length && (
        <div className="story-library-empty">
          <BookOpen size={28} />
          <h2>{query ? t('legacy.visible19') : t('legacy.visible20')}</h2>
          <p>{query ? t('legacy.visible21') : t('legacy.visible22')}</p>
        </div>
      )}
      {c.account && !c.projectsLoaded && (
        <p className="story-library-note">{t('account.copy24')}</p>
      )}
      {visible.length > visibleLimit ? (
        <button
          type="button"
          className="story-library-more"
          disabled={working}
          onClick={() => setVisibleLimit((value) => value + 12)}
        >
          {t('account.copy25')}
        </button>
      ) : (
        c.moreProjects && (
          <button
            type="button"
            className="story-library-more"
            disabled={working}
            onClick={() =>
              act(t('legacy.visible23'), async () => {
                await cloud.loadMoreProjects();
                setVisibleLimit((value) => value + 12);
              })
            }
          >
            {t('account.copy26')}
          </button>
        )
      )}
      {c.moreProjects && query && <p className="story-library-note">{t('account.copy27')}</p>}
      {library.library.error && (
        <p className="story-library-error" role="alert">
          {t('account.copy28')}
          <button type="button" onClick={onOpenLocal}>
            {t('account.copy15')}
          </button>
        </p>
      )}
      {state.workspace.mode === 'cloud' && state.workspace.dirty && (
        <p className="story-library-note">{t('account.copy29')}</p>
      )}
      {library.pending && (
        <p className="story-library-note" role="status">
          {library.pending}
        </p>
      )}
      {library.error && (
        <p className="story-library-error" role="alert">
          {library.error}
        </p>
      )}
    </section>
  );
}

export function RecentStoriesDropdown({ state, onOpenCloud, onOpenLocal, onSelect }) {
  const { t, locale } = useI18n();
  const library = useStoryLibrary(state),
    details = useRef(),
    stories = [...library.stories]
      .sort(
        (a, b) =>
          (storyEditedTimestamp(b.project.updatedAt ?? b.project.updated_at) || 0) -
          (storyEditedTimestamp(a.project.updatedAt ?? a.project.updated_at) || 0),
      )
      .slice(0, 8);
  return (
    <details
      className="story-recent"
      ref={details}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          details.current.open = false;
          details.current.querySelector('summary')?.focus();
        }
      }}
    >
      <summary aria-label={t('legacy.recentStory', { title: state.project.title })}>
        <span className="story-recent-title">{state.project.title}</span>
        <StoryLocation mode={state.workspace.mode} />
        <ChevronDown size={14} />
      </summary>
      <div className="story-recent-menu">
        <b>{t('account.copy30')}</b>
        {stories.map((story) => (
          <button
            type="button"
            key={story.mode + ':' + story.project.id}
            disabled={library.working}
            aria-current={
              state.workspace.mode === story.mode && state.project.id === story.project.id
                ? 'page'
                : undefined
            }
            onClick={() =>
              void library
                .openStory(
                  story,
                  (selection) => {
                    details.current.open = false;
                    onSelect?.(selection);
                  },
                  onOpenCloud,
                )
                .finally(() => {
                  if (details.current) details.current.open = false;
                })
            }
          >
            <span>{storyTitle(story.project, t('legacy.visible24'))}</span>
            <StoryLocation mode={story.mode} />
          </button>
        ))}
        {!library.c.account && (
          <button type="button" onClick={onOpenCloud}>
            {t('account.copy23')}
          </button>
        )}
        {library.library.error && (
          <button type="button" onClick={onOpenLocal}>
            {t('account.copy15')}
          </button>
        )}
        {library.pending && <small role="status">{library.pending}</small>}
        {library.error && <small role="alert">{library.error}</small>}
      </div>
    </details>
  );
}
