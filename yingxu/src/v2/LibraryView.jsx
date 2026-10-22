// View ported from design/inkseq-v2-prototype/src/library.jsx.
// All ownership, uploads, commands and receipts are supplied by Library.jsx.
import React, { useRef, useState } from 'react';
import {
  Box,
  Check,
  Files,
  Film,
  Image as ImageIcon,
  MapPin,
  Music,
  Tag,
  Trash2,
  UserRound,
  X,
} from 'lucide-react';
import StudioHeader from '../StudioHeader.jsx';
import { useI18n } from '../i18n/I18nProvider.jsx';
import './library-adapter.css';
import {
  CreateProfileMenu,
  InlineName,
  PopMenu,
  ReferencePicker,
  UploadMenu,
  displayName,
  managedRow,
} from './LibraryParts.jsx';
const ICONS = {
  character: UserRound,
  location: MapPin,
  prop: Box,
  file: Files,
  image: ImageIcon,
  video: Film,
  audio: Music,
};

// Drag the sidebar edge to resize it (desktop); the width is a per-browser convenience.
const SIDE_MIN = 180,
  SIDE_MAX = 420,
  SIDE_DEFAULT = 240,
  SIDE_KEY = 'inkseq.librarySideWidth';
const clampSide = (value) => Math.min(SIDE_MAX, Math.max(SIDE_MIN, Math.round(value)));
function useSideWidth() {
  const [width, setWidth] = useState(() => {
    try {
      const saved = Number(localStorage.getItem(SIDE_KEY));
      return saved ? clampSide(saved) : SIDE_DEFAULT;
    } catch {
      return SIDE_DEFAULT;
    }
  });
  const latest = useRef(width);
  const save = (value) => {
    latest.current = value;
    setWidth(value);
    try {
      localStorage.setItem(SIDE_KEY, String(value));
    } catch {}
  };
  const start = (event) => {
    event.preventDefault();
    const origin = event.clientX,
      from = latest.current;
    const move = (e) => save(clampSide(from + e.clientX - origin));
    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', stop);
      document.body.classList.remove('libp-resizing');
    };
    document.body.classList.add('libp-resizing');
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop);
  };
  const key = (event) => {
    if (event.key === 'ArrowLeft') save(clampSide(latest.current - 16));
    if (event.key === 'ArrowRight') save(clampSide(latest.current + 16));
  };
  return { width, start, key, reset: () => save(SIDE_DEFAULT) };
}

export default function LibraryView({
  account,
  filters = { type: 'file' },
  setFilters,
  projects = [],
  rows = [],
  counts = null,
  selected = [],
  setSelected,
  libraryKey,
  locked,
  loading,
  next,
  onMore,
  onUploadFiles,
  onCreateProfile,
  onRename,
  onAddTags,
  loadProfiles,
  onFileInto,
  onCreateReference,
  onDelete,
  onDetail,
  openKey,
  renderMedia,
  feedback,
  children,
}) {
  const side = useSideWidth();
  const { t } = useI18n(),
    profile = ['character', 'location', 'prop'].includes(filters.type),
    tags = [...new Set(rows.flatMap((r) => r.tags || []))],
    [dropping, setDropping] = useState(false),
    [bulkTag, setBulkTag] = useState('');
  const change = (patch) => setFilters({ ...filters, ...patch });
  const label = (type) =>
    t(
      ['image', 'video', 'audio'].includes(type)
        ? 'library.view.' + type
        : type === 'file'
          ? 'library.allFiles'
          : 'trash.kind.' + type,
    );
  const count = (type) => (Number.isSafeInteger(counts?.[type]) ? String(counts[type]) : '');
  const countTitle = (type) =>
    t(
      counts
        ? counts[type] === 1
          ? 'library.view.totalCountOne'
          : 'library.view.totalCount'
        : 'library.view.notLoaded',
      { count: counts?.[type] ?? 0 },
    );
  const uses = (row) =>
    new Set((row.usage || []).map((use) => use.project_id).filter(Boolean)).size;
  const usedText = (row) =>
    uses(row)
      ? t(uses(row) === 1 ? 'library.view.usedInOne' : 'library.view.usedIn', { count: uses(row) })
      : t('library.view.unused');
  const toggle = (row) =>
    setSelected((old) =>
      old.includes(libraryKey(row.target))
        ? old.filter((k) => k !== libraryKey(row.target))
        : [...old, libraryKey(row.target)],
    );
  const picked = rows.filter((r) => selected.includes(libraryKey(r.target))),
    pickedManaged = picked.some(managedRow);
  // New files go to the story being looked at, else the first story.
  const defaultStory = filters.project_id || projects[0]?.id || '';
  const Title = ICONS[filters.type] || Files;
  const sideButton = (k) => {
    const I = ICONS[k];
    return (
      <button
        key={k}
        className={filters.type === k ? 'on' : ''}
        onClick={() => change({ type: k })}
      >
        <I size={14} />
        <span>{label(k)}</span>
        <small title={countTitle(k)}>{count(k)}</small>
      </button>
    );
  };
  if (!account)
    return (
      <div className="home lib-shell">
        <StudioHeader account={account} active="library" />
        <main className="home-main library-signed-out">
          <h1 className="page-title">
            <Files size={20} />
            {t('library.title')}
          </h1>
          <div className="empty-state">
            <span>
              <UserRound size={24} />
            </span>
            <b>{t('library.signIn')}</b>
            <a className="library-sign-in" href="/quick-chat">
              {t('story.copy14')}
            </a>
          </div>
        </main>
      </div>
    );
  return (
    <div className="home lib-shell">
      <StudioHeader account={account} active="library" />
      <div className="libp" style={{ '--lib-side': side.width + 'px' }}>
        <aside className="libp-side">
          <div className="libp-group">{t('library.view.files')}</div>
          {['file', 'image', 'video', 'audio'].map(sideButton)}
          <div className="libp-group">{t('library.view.profiles')}</div>
          {['character', 'location', 'prop'].map(sideButton)}
          <div className="libp-group">{t('library.view.works')}</div>
          <button
            className={!filters.project_id && !filters.unused ? 'on' : ''}
            onClick={() => change({ project_id: '', work: '', unused: '' })}
          >
            {t('library.view.allWorks')}
          </button>
          {projects.map((p) => (
            <button
              key={p.id}
              title={p.title}
              aria-label={p.title}
              className={filters.project_id === p.id ? 'on' : ''}
              onClick={() => change({ project_id: p.id, work: '', unused: '' })}
            >
              <span className="clip" title={p.title}>
                {p.title}
              </span>
            </button>
          ))}
          <button
            className={filters.unused ? 'on' : ''}
            onClick={() => change({ project_id: '', work: '', unused: true })}
          >
            {t('library.view.unused')}
          </button>
          {!!tags.length && (
            <>
              <div className="libp-group">{t('library.tagHeading')}</div>
              <div className="libp-tags">
                {tags.map((tag) => (
                  <button
                    key={tag}
                    className={'tagchip' + (filters.tag === tag ? ' on' : '')}
                    onClick={() => change({ tag: filters.tag === tag ? '' : tag })}
                  >
                    {tag}
                    <small>{rows.filter((r) => r.tags?.includes(tag)).length}</small>
                  </button>
                ))}
              </div>
            </>
          )}
        </aside>
        <div
          className="libp-resize"
          role="separator"
          aria-orientation="vertical"
          aria-label={t('library.resizeSidebar')}
          aria-valuenow={side.width}
          aria-valuemin={SIDE_MIN}
          aria-valuemax={SIDE_MAX}
          tabIndex={0}
          onPointerDown={side.start}
          onKeyDown={side.key}
          onDoubleClick={side.reset}
        />
        <main
          className={'libp-main' + (dropping ? ' is-dropping' : '')}
          data-drop-label={t('library.dropHere', {
            story: projects.find((p) => p.id === defaultStory)?.title || '',
          })}
          onDragOver={(e) => {
            if (profile || ![...e.dataTransfer.types].includes('Files')) return;
            e.preventDefault();
            setDropping(true);
          }}
          onDragLeave={(e) => {
            if (!e.currentTarget.contains(e.relatedTarget)) setDropping(false);
          }}
          onDrop={(e) => {
            setDropping(false);
            if (profile || !e.dataTransfer.files.length) return;
            e.preventDefault();
            if (defaultStory && !locked) onUploadFiles?.([...e.dataTransfer.files], defaultStory);
          }}
        >
          <div className="libp-head">
            <h1 className="page-title">
              <Title size={20} />
              {label(filters.type)}
              <small>{rows.length}</small>
            </h1>
            {(filters.project_id || filters.unused) && (
              <span className="pill">
                {filters.unused
                  ? t('library.view.unused')
                  : projects.find((p) => p.id === filters.project_id)?.title}
                <button
                  className="x"
                  aria-label={t('library.clear')}
                  onClick={() => change({ project_id: '', work: '', unused: '' })}
                >
                  <X size={11} />
                </button>
              </span>
            )}
            {filters.tag && (
              <span className="pill">
                <Tag size={11} />
                {filters.tag}
                <button
                  className="x"
                  aria-label={t('library.clear')}
                  onClick={() => change({ tag: '' })}
                >
                  <X size={11} />
                </button>
              </span>
            )}
            <input
              className="lib-search lib-search-left"
              aria-label={t('library.search')}
              placeholder={t('library.search')}
              value={filters.q || ''}
              onChange={(e) => change({ q: e.target.value })}
            />
            <div className="spacer" />
            {profile ? (
              <CreateProfileMenu
                key={filters.type}
                type={filters.type}
                projects={projects}
                defaultStory={defaultStory}
                disabled={locked}
                onCreate={onCreateProfile}
              />
            ) : (
              <UploadMenu
                projects={projects}
                defaultStory={defaultStory}
                disabled={locked}
                onFiles={onUploadFiles}
              />
            )}
          </div>
          {feedback}
          {picked.length > 0 && (
            <div className="libp-bulk">
              <b>{t('library.selected', { count: picked.length })}</b>
              <PopMenu
                summaryClass="small"
                disabled={locked || pickedManaged}
                title={pickedManaged ? t('library.managedHint') : undefined}
                label={
                  <>
                    <Tag size={13} />
                    {t('library.addTag')}
                  </>
                }
              >
                <form
                  className="lib-pop-new"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const add = bulkTag
                      .split(/[,，]/)
                      .map((s) => s.trim())
                      .filter(Boolean);
                    if (!add.length) return;
                    e.currentTarget.closest('details')?.removeAttribute('open');
                    setBulkTag('');
                    onAddTags(picked, add);
                  }}
                >
                  <input
                    autoFocus
                    list="lib-bulk-tags"
                    value={bulkTag}
                    placeholder={t('library.addTag')}
                    aria-label={t('library.addTag')}
                    onChange={(e) => setBulkTag(e.target.value)}
                  />
                  <datalist id="lib-bulk-tags">
                    {tags.map((s) => (
                      <option key={s} value={s} />
                    ))}
                  </datalist>
                  <button className="small primary" disabled={!bulkTag.trim()}>
                    {t('library.add')}
                  </button>
                </form>
              </PopMenu>
              {!profile && (
                <ReferencePicker
                  items={picked}
                  disabled={locked || pickedManaged}
                  loadProfiles={loadProfiles}
                  onFile={onFileInto}
                  onCreate={onCreateReference}
                />
              )}
              {/* Trash works across stories (one request carries every project's version). */}
              <button className="small danger" disabled={locked} onClick={onDelete}>
                <Trash2 size={13} />
                {t('trash.move')}
              </button>
              <div className="spacer" />
              <button
                className="small ghost"
                onClick={() => setSelected(rows.map((r) => libraryKey(r.target)))}
              >
                {t('library.select')}
              </button>
              <button className="small ghost" onClick={() => setSelected([])}>
                {t('library.clear')}
              </button>
            </div>
          )}
          {loading && rows.length === 0 && (
            <p className="muted" role="status">
              {t('library.loading')}
            </p>
          )}
          <div className={profile ? 'libp-profiles' : 'libp-grid'}>
            {rows.map((row) =>
              profile ? (
                <button
                  key={libraryKey(row.target)}
                  data-lib-key={libraryKey(row.target)}
                  className={'libp-pcard' + (openKey === libraryKey(row.target) ? ' open' : '')}
                  data-ui-surface="card"
                  onClick={() => onDetail(row)}
                >
                  <span className="athumb empty library-profile-thumb">{renderMedia(row)}</span>
                  <span>
                    <b>{row.title}</b>
                    <small
                      className="muted clip"
                      title={row.description || row.source_project_title || row.project_title}
                    >
                      {row.description || row.source_project_title || row.project_title}
                    </small>
                    <small>{usedText(row)}</small>
                  </span>
                </button>
              ) : (
                <div
                  key={libraryKey(row.target)}
                  data-lib-key={libraryKey(row.target)}
                  className={
                    'libp-tile' +
                    (selected.includes(libraryKey(row.target)) ? ' picked' : '') +
                    (openKey === libraryKey(row.target) ? ' open' : '') +
                    (selected.length ? ' selecting' : '')
                  }
                  role="button"
                  aria-label={displayName(row, t)}
                  data-ui-surface="card"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && e.target === e.currentTarget) onDetail(row);
                  }}
                  onClick={(e) =>
                    e.shiftKey || e.metaKey || e.ctrlKey || selected.length
                      ? toggle(row)
                      : onDetail(row)
                  }
                >
                  <button
                    className="libp-check"
                    aria-label={displayName(row, t)}
                    aria-pressed={selected.includes(libraryKey(row.target))}
                    onClick={(e) => {
                      e.stopPropagation();
                      toggle(row);
                    }}
                  >
                    {selected.includes(libraryKey(row.target)) && <Check size={12} />}
                  </button>
                  {row.type === 'audio' ? (
                    <div className="libp-audio">
                      <Music size={24} aria-hidden="true" />
                    </div>
                  ) : (
                    renderMedia(row)
                  )}
                  <span
                    className="libp-name"
                    onClick={(e) => selected.length || e.stopPropagation()}
                  >
                    <InlineName
                      value={displayName(row, t)}
                      disabled={locked || !!selected.length || managedRow(row)}
                      onSave={(title) => onRename(row, title)}
                    />
                  </span>
                  <span className="libp-meta">
                    {usedText(row)}
                    {row.tags?.length ? ' · ' + row.tags.join(' · ') : ''}
                  </span>
                </div>
              ),
            )}
          </div>
          {!loading && !rows.length && (
            <p className="muted">{t(profile ? 'library.noneProfile' : 'library.noneFiles')}</p>
          )}
          {next !== null && (
            <button disabled={loading || locked} onClick={onMore}>
              {t('library.more')}
            </button>
          )}
        </main>
        {children}
      </div>
    </div>
  );
}
