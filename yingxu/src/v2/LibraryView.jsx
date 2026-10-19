// View ported from design/inkseq-v2-prototype/src/library.jsx.
// All ownership, uploads, commands and receipts are supplied by Library.jsx.
import React from 'react';
import {
  Box,
  Check,
  Files,
  Film,
  Image as ImageIcon,
  Inbox,
  MapPin,
  Music,
  SlidersHorizontal,
  Tag,
  Trash2,
  Upload,
  UserRound,
  X,
} from 'lucide-react';
import StudioHeader from '../StudioHeader.jsx';
import { useI18n } from '../i18n/I18nProvider.jsx';
import './library-adapter.css';
const ICONS = {
  character: UserRound,
  location: MapPin,
  prop: Box,
  file: Files,
  image: ImageIcon,
  video: Film,
  audio: Music,
  inbox: Inbox,
};

export default function LibraryView({
  account,
  filters = { type: 'image' },
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
  onUpload,
  showUpload,
  uploadPanel,
  onRefresh,
  onTags,
  onFile,
  onDelete,
  onDetail,
  openKey,
  renderMedia,
  feedback,
  children,
}) {
  const { t } = useI18n(),
    profile = ['character', 'location', 'prop'].includes(filters.type),
    tags = [...new Set(rows.flatMap((r) => r.tags || []))];
  const change = (patch) => setFilters({ ...filters, ...patch });
  const label = (type) =>
    t(
      ['image', 'video', 'audio'].includes(type)
        ? 'library.view.' + type
        : type === 'file'
          ? 'library.allFiles'
          : type === 'inbox'
            ? 'library.view.inbox'
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
  const toggle = (row) =>
    setSelected((old) =>
      old.includes(libraryKey(row.target))
        ? old.filter((k) => k !== libraryKey(row.target))
        : [...old, libraryKey(row.target)],
    );
  const Title = ICONS[filters.type] || Files;
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
      <div className="libp">
        <aside className="libp-side">
          <div className="libp-group">{t('library.view.profiles')}</div>
          {['character', 'location', 'prop'].map((k) => {
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
          })}
          <div className="libp-group">{t('library.view.files')}</div>
          {['file', 'image', 'video', 'audio'].map((k) => {
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
          })}
          <button
            className={filters.type === 'inbox' ? 'on' : ''}
            onClick={() => change({ type: 'inbox' })}
          >
            <Inbox size={14} />
            <span>{t('library.view.inbox')}</span>
            <small title={countTitle('inbox')}>{count('inbox')}</small>
          </button>
          <div className="libp-group">
            {t('library.view.works')} <small>{t('library.view.workHint')}</small>
          </div>
          <button
            className={!filters.work && !filters.unused ? 'on' : ''}
            onClick={() => change({ work: '', unused: '' })}
          >
            {t('library.view.allWorks')}
          </button>
          {projects.map((p) => (
            <button
              key={p.id}
              title={p.title}
              aria-label={p.title}
              className={filters.work === p.id ? 'on' : ''}
              onClick={() => change({ work: p.id, unused: '' })}
            >
              <span className="clip" title={p.title}>
                {p.title}
              </span>
            </button>
          ))}
          <button
            className={filters.unused ? 'on' : ''}
            onClick={() => change({ work: '', unused: true })}
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
          <details className="v2-library-tools library-source-tools">
            <summary>
              <SlidersHorizontal size={14} />
              <span>{t('library.source')}</span>
            </summary>
            <div className="card-box">
              <label className="field">
                <span>{t('library.source')}</span>
                <select
                  value={filters.project_id || ''}
                  onChange={(e) => change({ project_id: e.target.value })}
                >
                  <option value="">{t('library.all')}</option>
                  {projects.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.title}
                    </option>
                  ))}
                </select>
              </label>
              <p className="muted">{t('library.scope')}</p>
              <button disabled={loading} onClick={onRefresh}>
                {t('library.refresh')}
              </button>
            </div>
          </details>
        </aside>
        <main
          className="libp-main"
          onDragOver={(e) => {
            if ([...e.dataTransfer.types].includes('Files')) e.preventDefault();
          }}
          onDrop={(e) => {
            e.preventDefault();
            if (account) onUpload?.(e.dataTransfer.files);
          }}
        >
          <div className="libp-head">
            <h1 className="page-title">
              <Title size={20} />
              {label(filters.type)}
              <small>{rows.length}</small>
            </h1>
            {(filters.work || filters.unused) && (
              <span className="pill">
                {filters.unused
                  ? t('library.unused')
                  : projects.find((p) => p.id === filters.work)?.title}
                <button
                  className="x"
                  aria-label={t('library.clear')}
                  onClick={() => change({ work: '', unused: '' })}
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
            <div className="spacer" />
            <input
              className="lib-search"
              aria-label={t('library.search')}
              placeholder={t('library.search')}
              value={filters.q || ''}
              disabled={!account}
              onChange={(e) => change({ q: e.target.value })}
            />
            <button className="primary" disabled={!account || locked} onClick={() => onUpload?.()}>
              <Upload size={15} />
              {t('v2.upload')}
            </button>
          </div>
          {feedback}
          <>
            {showUpload && uploadPanel}
            {selected.length > 0 && (
              <div className="libp-bulk">
                <b>{t('library.selected', { count: selected.length })}</b>
                <button
                  disabled={locked || !filters.project_id}
                  title={filters.project_id ? undefined : t('library.pickStoryFirst')}
                  onClick={onTags}
                >
                  <Tag size={14} />
                  {t('library.applyTags')}
                </button>
                <button
                  disabled={locked || !filters.project_id}
                  title={filters.project_id ? undefined : t('library.pickStoryFirst')}
                  onClick={onFile}
                >
                  {t('library.fileInto')}
                </button>
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
                    className="libp-pcard"
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
                      <small>
                        {uses(row)
                          ? t(uses(row) === 1 ? 'library.view.usedInOne' : 'library.view.usedIn', {
                              count: uses(row),
                            })
                          : t('library.view.unused')}
                      </small>
                    </span>
                  </button>
                ) : (
                  <div
                    key={libraryKey(row.target)}
                    className={
                      'libp-tile' +
                      (selected.includes(libraryKey(row.target)) ? ' picked' : '') +
                      (openKey === libraryKey(row.target) ? ' open' : '')
                    }
                    role="button"
                    aria-label={row.title || t('library.receiptName')}
                    data-ui-surface="card"
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') onDetail(row);
                    }}
                    onClick={(e) =>
                      e.shiftKey || e.metaKey || e.ctrlKey || selected.length
                        ? toggle(row)
                        : onDetail(row)
                    }
                  >
                    <button
                      className="libp-check"
                      aria-label={row.title}
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
                    <span className="libp-name clip" title={row.title || t('library.receiptName')}>
                      {row.title || t('library.receiptName')}
                    </span>
                    <span className="libp-meta">
                      {uses(row)
                        ? t(uses(row) === 1 ? 'library.view.usedInOne' : 'library.view.usedIn', {
                            count: uses(row),
                          })
                        : t('library.view.unused')}
                      {row.tags?.length ? ' · ' + row.tags.join(' · ') : ''}
                    </span>
                  </div>
                ),
              )}
            </div>
            {!loading && !rows.length && <p className="muted">{t('library.none')}</p>}
            {next !== null && (
              <button disabled={loading || locked} onClick={onMore}>
                {t('library.more')}
              </button>
            )}
            {!profile && (
              <p className="muted libp-tip">
                {t('library.view.tip')} <span>{t('library.usageNote')}</span>
              </p>
            )}
          </>
        </main>
        {children}
      </div>
    </div>
  );
}
