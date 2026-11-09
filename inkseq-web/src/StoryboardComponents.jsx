import { Disclosure } from './ui/Disclosure.jsx';
import { TakeNextStep } from './TakeNextStep.jsx';
import {
  UserRound,
  MapPin,
  Box,
  Image as ImageIcon,
  Film,
  Music,
  Inbox,
  Circle,
  CheckCircle2,
  Play,
  Pause,
  SkipBack,
  Plus,
  X,
  Search,
  Maximize2,
  Minimize2,
  Check,
  Pin,
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Upload,
  Pencil,
  CircleHelp,
  GripVertical,
} from 'lucide-react';
import { Segmented } from './ui/Segmented.jsx';
import { Button, IconButton } from './ui/Button.jsx';
import { FilterChip } from './ui/FilterChip.jsx';
import { Field } from './ui/Field.jsx';
import { Dialog, DialogActions } from './ui/Dialog.jsx';
import { Tooltip } from './ui/Tooltip.jsx';
import { useDragMove } from './ui/useDragMove.js';
import { Badge, Chip, Note } from './ui/Note.jsx';
import './profile-editor.css';
import { TrashMenu, TrashButton, RenameDialog } from './UniversalTrash.jsx';
import { WorkbenchTakes, WorkbenchCopies } from './StoryboardWorkbench.jsx';
import { AssistantBadge } from './AssistantConnections.jsx';
import { useI18n } from './i18n/I18nProvider.jsx';
import React, {
  useEffect,
  useEffectEvent,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import { useOptimisticChoice } from './optimistic-choice.js';
import { useQuery } from '@tanstack/react-query';
import { openKey, useCardGrid } from './card-grid.js';
import { assetLabel, secondsLabel, shotName } from './storyboard-a11y.js';
import { sceneBeats } from './script-beats.js';
import { planChecklist, planOf } from './shot-plan.js';
import { highlightGuideField } from './guide.js';
import { store } from './store.js';
import { cutFps, videoCut } from './video-cut-model.js';
import { trimClip } from './film-edit-model.js';
import TrimBar from './TrimBar.jsx';
import ShotSegments from './ShotSegments.jsx';
import TakeReview from './TakeReview.jsx';
import ScriptWindows from './ScriptWindows.jsx';
import { segmentsTotal, sourceTime } from './shot-segments.js';
import { createPortal } from 'react-dom';
import { useDraggable, useDroppable } from '@dnd-kit/core';
import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import {
  StoryboardDownloads,
  StoryboardAssetImage,
  StoryboardVideo,
  StoryboardPlayButton,
} from './StoryboardMedia.jsx';
import {
  chosenVersion,
  coverOf,
  expand,
  fmt,
  planDrop,
  segments,
  shotStatus,
  startFrame,
  variantOf,
  queuePositions,
} from './storyboard-ui-model.js';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';
import { ShotPicture, useDrawingPicture } from './ShotPicture.jsx';
import { ShotLook } from './ShotLook.jsx';
import { ShotCardPrompt } from './ShotCardPrompt.jsx';
import { VoicePicker } from './VoicePicker.jsx';
import { BibleDots, ProfileBible } from './ProfileBible.jsx';
import { BibleImportExport } from './BibleImport.jsx';
import { characterGroups } from './story-bible.js';
import { ShotCardPlan, ShotPlanPanel } from './ShotPlan.jsx';
import { useShotReadiness } from './shot-readiness.js';
import { StoryboardNameDialog } from './StoryboardDialog.jsx';
import { createStoryboardPlayback } from './storyboard-playback.js';

function displayLabels(tr) {
  return {
    TYPE_LABEL: {
      character: tr('storyboard.labels.TYPE_LABEL.character'),
      location: tr('storyboard.labels.TYPE_LABEL.location'),
      prop: tr('storyboard.labels.TYPE_LABEL.prop'),
    },
    VARIANT_LABEL: {
      character: tr('storyboard.labels.VARIANT_LABEL.character'),
      location: tr('storyboard.labels.VARIANT_LABEL.location'),
      prop: tr('storyboard.labels.VARIANT_LABEL.prop'),
    },
    KIND_LABEL: {
      image: tr('storyboard.labels.KIND_LABEL.image'),
      video: tr('storyboard.labels.KIND_LABEL.video'),
      audio: tr('storyboard.labels.KIND_LABEL.audio'),
    },
    MODE_LABEL: {
      FL: tr('storyboard.labels.MODE_LABEL.FL'),
      REF: tr('storyboard.labels.MODE_LABEL.REF'),
    },
    QUALITY_LABEL: {
      draft: tr('storyboard.labels.QUALITY_LABEL.draft'),
      final: tr('storyboard.labels.QUALITY_LABEL.final'),
    },
  };
}
/* ---------- thumbs & draggable tiles ---------- */

export function AssetThumb({ asset, size = 36 }) {
  if (!asset) return <span className="athumb empty" style={{ width: size, height: size }} />;
  if (asset.kind === 'audio')
    return (
      <span className="athumb audio" style={{ width: size, height: size }}>
        <Music size={16} />
      </span>
    );
  return (
    <StoryboardAssetImage asset={asset} className="athumb" style={{ width: size, height: size }} />
  );
}

// A library file. Enter or Space opens its preview; the mouse drags it onto a shot or profile
// (the full-page library has nothing to drop on, so it does not drag there).
function Tile({ asset, used, onClick, onZoom, dragId, draggable = true, gridCard, children }) {
  const { state, dispatch } = useStore();
  const [renaming, setRenaming] = useState(false);
  const { t: tr } = useI18n();
  const drag = useDraggable({ id: dragId || 'asset:' + asset.id, disabled: !draggable });
  const label = assetLabel(asset, tr, { full: true }),
    length = secondsLabel(asset.seconds, tr);
  return (
    <div
      ref={drag.setNodeRef}
      onPointerDown={draggable ? drag.listeners?.onPointerDown : undefined}
      role="group"
      aria-label={label}
      tabIndex={0}
      data-grid-card={gridCard ? asset.id : undefined}
      className={
        'tile' +
        (drag.isDragging ? ' dragging' : '') +
        (used ? ' used' : '') +
        (draggable ? '' : ' still')
      }
      onClick={onClick}
      onKeyDown={onZoom ? (event) => openKey(event, () => onZoom(asset)) : undefined}
      title={draggable ? tr('storyboard.mixed1', { p0: label }) : label}
    >
      {asset.kind === 'audio' ? (
        <div className="tile-audio">
          <Music size={22} />
          {length && <small>{length}</small>}
        </div>
      ) : (
        <StoryboardAssetImage asset={asset} />
      )}
      {asset.kind === 'video' && length && (
        <span className="tile-badge">
          <Play size={11} /> {length}
        </span>
      )}
      {asset.kind === 'audio' && (
        <span className="tile-play">
          <PlayButton asset={asset} />
        </span>
      )}
      {asset.source === 'ai' && <span className="tile-badge ai">AI</span>}
      {['frame', 'artifact_frame'].includes(asset.source) && !asset.use && (
        <span className="tile-badge">{tr('storyboard.copy37')}</span>
      )}
      {used && (
        <span className="tile-used">
          <Check size={12} />
        </span>
      )}
      <span className="tile-name">{assetLabel(asset, tr)}</span>
      {onZoom && asset.kind !== 'audio' && (
        <Button
          className="tile-zoom"
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => {
            e.stopPropagation();
            onZoom(asset);
          }}
          aria-label={tr('storyboard.zoomAsset')}
        >
          <Maximize2 size={14} />
        </Button>
      )}
      {asset.entity?.id && (
        <TrashMenu
          target={{ kind: 'entity', project_id: state.project.id, id: asset.entity.id }}
          onRename={() => setRenaming(true)}
          onEdit={() => setRenaming(true)}
        />
      )}
      {renaming && (
        <RenameDialog
          key={state.contextId + ':' + asset.id}
          title={asset.name}
          onClose={() => setRenaming(false)}
          onSave={async (title) => {
            const result = await dispatch({ type: 'assetTitle', asset: asset.id, title });
            if (result?.ok === false || result === false) throw Error('trash.changed');
          }}
        />
      )}
      {children}
    </div>
  );
}

// A person, place or prop. Enter or Space opens it; it drags onto a shot from its handle only.
function ProfileCard({ p, used, onOpen, onAdd, draggable = true }) {
  const { t: tr } = useI18n();
  const { TYPE_LABEL } = displayLabels(tr);
  const { db, state } = useStore();
  const drag = useDraggable({
    id: 'profile:' + p.id,
    disabled: !draggable,
    attributes: { roleDescription: tr('dnd.roleDescription') },
  });
  const drop = useDroppable({ id: 'pdrop:' + p.id });
  const cover = coverOf(db, p, p.def);
  const count = p.variants.reduce((n, v) => n + v.images.length, 0);
  const dragged =
    drop.isOver && String(drop.active?.id).startsWith('asset:')
      ? db.asset(String(drop.active.id).slice(6))
      : null;
  const fileOver = !!dragged;
  const hint = !dragged
    ? ''
    : dragged.kind === 'image'
      ? tr('storyboard.mixed2', { p0: p.name })
      : dragged.kind === 'audio'
        ? p.type === 'character'
          ? tr('storyboard.mixed3', { p0: p.name })
          : p.type === 'location'
            ? tr('storyboard.detail1')
            : tr('storyboard.detail2')
        : tr('storyboard.detail3');
  return (
    <div
      ref={(n) => {
        drag.setNodeRef(n);
        drop.setNodeRef(n);
      }}
      role="group"
      aria-label={tr('library.profileCard', { type: TYPE_LABEL[p.type], name: p.name })}
      data-grid-card={p.id}
      onKeyDown={(event) => openKey(event, onOpen)}
      className={
        'pcard ' +
        p.type +
        (drag.isDragging ? ' dragging' : '') +
        (used ? ' used' : '') +
        (fileOver ? ' drop-hot' : '')
      }
      onClick={onOpen}
      title={tr('storyboard.detail82')}
    >
      {cover ? (
        <AssetThumb asset={cover} size={38} />
      ) : (
        <span className="profile-initial" aria-hidden="true">
          {Array.from((p.name || tr('storyboard.detail4')).trim())[0]?.toLocaleUpperCase() ||
            tr('storyboard.detail5')}
        </span>
      )}
      <div className="pcard-body">
        <b title={p.name}>
          {p.name}
          {p.bible?.speaker && <span className="bible-speaker">{p.bible.speaker}</span>}
        </b>
        <BibleDots p={p} used={used} />
        <small>
          {p.type !== 'prop' && (
            <>
              {tr(
                'storyboard.' +
                  (p.type === 'character'
                    ? p.variants.length === 1
                      ? 'oneLook'
                      : 'looks'
                    : p.variants.length === 1
                      ? 'oneVersion'
                      : 'versions'),
                { count: p.variants.length },
              )}
              {' · '}
            </>
          )}
          {tr(count === 1 ? 'storyboard.oneImage' : 'storyboard.images', { count })}
          {!!p.voices?.length && (
            <>
              {' · '}
              {tr(p.voices.length === 1 ? 'storyboard.oneVoice' : 'storyboard.voices', {
                count: p.voices.length,
              })}
            </>
          )}
          {p.ambient ? tr('storyboard.detail6') : ''}
        </small>
        <small className="muted clip">{p.desc}</small>
      </div>
      {(draggable || onAdd) && (
        <span className="pcard-actions">
          {draggable && (
            <IconButton
              size="xs"
              className="pcard-grip"
              data-drag-handle
              ref={drag.setActivatorNodeRef}
              {...drag.attributes}
              {...drag.listeners}
              label={tr('dnd.dragProfile', { name: p.name })}
              onClick={(e) => e.stopPropagation()}
            >
              <GripVertical size={14} aria-hidden="true" />
            </IconButton>
          )}
          {onAdd && (
            <Button
              className="pcard-open"
              aria-label={
                used
                  ? tr('storyboard.mixed7', { p0: p.name })
                  : tr('storyboard.mixed8', { p0: p.name })
              }
              title={used ? tr('storyboard.detail7') : tr('storyboard.detail8')}
              onPointerDown={(e) => e.stopPropagation()}
              onClick={(e) => {
                e.stopPropagation();
                onAdd();
              }}
            >
              {used ? <Check size={14} /> : <Plus size={14} />}
            </Button>
          )}
        </span>
      )}
      <TrashMenu
        target={{ kind: 'entity', project_id: state.project.id, id: p.id }}
        onRename={onOpen}
        onEdit={onOpen}
      />
      {fileOver && (
        <div
          className={
            'drop-hint' +
            (dragged.kind === 'video' || (dragged.kind === 'audio' && p.type === 'prop')
              ? ' warn'
              : '')
          }
        >
          {hint}
        </div>
      )}
    </div>
  );
}

/* ---------- audio playback ---------- */

const PlayButton = StoryboardPlayButton;
// Preserve actual File objects for the normal /v1/assets upload adapter.
export async function readFiles(fileList) {
  return [...fileList];
}
const nativeDrop = (onFiles) => ({
  onDragOver: (e) => {
    if ([...e.dataTransfer.types].includes('Files')) {
      e.preventDefault();
      e.currentTarget.classList.add('drop-hot');
    }
  },
  onDragLeave: (e) => e.currentTarget.classList.remove('drop-hot'),
  onDrop: async (e) => {
    if (!e.dataTransfer.files.length) return;
    e.preventDefault();
    e.currentTarget.classList.remove('drop-hot');
    onFiles(await readFiles(e.dataTransfer.files));
  },
});

// Choose files already in the library, or bring new ones from the computer.
function ChooseFiles({ kind, exclude, title, onPick, onFiles, onClose }) {
  const { t: tr } = useI18n();
  const { state } = useStore();
  const [q, setQ] = useState('');
  const input = useRef();
  const list = state.assets.filter(
    (a) => a.kind === kind && !exclude.has(a.id) && (!q || a.name.includes(q)),
  );
  // Its own modal on top of whatever opened it (e.g. the profile editor), via ui/Dialog.
  return (
    <Dialog
      title={title}
      onClose={onClose}
      portal
      wide
      className="sb-portal density-compact pe-choose"
    >
      <div
        className="pe-choose-body"
        {...nativeDrop((files) => {
          onFiles(files);
          onClose();
        })}
      >
        <div className="pe-row">
          <input
            className="pe-grow"
            type="search"
            aria-label={tr('storyboard.finish1')}
            placeholder={tr('storyboard.finish1')}
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <Button variant="primary" onClick={() => input.current.click()}>
            {tr('storyboard.copy38')}
          </Button>
          <input
            ref={input}
            type="file"
            multiple
            hidden
            accept={kind === 'image' ? 'image/*' : 'audio/*'}
            onChange={async (e) => {
              onFiles(await readFiles(e.target.files));
              onClose();
            }}
          />
        </div>
        <Note as="div" tone="info">
          {tr('storyboard.copy39')}
        </Note>
        {kind === 'image' ? (
          // One square per file, same size whether its picture has loaded, is still waiting or
          // has none: images show themselves, videos their poster or first frame, sounds an icon.
          <div className="pe-choose-grid">
            {list.map((a) => {
              const name = assetLabel(a, tr);
              return (
                <Button
                  key={a.id}
                  className="pe-choose-tile"
                  onClick={() => onPick(a.id)}
                  title={name}
                  aria-label={name}
                >
                  <span className="pe-choose-thumb" aria-hidden="true">
                    {a.kind === 'audio' ? (
                      <Music size={20} />
                    ) : (
                      <StoryboardAssetImage asset={a} alt="" />
                    )}
                  </span>
                  <small className="clip">{name}</small>
                </Button>
              );
            })}
          </div>
        ) : (
          <div className="pe-choose-list">
            {list.map((a) => (
              <div key={a.id} className="pe-row">
                <PlayButton asset={a} />
                <span className="pe-grow clip">{assetLabel(a, tr)}</span>
                <small className="muted">{secondsLabel(a.seconds, tr)}</small>
                <Button size="sm" onClick={() => onPick(a.id)}>
                  {tr('storyboard.copy40')}
                </Button>
              </div>
            ))}
          </div>
        )}
        {!list.length && (
          <p className="muted">
            {tr('storyboard.finish14', {
              kind: kind === 'image' ? tr('storyboard.detail9') : tr('storyboard.copy51'),
            })}
          </p>
        )}
      </div>
    </Dialog>
  );
}

/* ---------- library ---------- */

// The panel beside the storyboard holds everything, so any of it can be dragged onto a shot. As a
// whole page it holds one half: page 'world' the people, places and props, page 'files' the files.
export function AssetLibrary({
  onPick,
  selectedShot,
  compact,
  collapsed = false,
  onToggle,
  page = null,
}) {
  const { t: tr } = useI18n();
  const { TYPE_LABEL } = displayLabels(tr);
  const ALL_TABS = [
    ['character', tr('storyboard.detail10'), UserRound],
    ['location', tr('storyboard.detail11'), MapPin],
    ['prop', tr('storyboard.copy88'), Box],
    ['image', tr('storyboard.detail9'), ImageIcon],
    ['video', tr('library.tab.video'), Film],
    ['audio', tr('storyboard.copy51'), Music],
    ['inbox', tr('storyboard.detail13'), Inbox],
  ];
  const LIB_TABS = ALL_TABS.filter(
    ([k]) => !page || ['character', 'location', 'prop'].includes(k) === (page === 'world'),
  );

  const { state, dispatch, db } = useStore();
  // Each page keeps its own category and search.
  const place = page || 'panel',
    [tabs, setTabs] = useState({}),
    [searches, setSearches] = useState({}),
    tab = tabs[place] || LIB_TABS[0][0],
    q = searches[place] || '',
    setTab = (value) => setTabs((old) => ({ ...old, [place]: value })),
    setQ = (value) => setSearches((old) => ({ ...old, [place]: value }));
  const [wide, setWide] = useState(false),
    [zoom, setZoom] = useState(null),
    [newProfileType, setNewProfileType] = useState(null);
  const uploadInput = useRef();
  // Arrow keys between profile cards and between files (card-grid.js).
  const profileGrid = useCardGrid('.pcard[data-grid-card]'),
    fileGrid = useCardGrid('.tile[data-grid-card]');
  const inbox = state.assets.filter((a) => a.inbox);
  const count = (k) =>
    k === 'inbox'
      ? inbox.length
      : ['character', 'location', 'prop'].includes(k)
        ? state.profiles.filter((p) => p.type === k).length
        : state.assets.filter((a) => a.kind === k && !a.inbox).length;
  const match = (s) => !q || s.includes(q);
  const usedP = new Set(
    selectedShot
      ? [...selectedShot.cast.map((c) => c.id), selectedShot.place?.id, ...selectedShot.props]
      : [],
  );
  const profileFiles = selectedShot
    ? [
        ...selectedShot.cast.map((c) => ({ p: db.profile(c.id), variant: c.variant })),
        ...(selectedShot.place
          ? [{ p: db.profile(selectedShot.place.id), variant: selectedShot.place.variant }]
          : []),
        ...selectedShot.props.map((id) => ({ p: db.profile(id) })),
      ].flatMap(({ p, variant }) =>
        p ? [...(variantOf(p, variant)?.images || []), p.voice, p.ambient].filter(Boolean) : [],
      )
    : [];
  const usedA = new Set(
    selectedShot
      ? [...selectedShot.refs, selectedShot.start, selectedShot.end, ...profileFiles].filter(
          Boolean,
        )
      : [],
  );
  const isProfileTab = ['character', 'location', 'prop'].includes(tab);
  const profileCount = tr(
      state.profiles.length === 1 ? 'storyboard.oneProfile' : 'storyboard.profiles',
      { count: state.profiles.length },
    ),
    fileCount = tr(state.assets.length === 1 ? 'storyboard.oneFile' : 'storyboard.files', {
      count: state.assets.length,
    });
  const newProfileLabel = tr('storyboard.mixed16', { kind: TYPE_LABEL[tab] }),
    heading = tr(
      page === 'world' ? 'stage.world' : page === 'files' ? 'stage.library' : 'storyboard.copy41',
    );
  return (
    <aside
      className={
        'library' +
        (wide ? ' wide' : '') +
        (compact ? ' compact' : '') +
        (collapsed ? ' collapsed' : '')
      }
      aria-label={heading}
    >
      <div className="lib-head">
        {!collapsed && (
          <>
            <b>{heading}</b>
            <small>
              {page === 'world'
                ? profileCount
                : page === 'files'
                  ? fileCount
                  : tr('storyboard.libraryCounts', { profiles: profileCount, assets: fileCount })}
            </small>
          </>
        )}
        {page ? null : onToggle ? (
          <Button
            variant="ghost"
            size="sm"
            className="library-toggle"
            aria-label={collapsed ? tr('storyboard.detail14') : tr('storyboard.detail15')}
            aria-expanded={!collapsed}
            onClick={onToggle}
            title={collapsed ? tr('storyboard.detail14') : tr('storyboard.detail15')}
          >
            {collapsed ? <ChevronRight size={15} /> : <ChevronLeft size={15} />}
          </Button>
        ) : (
          <Button variant="ghost" size="sm" onClick={() => setWide(!wide)}>
            {wide ? tr('storyboard.detail16') : tr('storyboard.detail17')}
          </Button>
        )}
      </div>
      <div className="row tight">
        {page === 'world' ? (
          <Button size="sm" onClick={() => setNewProfileType(tab)}>
            <Plus size={14} />
            {newProfileLabel}
          </Button>
        ) : (
          <Button size="sm" onClick={() => uploadInput.current.click()} disabled={state.busy}>
            <Upload size={14} />
            {tr('storyboard.copy42')}
          </Button>
        )}
        {isProfileTab && !page && (
          // #232 (#8): one "+" that creates whatever category is open.
          <Button
            variant="ghost"
            size="sm"
            className="lib-new"
            disabled={state.busy}
            title={newProfileLabel}
            aria-label={newProfileLabel}
            onClick={() => setNewProfileType(tab)}
          >
            <Plus size={14} />
          </Button>
        )}
        <input
          ref={uploadInput}
          type="file"
          hidden
          multiple
          accept="image/*,video/*,audio/*"
          onChange={(event) => {
            dispatch({ type: 'uploadFiles', files: [...event.target.files] });
            event.target.value = '';
          }}
        />
      </div>
      <input
        className="lib-search"
        aria-label={tr(
          page === 'world'
            ? 'stage.searchWorld'
            : page === 'files'
              ? 'stage.searchFiles'
              : 'storyboard.detail72',
        )}
        placeholder={
          page === 'world'
            ? tr('stage.searchWorld')
            : page === 'files'
              ? tr('stage.searchFiles')
              : tr('storyboard.detail73')
        }
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      <div className="lib-tabs">
        {LIB_TABS.map(([k, l, Icon]) => (
          <FilterChip
            key={k}
            active={tab === k}
            alert={k === 'inbox' && !!inbox.length}
            iconOnly={collapsed}
            icon={Icon}
            label={l}
            count={count(k)}
            onClick={() => {
              setTab(k);
              // On the icon rail a category opens the library at that category.
              if (collapsed) onToggle?.();
            }}
            title={l}
            aria-label={l}
          />
        ))}
      </div>

      {isProfileTab && (
        <>
          <div
            className="plist"
            ref={profileGrid}
            onContextMenu={(event) => {
              // Right-click empty list space: create one of this category (a shortcut to "+").
              if (event.target.closest('.pcard') || state.busy) return;
              event.preventDefault();
              setNewProfileType(tab);
            }}
          >
            {!state.profiles.some((p) => p.type === tab) && (
              <div className="lib-empty">
                <span className="muted">
                  {tr('storyboard.libEmpty', { kind: TYPE_LABEL[tab] })}
                </span>
                <Button size="sm" disabled={state.busy} onClick={() => setNewProfileType(tab)}>
                  <Plus size={14} />
                  {newProfileLabel}
                </Button>
              </div>
            )}
            {characterGroups(
              state.profiles.filter((p) => p.type === tab && match(p.name + p.desc)),
            ).map(({ group, members }) => {
              const cards = members.map((p) => (
                <ProfileCard
                  key={p.id}
                  p={p}
                  draggable={!page}
                  used={usedP.has(p.id)}
                  onOpen={() => dispatch({ type: 'openProfile', id: p.id })}
                  onAdd={selectedShot ? () => onPick({ profile: p.id }) : null}
                />
              ));
              // People who always appear together (三兄弟) stay one card each, grouped.
              return group ? (
                <div key={'group:' + group} className="bible-group">
                  <div className="bible-group-head">
                    <b>{group}</b>
                    <small className="muted">
                      {tr('bible.groupCount', { count: members.length })}
                    </small>
                  </div>
                  {cards}
                </div>
              ) : (
                cards[0]
              );
            })}
          </div>
          {page === 'world' && <BibleImportExport />}
          <div className="lib-tip">
            {tab === 'character'
              ? tr(page === 'world' ? 'stage.worldCharacterTip' : 'storyboard.detail18')
              : tab === 'location'
                ? tr('storyboard.detail19')
                : tr('storyboard.detail20')}
            {tr(page === 'world' ? 'stage.worldTip' : 'storyboard.mixed20')}
          </div>
        </>
      )}

      {['image', 'video', 'audio'].includes(tab) && (
        <div className="grid" ref={fileGrid}>
          {state.assets
            .filter((a) => a.kind === tab && !a.inbox && match(a.name + ' ' + assetLabel(a, tr)))
            // Files a shot uses come in shot and take order; the rest keep their own order.
            .sort(
              (a, b) =>
                (a.use?.shot ?? Infinity) - (b.use?.shot ?? Infinity) ||
                (a.use?.take ?? 0) - (b.use?.take ?? 0),
            )
            .map((a) => (
              <Tile
                key={a.id}
                asset={a}
                gridCard
                // The full-page library has no shot or profile to drop on (audit 2026-11-09).
                draggable={!page}
                used={usedA.has(a.id)}
                // On its own page there is no shot to add to: a click just looks at the file.
                onClick={page ? () => setZoom(a) : () => onPick?.({ asset: a.id })}
                onZoom={setZoom}
              />
            ))}
        </div>
      )}

      {tab === 'inbox' && (
        <div className="inbox">
          {!page && (
            <Button size="sm" onClick={() => uploadInput.current.click()} disabled={state.busy}>
              <Upload size={14} />
              {tr('storyboard.copy44')}
            </Button>
          )}
          {!inbox.length && <p className="muted">{tr('storyboard.copy45')}</p>}
          {inbox.map((a) => (
            <InboxRow key={a.id} a={a} />
          ))}
        </div>
      )}
      {zoom && (
        // A real dialog: Escape or a click outside closes it and focus goes back to the file.
        <Dialog
          title={assetLabel(zoom, tr, { full: true })}
          eyebrow={tr('storyboard.detail74')}
          onClose={() => setZoom(null)}
          portal
          wide
          className="sb-portal density-compact lib-preview"
        >
          <div className="lib-preview-media">
            {zoom.kind === 'video' ? (
              <StoryboardVideo asset={zoom} controls />
            ) : zoom.kind === 'audio' ? (
              <PlayButton asset={zoom} label={tr('library.play')} />
            ) : (
              <StoryboardAssetImage asset={zoom} />
            )}
          </div>
          {selectedShot && onPick && (
            <DialogActions>
              <Button
                variant="primary"
                onClick={() => {
                  onPick({ asset: zoom.id });
                  setZoom(null);
                }}
              >
                {tr('library.addToShot', {
                  shot: shotName(selectedShot, state.shots.indexOf(selectedShot), tr),
                })}
              </Button>
            </DialogActions>
          )}
        </Dialog>
      )}
      {newProfileType && (
        <StoryboardNameDialog
          title={tr('storyboard.mixed9', { p0: TYPE_LABEL[newProfileType] })}
          label={tr('storyboard.mixed10', { p0: TYPE_LABEL[newProfileType] })}
          onSubmit={(name) => dispatch({ type: 'newProfile', ptype: newProfileType, name })}
          onClose={() => setNewProfileType(null)}
        />
      )}
    </aside>
  );
}

function InboxRow({ a }) {
  const { t: tr } = useI18n();
  const { TYPE_LABEL } = displayLabels(tr);
  const { state, dispatch, db } = useStore();
  const sug = a.suggest && db.profile(a.suggest.pid || a.suggest.id);
  const [target, setTarget] = useState(sug ? `${sug.id}|${a.suggest.vid || sug.def}` : '');
  // Only pictures and loose sounds can be filed to a profile; a video or a shot's dialogue
  // track gets no picker and no grey "File" button (audit 2026-11-09).
  const fileable = a.kind === 'image' || (a.kind === 'audio' && !a.use);
  return (
    <div className="inrow">
      <Tile asset={a} />
      <div className="inrow-body">
        <small className="clip">{assetLabel(a, tr, { full: true })}</small>
        {!fileable ? null : sug ? (
          <small className="sug">{tr('storyboard.finish15', { name: sug.name })}</small>
        ) : (
          <small className="muted">{tr('storyboard.copy47')}</small>
        )}
        {fileable && (
          <select
            className="field-sm"
            aria-label={a.name + tr('storyboard.detail21')}
            value={target}
            onChange={(e) => setTarget(e.target.value)}
          >
            <option value="">{tr('storyboard.copy48')}</option>
            {state.profiles
              .filter(
                (p) =>
                  a.kind === 'image' ||
                  (a.kind === 'audio' && ['character', 'location'].includes(p.type)),
              )
              .map((p) => (
                <optgroup key={p.id} label={`${TYPE_LABEL[p.type]} · ${p.name}`}>
                  {p.variants.map((v) => (
                    <option key={v.id} value={`${p.id}|${v.id}`}>
                      {p.name} · {v.name}
                    </option>
                  ))}
                </optgroup>
              ))}
          </select>
        )}
        <div className="row tight">
          {fileable && (
            <Button
              variant="primary"
              size="sm"
              disabled={!target}
              onClick={() => {
                const [pid, vid] = target.split('|');
                dispatch(
                  a.kind === 'audio'
                    ? db.profile(pid)?.type === 'character'
                      ? { type: 'addVoice', asset: a.id, pid }
                      : { type: 'profile', id: pid, patch: { ambient: a.id } }
                    : { type: 'fileImage', asset: a.id, pid, vid },
                );
              }}
            >
              {tr('storyboard.copy49')}
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => dispatch({ type: 'dismissInbox', asset: a.id })}
          >
            {tr('storyboard.copy50')}
          </Button>
        </div>
      </div>
    </div>
  );
}

/* ---------- profile editor ---------- */

function VariantDrop({ pid, vid, children, onFiles }) {
  const { setNodeRef, isOver, active } = useDroppable({ id: `vdrop:${pid}:${vid}` });
  return (
    <div
      ref={setNodeRef}
      {...nativeDrop(onFiles)}
      className={
        'pe-photos' + (isOver && String(active?.id).startsWith('asset:') ? ' drop-hot' : '')
      }
    >
      {children}
    </div>
  );
}

function VoiceSection({ p }) {
  const { t: tr } = useI18n();
  const { dispatch, db } = useStore();
  const [choose, setChoose] = useState(false);
  const voices = p.voices || [];
  const { setNodeRef, isOver, active } = useDroppable({ id: 'voicedrop:' + p.id });
  const hot = isOver && db.asset(String(active?.id).slice(6))?.kind === 'audio';
  return (
    <section
      ref={setNodeRef}
      className={'pe-section' + (hot ? ' drop-hot' : '')}
      {...nativeDrop((files) =>
        dispatch({ type: 'uploadFiles', files, target: { kind: 'voice', pid: p.id } }),
      )}
    >
      <header className="pe-section-head">
        <h3>{tr('storyboard.voiceSamples')}</h3>
        <small className="muted">{tr('storyboard.copy52')}</small>
        <Button size="sm" onClick={() => setChoose(true)}>
          <Plus size={14} aria-hidden="true" />
          {tr('storyboard.copy53')}
        </Button>
      </header>
      {voices.length ? (
        voices.map((v) => {
          const a = db.asset(v.asset);
          if (!a) return null;
          const max = Math.max(0, (Number.isFinite(a.seconds) ? a.seconds : 3) - 3);
          return (
            <div key={v.asset} className={'pe-voice' + (p.voice === v.asset ? ' main' : '')}>
              <div className="pe-row">
                <label className="ui-check">
                  <input
                    type="radio"
                    name={'voice-' + p.id}
                    checked={p.voice === v.asset}
                    onChange={() =>
                      dispatch({ type: 'profile', id: p.id, patch: { voice: v.asset } })
                    }
                  />
                  <span>{tr('storyboard.copy56')}</span>
                </label>
                <span className="pe-grow clip">
                  <b>{a.name}</b>{' '}
                  <small className="muted">
                    ·{' '}
                    {Number.isFinite(a.seconds)
                      ? a.seconds + tr('storyboard.detail22')
                      : tr('storyboard.detail23')}
                    {a.source === 'upload' ? tr('storyboard.detail24') : ''}
                  </small>
                </span>
                <PlayButton asset={a} label={tr('storyboard.finish2')} />
                <IconButton
                  size="sm"
                  label={tr('storyboard.finish3')}
                  onClick={() => dispatch({ type: 'removeVoice', pid: p.id, asset: v.asset })}
                >
                  <X size={14} />
                </IconButton>
              </div>
              <div className="pe-row">
                <small className="muted">{tr('storyboard.copy57')}</small>
                <input
                  className="pe-grow"
                  aria-label={a.name + tr('storyboard.detail25')}
                  type="range"
                  min={0}
                  max={max}
                  step={0.5}
                  value={Math.min(v.start, max)}
                  disabled={!max}
                  onChange={(e) =>
                    dispatch({
                      type: 'voice',
                      pid: p.id,
                      asset: v.asset,
                      patch: { start: +e.target.value },
                    })
                  }
                />
                <small className="muted">
                  {tr('storyboard.finish16', { start: v.start, end: v.start + 3 })}
                </small>
                <PlayButton asset={a} start={v.start} len={3} label={tr('storyboard.finish4')} />
              </div>
            </div>
          );
        })
      ) : (
        <p className="pe-drop">
          {tr('storyboard.copy55')}
          <br />
          <small>{tr('storyboard.copy54')}</small>
        </p>
      )}
      {choose && (
        <ChooseFiles
          kind="audio"
          title={tr('storyboard.finish25', { name: p.name })}
          exclude={new Set(voices.map((v) => v.asset))}
          onPick={(id) => {
            dispatch({ type: 'addVoice', pid: p.id, asset: id });
            setChoose(false);
          }}
          onFiles={(files) =>
            dispatch({ type: 'uploadFiles', files, target: { kind: 'voice', pid: p.id } })
          }
          onClose={() => setChoose(false)}
        />
      )}
    </section>
  );
}

// Character, place or prop profile on the shared Dialog/Field/Button/Segmented (owner,
// 2026-10-26: one framework everywhere). Edits save as you type, like before.
export function ProfileEditor() {
  const { t: tr } = useI18n();
  const { TYPE_LABEL, VARIANT_LABEL } = displayLabels(tr);
  const { state, dispatch, db } = useStore();
  const p = state.openProfile && db.profile(state.openProfile);
  const [vid, setVid] = useState(null),
    [choose, setChoose] = useState(false),
    [ambientChoose, setAmbientChoose] = useState(false),
    [newVariant, setNewVariant] = useState(false);
  useEffect(() => {
    setVid(null);
    setChoose(false);
    setAmbientChoose(false);
    setNewVariant(false);
  }, [state.openProfile]);
  if (!p) return null;
  const close = () => dispatch({ type: 'openProfile', id: null });
  const eyebrow = tr('storyboard.finish17', { kind: TYPE_LABEL[p.type] });
  const v = variantOf(p, vid || p.def);
  if (!v)
    return (
      <Dialog title={p.name} eyebrow={eyebrow} onClose={close} className="pe-dialog">
        <p className="muted">{tr('storyboard.mixed17', { kind: VARIANT_LABEL[p.type] })}</p>
        <DialogActions>
          <Button onClick={close}>{tr('storyboard.copy58')}</Button>
          <Button
            variant="primary"
            onClick={() => dispatch({ type: 'addVariant', pid: p.id, name: '默认' })}
          >
            {tr('storyboard.mixed18', { kind: VARIANT_LABEL[p.type] })}
          </Button>
        </DialogActions>
      </Dialog>
    );
  const audios = state.assets.filter((a) => a.kind === 'audio');
  const usedIn = state.shots.filter(
    (s) => s.cast.some((c) => c.id === p.id) || s.place?.id === p.id || s.props.includes(p.id),
  );
  const upload = (files) =>
    dispatch({ type: 'uploadFiles', files, target: { kind: 'image', pid: p.id, vid: v.id } });
  const describe =
    p.type === 'character'
      ? tr('storyboard.detail26')
      : p.type === 'location'
        ? tr('storyboard.detail27')
        : tr('storyboard.detail28');
  return (
    <Dialog
      title={p.name || tr('storyboard.finish5')}
      eyebrow={eyebrow}
      onClose={close}
      wide
      className="pe-dialog"
    >
      <div className="pe-body">
        <div className="pe-grid">
          <Field label={tr('storyboard.finish5')}>
            <input
              value={p.name}
              onChange={(e) =>
                dispatch({ type: 'profile', id: p.id, patch: { name: e.target.value } })
              }
            />
          </Field>
          <Field label={describe} help={tr('storyboard.finish24').replace(/^\s*·\s*/, '')}>
            <textarea
              rows={5}
              value={p.desc}
              onChange={(e) =>
                dispatch({ type: 'profile', id: p.id, patch: { desc: e.target.value } })
              }
            />
          </Field>
        </div>

        {p.type !== 'prop' && (
          <div className="pe-looks">
            <Segmented
              tabs
              label={VARIANT_LABEL[p.type]}
              value={v.id}
              onChange={setVid}
              options={p.variants.map((x) => ({
                value: x.id,
                label: (
                  <>
                    {x.name}
                    {x.id === p.def && <Badge tone="accent">{tr('storyboard.copy59')}</Badge>}
                    <small className="muted">{x.images.length}</small>
                  </>
                ),
              }))}
            />
            <Button
              size="sm"
              variant="ghost"
              disabled={state.busy}
              onClick={() => setNewVariant(true)}
            >
              <Plus size={14} aria-hidden="true" />
              {VARIANT_LABEL[p.type]}
            </Button>
          </div>
        )}

        <section className="pe-section">
          {p.type !== 'prop' && (
            <div className="pe-variant">
              <Field label={tr('storyboard.variantName', { kind: VARIANT_LABEL[p.type] })}>
                <input
                  value={v.name}
                  onChange={(e) =>
                    dispatch({
                      type: 'variant',
                      pid: p.id,
                      vid: v.id,
                      patch: { name: e.target.value },
                    })
                  }
                />
              </Field>
              <Field label={tr('storyboard.variantDesc', { kind: VARIANT_LABEL[p.type] })}>
                <input
                  value={v.desc}
                  placeholder={
                    p.type === 'character' ? tr('storyboard.detail31') : tr('storyboard.detail32')
                  }
                  onChange={(e) =>
                    dispatch({
                      type: 'variant',
                      pid: p.id,
                      vid: v.id,
                      patch: { desc: e.target.value },
                    })
                  }
                />
              </Field>
              <div className="pe-variant-actions">
                {v.id !== p.def && (
                  <Button
                    size="sm"
                    onClick={() => dispatch({ type: 'profile', id: p.id, patch: { def: v.id } })}
                  >
                    {tr('storyboard.copy60')}
                  </Button>
                )}
                <TrashMenu
                  target={{
                    kind: p.type === 'character' ? 'look' : 'location_version',
                    project_id: state.project.id,
                    id: v.id,
                    parent_id: p.id,
                  }}
                />
              </div>
            </div>
          )}
          <header className="pe-section-head">
            <h3>{tr('storyboard.copy61')}</h3>
            <small className="muted">{tr('storyboard.copy62')}</small>
            <Button size="sm" onClick={() => setChoose(true)}>
              <Plus size={14} aria-hidden="true" />
              {tr('storyboard.copy63')}
            </Button>
          </header>
          <VariantDrop pid={p.id} vid={v.id} onFiles={upload}>
            {v.images.map((id, i) => {
              const a = db.asset(id);
              return (
                a && (
                  <figure key={id} className="pe-photo">
                    <StoryboardAssetImage asset={a} />
                    <IconButton
                      size="sm"
                      className="pe-photo-remove"
                      label={tr('storyboard.remove')}
                      onClick={() =>
                        dispatch({ type: 'unfileImage', pid: p.id, vid: v.id, asset: id })
                      }
                    >
                      <X size={14} />
                    </IconButton>
                    {a.source === 'ai' && <Badge className="pe-photo-ai">AI</Badge>}
                    <figcaption>
                      <small className="clip">{a.name}</small>
                      {i === 0 ? (
                        <Badge tone="accent">{tr('storyboard.copy64')}</Badge>
                      ) : (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() =>
                            dispatch({ type: 'coverImage', pid: p.id, vid: v.id, asset: id })
                          }
                        >
                          {tr('storyboard.copy65')}
                        </Button>
                      )}
                    </figcaption>
                  </figure>
                )
              );
            })}
            <Button className="pe-photo-add" onClick={() => setChoose(true)}>
              <Plus size={18} aria-hidden="true" />
              <span>{tr('storyboard.copy63')}</span>
              <small>{tr('storyboard.copy66')}</small>
            </Button>
          </VariantDrop>
          {p.type === 'character' && !v.images.length && (
            <Note tone="warn">{tr('storyboard.copy67')}</Note>
          )}
          {p.type === 'character' && (p.voices || []).length > 1 && (
            <Field label={tr('storyboard.copy68').replace(/[：:]\s*$/, '')}>
              <select
                value={v.voice || ''}
                onChange={(e) =>
                  dispatch({
                    type: 'variant',
                    pid: p.id,
                    vid: v.id,
                    patch: { voice: e.target.value || null },
                  })
                }
              >
                <option value="">{tr('storyboard.copy69')}</option>
                {p.voices.map((x) => (
                  <option key={x.asset} value={x.asset}>
                    {db.asset(x.asset)?.name}
                  </option>
                ))}
              </select>
            </Field>
          )}
        </section>

        {p.type === 'character' && <VoicePicker profile={p} samples={<VoiceSection p={p} />} />}
        {p.type === 'location' && (
          <section className="pe-section">
            <header className="pe-section-head">
              <h3>{tr('storyboard.copy70')}</h3>
              <small className="muted">{tr('storyboard.copy73')}</small>
              <Button size="sm" onClick={() => setAmbientChoose(true)}>
                <Plus size={14} aria-hidden="true" />
                {tr('storyboard.copy72')}
              </Button>
            </header>
            <div className="pe-row">
              <select
                className="pe-grow"
                aria-label={tr('storyboard.finish6')}
                value={p.ambient || ''}
                onChange={(e) =>
                  dispatch({
                    type: 'profile',
                    id: p.id,
                    patch: { ambient: e.target.value || null },
                  })
                }
              >
                <option value="">{tr('storyboard.copy71')}</option>
                {audios.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}（
                    {Number.isFinite(a.seconds)
                      ? a.seconds + tr('storyboard.detail22')
                      : tr('storyboard.detail23')}
                    ）
                  </option>
                ))}
              </select>
              {p.ambient && <PlayButton asset={db.asset(p.ambient)} />}
            </div>
          </section>
        )}

        <ProfileBible p={p} />

        <section className="pe-used">
          <h3>
            {tr(usedIn.length === 1 ? 'storyboard.usedInOne' : 'storyboard.finish18', {
              count: usedIn.length,
            })}
          </h3>
          {usedIn.length > 0 && (
            <>
              <div className="pe-chips">
                {usedIn.map((s) => (
                  <Chip key={s.id}>{s.title}</Chip>
                ))}
              </div>
              <small className="muted">{tr('storyboard.copy74')}</small>
            </>
          )}
        </section>
      </div>
      <DialogActions>
        <TrashButton
          target={{ kind: 'entity', project_id: state.project.id, id: p.id }}
          label={tr('storyboard.profileDelete')}
          onTrashSettled={close}
        />
        <span className="pe-grow" />
        <Button variant="primary" onClick={close}>
          {tr('storyboard.profileDone')}
        </Button>
      </DialogActions>
      {ambientChoose && (
        <ChooseFiles
          kind="audio"
          title={tr('storyboard.finish26', { name: p.name })}
          exclude={new Set()}
          onPick={(asset) => {
            dispatch({ type: 'profile', id: p.id, patch: { ambient: asset } });
            setAmbientChoose(false);
          }}
          onFiles={(files) =>
            dispatch({ type: 'uploadFiles', files, target: { kind: 'ambient', pid: p.id } })
          }
          onClose={() => setAmbientChoose(false)}
        />
      )}
      {choose && (
        <ChooseFiles
          kind="image"
          title={tr('storyboard.mixed11', { p0: p.name, p1: v.name })}
          exclude={new Set(v.images)}
          onPick={(id) => dispatch({ type: 'fileImage', asset: id, pid: p.id, vid: v.id })}
          onFiles={upload}
          onClose={() => setChoose(false)}
        />
      )}
      {newVariant && (
        <StoryboardNameDialog
          key={p.id}
          title={tr('storyboard.mixed12', { p0: VARIANT_LABEL[p.type] })}
          label={tr('storyboard.mixed13', { p0: VARIANT_LABEL[p.type] })}
          onSubmit={(name) => dispatch({ type: 'addVariant', pid: p.id, name })}
          onClose={() => setNewVariant(false)}
        />
      )}
    </Dialog>
  );
}

/* ---------- right-click picker ---------- */

const SLOT_ALLOW = {
  start: ['location', 'prop', 'image'],
  end: ['location', 'prop', 'image'],
  refs: ['image', 'video', 'audio'],
  cast: ['character', 'location', 'prop'],
  smart: ['character', 'location', 'prop', 'image', 'video', 'audio'],
};

export function AssetPicker({ shotId, slot, x, y, onClose }) {
  const { t: tr } = useI18n();
  const { TYPE_LABEL } = displayLabels(tr);
  const SLOT_TITLE = {
    start: tr('storyboard.detail33'),
    end: tr('storyboard.detail34'),
    refs: tr('storyboard.detail35'),
    cast: tr('storyboard.detail36'),
    smart: tr('storyboard.detail37'),
  };

  const { state, dispatch, db } = useStore();
  const shot = state.shots.find((s) => s.id === shotId);
  const [q, setQ] = useState(''),
    [tab, setTab] = useState('all'),
    [flash, setFlash] = useState('');
  const box = useRef(),
    input = useRef();
  const [moved, handle] = useDragMove();
  useEffect(() => {
    input.current?.focus();
    const away = (e) => box.current && !box.current.contains(e.target) && onClose();
    window.addEventListener('pointerdown', away, true);
    return () => window.removeEventListener('pointerdown', away, true);
  }, [onClose]);
  if (!shot) return null;
  const allow = SLOT_ALLOW[slot] || SLOT_ALLOW.smart;
  const tabs = ['all', ...allow];
  const label = (k) =>
    ({
      all: tr('storyboard.detail38'),
      ...TYPE_LABEL,
      image: tr('storyboard.detail9'),
      video: tr('storyboard.detail12'),
      audio: tr('storyboard.copy51'),
    })[k];
  const ok = (k) => allow.includes(k) && (tab === 'all' || tab === k);
  const profiles = state.profiles.filter(
    (p) => ok(p.type) && (!q || (p.name + p.desc).includes(q)),
  );
  const files = state.assets.filter(
    (a) => !a.inbox && ok(a.kind) && (!q || (a.name + ' ' + assetLabel(a, tr)).includes(q)),
  );
  const results = [...profiles.map((p) => ({ profile: p })), ...files.map((a) => ({ asset: a }))];
  const multi = slot !== 'start' && slot !== 'end';
  const pick = async (item) => {
    const s = state.shots.find((x) => x.id === shotId);
    const { patch, msg } = planDrop(s, item, slot === 'cast' ? 'smart' : slot, db);
    if (!patch) {
      setFlash(msg);
      return;
    }
    const receipt = await dispatch(
      item.profile
        ? {
            type: 'drop',
            profile: item.profile.id,
            shot: shotId,
            slot: slot === 'cast' ? 'smart' : slot,
          }
        : {
            type: 'drop',
            asset: item.asset.id,
            shot: shotId,
            slot: slot === 'cast' ? 'smart' : slot,
          },
    );
    if (receipt?.ok === false) {
      setFlash(receipt.error);
      return;
    }
    if (multi) setFlash(msg);
    else onClose();
  };
  const usedP = new Set([...shot.cast.map((c) => c.id), shot.place?.id, ...shot.props]);
  const usedA = new Set([...shot.refs, shot.start, shot.end]);
  const left = Math.max(
      8,
      Math.min(x || 8, window.innerWidth - Math.min(400, window.innerWidth - 16)),
    ),
    top = Math.max(
      8,
      Math.min(y || 8, window.innerHeight - Math.min(500, window.innerHeight - 16)),
    );
  return createPortal(
    <div
      ref={box}
      className="sb-portal density-compact picker"
      style={{ left: left + moved.x, top: top + moved.y }}
      role="dialog"
      aria-label={SLOT_TITLE[slot]}
      onContextMenu={(e) => {
        e.preventDefault();
        e.stopPropagation();
      }}
      onClick={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === 'Escape') onClose();
      }}
    >
      <div className="picker-head" {...handle}>
        <b>{SLOT_TITLE[slot]}</b>
        <small>{shotName(shot, state.shots.indexOf(shot), tr)}</small>
        <IconButton size="xs" onClick={onClose} label={tr('storyboard.copy58')}>
          <X size={14} />
        </IconButton>
      </div>
      <input
        ref={input}
        className="lib-search"
        placeholder={tr('storyboard.finish7')}
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && results[0]) pick(results[0]);
        }}
      />
      <div className="lib-tabs">
        {tabs.map((k) => (
          <FilterChip key={k} active={tab === k} label={label(k)} onClick={() => setTab(k)} />
        ))}
      </div>
      {(slot === 'start' || slot === 'end') && (
        <Note as="div" tone="info">
          {tr('storyboard.copy75')}
        </Note>
      )}
      <div className="picker-grid">
        {profiles.map((p) => {
          const c = coverOf(db, p, p.def);
          return (
            <Button
              key={p.id}
              className={'tile prof ' + p.type + (usedP.has(p.id) ? ' used' : '')}
              onClick={() => pick({ profile: p })}
              title={p.desc}
            >
              {c ? (
                <StoryboardAssetImage asset={c} />
              ) : (
                <div className="tile-audio">{TYPE_LABEL[p.type]}</div>
              )}
              <span className="tile-badge">{TYPE_LABEL[p.type]}</span>
              {usedP.has(p.id) && (
                <span className="tile-used">
                  <Check size={12} />
                </span>
              )}
              <span className="tile-name">{p.name}</span>
            </Button>
          );
        })}
        {files.map((a) => (
          <Button
            key={a.id}
            className={'tile' + (usedA.has(a.id) ? ' used' : '')}
            onClick={() => pick({ asset: a })}
            title={a.name}
          >
            {a.kind === 'audio' ? (
              <div className="tile-audio">
                <Music size={22} />
                <small>{secondsLabel(a.seconds, tr)}</small>
              </div>
            ) : (
              <StoryboardAssetImage asset={a} />
            )}
            {a.kind === 'video' && secondsLabel(a.seconds, tr) && (
              <span className="tile-badge">
                <Play size={11} /> {secondsLabel(a.seconds, tr)}
              </span>
            )}
            {usedA.has(a.id) && (
              <span className="tile-used">
                <Check size={12} />
              </span>
            )}
            <span className="tile-name">{assetLabel(a, tr)}</span>
          </Button>
        ))}
      </div>
      {!results.length && <p className="muted">{tr('storyboard.copy76')}</p>}
      <div className="picker-foot">
        {flash || (multi ? tr('storyboard.detail39') : tr('storyboard.detail40'))}
      </div>
    </div>,
    document.body,
  );
}

/* ---------- shot card ---------- */

export function useShotSortable(id, roleDescription) {
  const s = useSortable({ id: 'shot:' + id, attributes: { roleDescription } });
  return {
    ...s,
    style: {
      transform: CSS.Transform.toString(s.transform),
      transition: s.transition,
      opacity: s.isDragging ? 0.4 : 1,
    },
  };
}

export function ModeChip({ info }) {
  const { t: tr } = useI18n();
  const { MODE_LABEL } = displayLabels(tr);
  return (
    <span className={'mode ' + info.mode.toLowerCase()} title={MODE_LABEL[info.mode] || info.kind}>
      {MODE_LABEL[info.mode] || info.kind}
    </span>
  );
}
const Pill = ({ status }) => <span className={'pill ' + status.tone}>{status.label}</span>;
function Progress({ v }) {
  if (!v || !Number.isFinite(v.progress) || !['generating', 'running', 'queued'].includes(v.status))
    return null;
  return (
    <div className="progress">
      <i style={{ width: `${Math.round(v.progress * 100)}%` }} />
    </div>
  );
}
export function ShotThumb({ shot }) {
  const { t: tr } = useI18n();
  const v = chosenVersion(shot);
  return v ? (
    <StoryboardAssetImage asset={v.asset || v} className="sthumb" retryable />
  ) : (
    <div className="sthumb empty" role="img" aria-label={tr('storyboard.detail75')}>
      <svg
        width="24"
        height="24"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        aria-hidden="true"
      >
        <rect x="3" y="5" width="18" height="14" rx="2" />
        <path d="m9 14 3-3 5 5M7 8h.01" />
      </svg>
    </div>
  );
}

// Stories made by the assistant or older versions name the first look in Chinese ("默认造型");
// show it in the reader's language. Names the user typed stay as they are.
const DEFAULT_LOOK_NAMES = new Set(['默认造型', '默认', '主图', 'Default look', 'Default']);
const lookName = (look, tr) =>
  look && DEFAULT_LOOK_NAMES.has(look.name) ? tr('storyboard.defaultLook') : look?.name;

export function WhoChips({ shot, small }) {
  const { t: tr } = useI18n();
  const { db } = useStore();
  return (
    <div className={'who' + (small ? ' small' : '')}>
      {shot.cast.map((c) => {
        const p = db.profile(c.id);
        return (
          p && (
            <Chip key={c.id} kind="character">
              {p.name}
              <i>{lookName(variantOf(p, c.variant), tr) || tr('storyboard.detail41')}</i>
            </Chip>
          )
        );
      })}
      {shot.place && db.profile(shot.place.id) && (
        <Chip kind="location">{db.profile(shot.place.id).name.replace('旧车站 · ', '')}</Chip>
      )}
      {shot.props.map(
        (id) =>
          db.profile(id) && (
            <Chip key={id} kind="prop">
              {db.profile(id).name}
            </Chip>
          ),
      )}
    </div>
  );
}

const dragItem = (active, db) => {
  const id = String(active?.id || '');
  if (id.startsWith('asset:')) return { asset: db.asset(id.slice(6)) };
  if (id.startsWith('profile:')) return { profile: db.profile(id.slice(8)) };
  return null;
};

// Whole seconds for a card; a cut shorter than a second still reads as 0:01.
const roundedSeconds = (sec) =>
  Number.isFinite(sec) && sec > 0 ? Math.max(1, Math.round(sec)) : sec;
// 4 → "4", 0.6666 → "0.7".
const shortSeconds = (sec) => (Number.isFinite(sec) ? String(Math.round(sec * 10) / 10) : sec);
// The story's most common mode and tier; a card labels only what differs from them.
const usualCache = new WeakMap();
function usualSettings(shots, db, locale) {
  if (usualCache.has(shots)) return usualCache.get(shots);
  const top = (values) => {
    const counts = new Map();
    for (const v of values) counts.set(v, (counts.get(v) || 0) + 1);
    return [...counts].sort((a, b) => b[1] - a[1])[0]?.[0];
  };
  const value = {
    mode: top(shots.map((s) => expand(s, db, locale).mode)),
    quality: top(shots.map((s) => s.quality)),
  };
  usualCache.set(shots, value);
  return value;
}

export function ShotCard({
  shot,
  index,
  selected,
  onOpen,
  checked = false,
  picking = false,
  onSelect,
}) {
  const { t: tr, locale } = useI18n();
  const { QUALITY_LABEL } = displayLabels(tr);
  const { state, db, dispatch, adapter } = useStore();
  const [picker, setPicker] = useState(null);
  const { setNodeRef, setActivatorNodeRef, attributes, listeners, style, isOver, active } =
    useShotSortable(shot.id, tr('dnd.roleDescription'));
  // Takes marked "needs a redo", from the review summaries the shot editor already read; the
  // card never reads them itself.
  const reviews = useQuery({
    queryKey: ['take-review-summaries', state.contextId, shot.id],
    queryFn: () => adapter.readTakeReviewSummaries(shot.id),
    enabled: false,
  });
  const redo = (Array.isArray(reviews.data) ? reviews.data : []).filter(
    (row) => row.verdict === 'redo',
  ).length;
  const title = String(shot.title || '').trim(),
    name = shotName(shot, index, tr),
    number = String(index + 1).padStart(2, '0');
  const info = expand(shot, db, locale);
  const missing = useShotReadiness(shot, tr);
  const warnings = [...info.warnings, ...missing];
  const status = shotStatus(shot, queuePositions(state.shots)[shot.id], locale);
  const latest = shot.versions[shot.versions.length - 1];
  const usual = usualSettings(state.shots, db, locale);
  const item = isOver ? dragItem(active, db) : null;
  const plan = item && (item.asset || item.profile) ? planDrop(shot, item, 'smart', db) : null;
  // A restored take keeps its exact prompt and references; it is only changed in the editor.
  const restored = !!shot.entity?.data.h3?.explicitReferences && !shot.entity?.data.storyboard;
  const picturing = useDrawingPicture(shot.id);
  return (
    <div
      ref={setNodeRef}
      style={style}
      role="group"
      aria-label={tr(title ? 'shotCard.label' : 'shotCard.labelUntitled', {
        n: number,
        title,
        status: status.label,
      })}
      data-shot-card={shot.id}
      data-grid-card={shot.id}
      onKeyDown={(event) => openKey(event, (e) => onOpen(shot.id, e))}
      className={
        'card' +
        (selected ? ' selected' : '') +
        (picking ? ' picking' : '') +
        (plan ? ' drop-hot' : '')
      }
      {...nativeDrop((files) =>
        dispatch({
          type: 'uploadFiles',
          files,
          target: { kind: 'shot', shot: shot.id, slot: 'smart' },
        }),
      )}
      onClick={(event) => onOpen(shot.id, event)}
      onContextMenu={(e) => {
        e.preventDefault();
        setPicker({ slot: 'smart', x: e.clientX, y: e.clientY });
      }}
    >
      <div className="card-media">
        {onSelect && (
          <Button
            className={'workbench-select' + (checked ? ' checked' : '')}
            aria-label={tr('workbench.select')}
            aria-pressed={checked}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.stopPropagation();
              onSelect(e);
            }}
          >
            {checked ? <CheckCircle2 size={18} /> : <Circle size={18} />}
          </Button>
        )}
        <ShotThumb shot={shot} />
        <span className="card-no">{number}</span>
        {/* Dragging starts here only, so Enter and Space on the card open it (audit 2026-11-09). */}
        <IconButton
          size="xs"
          variant="on-media"
          className="card-grip"
          data-drag-handle
          ref={setActivatorNodeRef}
          {...attributes}
          {...listeners}
          label={tr('dnd.dragShot', { shot: name })}
          onClick={(event) => event.stopPropagation()}
        >
          <GripVertical size={14} aria-hidden="true" />
        </IconButton>
        <span className="card-dur">
          {fmt(
            // #232 (#27): show the real length, never 0:00 for a short kept cut.
            roundedSeconds(
              shot.segments?.length && shot.chosen ? segmentsTotal(shot.segments) : info.sec,
            ),
          )}
        </span>
        <span className={'card-state ' + status.tone} title={status.label} />
        {picturing && <span className="card-drawing">{tr('shotPicture.drawing')}</span>}
        <IconButton
          size="xs"
          className="card-add"
          label={tr('shotCard.addAfter')}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={(event) => {
            event.stopPropagation();
            if (state.busy || state.workbenchPending) return;
            dispatch({ type: 'add', after: shot.id, title: tr('storyboard.newShot') });
          }}
        >
          <Plus size={14} />
        </IconButton>
        <Progress v={latest} />
      </div>
      <div className="card-body">
        {/* One line: title, who made it, then mode / tier only when this shot differs from the
            story's usual (#232 #12); the card grows only for cast chips (owner, 2026-10-26). */}
        <div className="card-title" title={name}>
          <span className={'card-title-text' + (title ? '' : ' muted')}>{name}</span>
          <AssistantBadge createdBy={shot.created_by} />
          <span className="card-meta">
            {redo > 0 && (
              <span className="card-redo" title={tr('shotCard.redoHelp', { count: redo })}>
                {tr('shotCard.redo', { count: redo })}
              </span>
            )}
            {info.mode !== usual.mode && <ModeChip info={info} />}
            {shot.quality !== usual.quality && (
              <span className="q">{QUALITY_LABEL[shot.quality]}</span>
            )}
            {warnings.length > 0 && (
              <Tooltip tap content={warnings.join('\n')}>
                <Button
                  className="warnbadge"
                  aria-label={warnings.join('. ')}
                  onPointerDown={(event) => event.stopPropagation()}
                  onClick={(event) => event.stopPropagation()}
                >
                  !
                </Button>
              </Tooltip>
            )}
          </span>
        </div>
        <ShotCardPlan shot={shot} />
        {!restored && <ShotCardPrompt shot={shot} />}
        {(shot.cast.length > 0 || shot.place || shot.props.length > 0) && (
          <WhoChips shot={shot} small />
        )}
        {['wait', 'busy', 'warn'].includes(status.tone) && <Pill status={status} />}
      </div>
      {plan && (
        <div className={'drop-hint' + (plan.warn ? ' warn' : '')}>{plan.hint || plan.msg}</div>
      )}
      {picker && <AssetPicker shotId={shot.id} {...picker} onClose={() => setPicker(null)} />}
    </div>
  );
}

/* ---------- shot editor ---------- */

// #232 (#50): a small "?" that explains a field on hover or keyboard focus.
export function HelpTip({ text }) {
  return (
    <Tooltip content={text} tap>
      <span className="help-tip" tabIndex={0} role="img" aria-label={text}>
        <CircleHelp size={14} strokeWidth={1.75} aria-hidden="true" />
      </span>
    </Tooltip>
  );
}

function Slot({
  id,
  label,
  hint,
  help,
  children,
  onClear,
  onPick,
  onFiles,
  guide,
  className = '',
}) {
  const { t: tr } = useI18n();
  const { setNodeRef, isOver, active } = useDroppable({ id });
  const hot = isOver && /^(asset|profile):/.test(String(active?.id));
  const open = (e) => {
    e.preventDefault();
    onPick?.(e);
  };
  return (
    <div
      ref={setNodeRef}
      {...(onFiles ? nativeDrop(onFiles) : {})}
      className={'slot ' + className + (hot ? ' drop-hot' : '')}
      data-guide-field={guide}
      onContextMenu={open}
      title={tr('storyboard.finish8')}
    >
      <div className="slot-label">
        <span className="slot-name">
          {label}
          {help && <HelpTip text={help} />}
        </span>
        <span>
          {onPick && (
            <IconButton size="xs" label={tr('storyboard.finish9')} onClick={open}>
              <Search size={14} />
            </IconButton>
          )}
          {onClear && (
            <IconButton size="xs" onClick={onClear} label={tr('storyboard.remove')}>
              <X size={14} />
            </IconButton>
          )}
        </span>
      </div>
      {children || (
        <div className="slot-empty clickable" onClick={open}>
          {hint}
          <br />
          <small>{tr('storyboard.copy77')}</small>
        </div>
      )}
    </div>
  );
}

// Size a text box to its text. Shrinking needs a reset to 'auto', which makes the panel jump,
// so only typing or opening another shot shrinks; other renders only grow when text overflows.
function growTextarea(node, shrink = false) {
  if (shrink) node.style.height = 'auto';
  else if (node.scrollHeight <= node.clientHeight + 1) return;
  const height = Math.min(node.scrollHeight + 2, 480) + 'px';
  if (node.style.height !== height) node.style.height = height;
}

// The chosen take's trim, right under the panel's video (owner, 2026-10-21): drag a handle and
// the video shows that frame; letting go saves the new range. No separate editor needed.
// The chosen take's source length and the shot's current single trim, for the trim bar and
// as the first kept part (#512).
function useTakeRange(shotId, mediaRef) {
  const project = useSyncExternalStore(store.subscribe, () => store.getState().project);
  const [probed, setProbed] = useState(null);
  const shot = project.entities.find((e) => e.id === shotId),
    video = project.entities.find((e) => e.id === shot?.data.selectedAssetId);
  const getVideo = () => mediaRef.current?.querySelector('video') || null;
  useEffect(() => {
    const node = getVideo();
    if (!node) return;
    const read = () => Number.isFinite(node.duration) && setProbed(node.duration);
    read();
    node.addEventListener('loadedmetadata', read);
    return () => node.removeEventListener('loadedmetadata', read);
  });
  if (!shot || video?.type !== 'video') return { getVideo, shot: null };
  const cut = videoCut(shot, video),
    known = video.data.metadata?.duration ?? video.data.metadata?.duration_s,
    start = (cut.aligned?.startFrame ?? 0) / cutFps,
    end = start + (cut.requestedFrames || 0) / cutFps;
  return {
    getVideo,
    shot,
    duration: Number.isFinite(known) ? known : probed,
    start,
    end: end > start ? end : start + 1 / cutFps,
  };
}

function ShotParts({ shot, mediaRef }) {
  const take = useTakeRange(shot.id, mediaRef);
  if (!take.shot) return null;
  return (
    <ShotSegments
      key={shot.id}
      shot={shot}
      getVideo={take.getVideo}
      duration={take.duration}
      trim={[take.start, take.end]}
    />
  );
}

function ShotTrim({ shotId, mediaRef }) {
  const take = useTakeRange(shotId, mediaRef);
  if (!take.shot) return null;
  return (
    <TrimBar
      duration={take.duration}
      start={take.start}
      end={take.end}
      getVideo={take.getVideo}
      onCommit={(s, e, total) => store.editProject((p) => void trimClip(p, shotId, s, e, total))}
    />
  );
}

// Take review needs the cloud review route; local stories and older backends skip it.
function TakeReviewSlot(props) {
  const { adapter } = useStore();
  return adapter?.readTakeReview ? <TakeReview {...props} /> : null;
}

// The plan checklist (shot-plan.js) for one shot: the beats it claims and who speaks in them.
function usePlanChecklist(shot) {
  const { state } = useStore();
  const plan = planOf(shot.entity);
  const scene = (state.scenes || []).find((s) => s.id === shot.entity?.parentId),
    claimed = new Set(plan.beats);
  const beats = sceneBeats(scene).filter((b) => claimed.has(b.id));
  const speakers = [
    ...new Set(beats.filter((b) => b.kind === 'line' && b.speaker).map((b) => b.speaker)),
  ];
  const characters = state.profiles.filter((p) => p.type === 'character');
  return { items: planChecklist(shot, { speakers, characters }), speakers, characters };
}

// Which plan tab (ShotPlan.jsx) holds each checklist item, and the label of its field.
const PLAN_FIELDS = {
  purpose: ['plan', 'plan.purpose'],
  beats: ['plan', null],
  size: ['plan', 'plan.sizeLabel'],
  emotion: ['acting', 'plan.emotionFrom'],
  timeline: ['acting', null],
  continuity: ['continuity', 'plan.entry'],
};
const PLAN_TABS = ['plan', 'acting', 'continuity'];

// Open the item's tab, then focus and pulse its field.
function focusPlanField(root, key, tr) {
  const panel = root?.querySelector('.shot-plan'),
    [tab, label] = PLAN_FIELDS[key] || [];
  if (!panel || !tab) return;
  const button = panel.querySelectorAll('[role="tab"]')[PLAN_TABS.indexOf(tab)];
  if (button && button.getAttribute('aria-selected') !== 'true') button.click();
  requestAnimationFrame(() => {
    const field = label
      ? [...panel.querySelectorAll('.ui-field')].find(
          (node) => node.querySelector('.ui-field-label')?.textContent === tr(label),
        )
      : key === 'timeline'
        ? panel.querySelector('.plan-timeline')
        : panel.querySelector('.shot-plan-body');
    const input = field?.querySelector('input, textarea, select, button') || button;
    (field || panel).scrollIntoView({ block: 'center' });
    input?.focus({ preventScroll: true });
    const glow = field || panel;
    glow.classList.remove('guide-highlight');
    void glow.offsetWidth;
    glow.classList.add('guide-highlight');
    setTimeout(() => glow.classList.remove('guide-highlight'), 1600);
  });
}

// Next to Generate: what the plan still lacks, each a link to its field. It only informs;
// generating is never blocked by it (audit 2026-11-09). One line, so nothing moves as items
// get done.
function PlanMissing({ shot, editorRef }) {
  const { t: tr } = useI18n();
  const { dispatch } = useStore();
  const { items, speakers, characters } = usePlanChecklist(shot);
  const open = items.filter((item) => !item.done);
  const go = (item) => {
    if (item.key === 'firstFrame') {
      highlightGuideField(document, 'frames');
      return;
    }
    if (item.key === 'expressions') {
      const who = speakers
        .map((id) => characters.find((c) => c.id === id))
        .find((c) => {
          const sheet = c?.bible?.expressions;
          return !sheet || Object.values(sheet).filter(Boolean).length < 6;
        });
      if (who) dispatch({ type: 'openProfile', id: who.id });
      return;
    }
    focusPlanField(editorRef.current, item.key, tr);
  };
  return (
    <div className="ed-missing" role="group" aria-label={tr('plan.missingLabel')}>
      <span className="ed-missing-head">
        {open.length
          ? tr('plan.missing', { done: items.length - open.length, total: items.length })
          : tr('plan.allDone')}
      </span>
      {open.map((item) => (
        <Button key={item.key} variant="link" size="sm" onClick={() => go(item)}>
          {tr('plan.check.' + item.key)}
        </Button>
      ))}
    </div>
  );
}

export function ShotEditor({ shotId, onClose, inline, wide, onWide }) {
  const { t: tr, locale } = useI18n();
  const { MODE_LABEL, QUALITY_LABEL } = displayLabels(tr);
  const { state, dispatch, db } = useStore();
  const shot = state.shots.find((s) => s.id === shotId);
  const missing = useShotReadiness(shot, tr);
  const [copies, setCopies] = useState('1');
  // Hooks stay above the early return below (rules of hooks).
  const [regenerating, setRegenerating] = useState(false);
  const [preview, setPreview] = useState(null),
    [picker, setPicker] = useState(null),
    [saving, setSaving] = useState(false),
    [msg, setMsg] = useState('');
  useEffect(() => {
    setPreview(null);
    setPicker(null);
    setSaving(false);
    setMsg('');
    setCopies('1');
  }, [shotId, state.contextId]);
  // Text boxes grow with what is typed, like a chat composer; size them for saved text too.
  const editorRef = useRef(null),
    mediaRef = useRef(null);
  useEffect(() => {
    editorRef.current?.querySelectorAll('textarea').forEach((node) => growTextarea(node, true));
  }, [shotId]);
  useEffect(() => {
    editorRef.current?.querySelectorAll('textarea').forEach((node) => growTextarea(node));
  });
  const [shownQuality, chooseQuality] = useOptimisticChoice(shot?.quality, (quality) =>
    dispatch({ type: 'update', id: shotId, patch: { quality } }),
  );
  if (!shot) return null;
  const index = state.shots.indexOf(shot);
  // Resolution and labels follow the pressed quality too, not only the toggle (#1032).
  const info = expand({ ...shot, quality: shownQuality }, db, locale);
  const upd = (patch) => dispatch({ type: 'update', id: shot.id, patch });
  // One new take: a fresh seed only matters when the seed is locked; HD switches the tier first.
  const regenerate = async (patch) => {
    if (patch.newSeed && !shot.seedLock) delete patch.newSeed;
    setRegenerating(true);
    try {
      if (Object.keys(patch).length) {
        const result = await upd(patch);
        if (result?.ok === false) return;
      }
      await dispatch({ type: 'workbenchGenerate', ids: [shot.id], copies: 1 });
    } finally {
      setRegenerating(false);
    }
  };
  const openPicker = (slot) => (e) => setPicker({ slot, x: e.clientX, y: e.clientY });
  const sf = startFrame(shot, db, locale),
    endA = shot.end && db.asset(shot.end);
  const viewing = shot.versions.find((v) => v.id === preview) || chosenVersion(shot);
  const busy =
    state.busy ||
    shot.versions.some((v) =>
      [
        'queued',
        'generating',
        'waiting_capacity',
        'claimed',
        'submitting',
        'running',
        'collecting',
        'checking',
      ].includes(v.status),
    );
  // A queued take has not started; say so instead of "Generating…" (cold starts take minutes).
  const queued =
      !state.busy &&
      shot.versions.some((v) =>
        ['queued', 'waiting_capacity', 'claimed', 'submitting'].includes(v.status),
      ) &&
      !shot.versions.some((v) =>
        ['generating', 'running', 'collecting', 'checking'].includes(v.status),
      ),
    // "Another version" and "Regenerate" only once a take exists, not while the first one waits.
    made = shot.versions.some((v) => ['done', 'succeeded'].includes(v.status));
  const restoredExact = !!shot.entity?.data.h3?.explicitReferences && !shot.entity?.data.storyboard;
  const chosen = chosenVersion(shot),
    qualityLabel = QUALITY_LABEL[shownQuality] || tr('storyboard.detail48'),
    durationRange = info.durationRange;

  return (
    <section
      ref={editorRef}
      // Opening a shot moves focus here (StoryboardWorkspace); Escape in a field stops at the field.
      tabIndex={-1}
      data-shot-editor={shot.id}
      aria-label={tr('storyboard.editorLabel', { shot: shotName(shot, index, tr) })}
      className={'editor' + (inline ? ' inline' : '') + (wide ? ' wide' : '')}
      onInput={(event) => event.target.tagName === 'TEXTAREA' && growTextarea(event.target, true)}
    >
      <header className="ed-head">
        <div>
          <small>{tr('storyboard.finish19', { number: String(index + 1).padStart(2, '0') })}</small>
          <div className="ed-title-row">
            <input
              className="ed-title"
              aria-label={tr('storyboard.finish10')}
              value={shot.title}
              placeholder={tr('shot.untitled', { n: index + 1 })}
              size={Math.max(4, Math.min(28, [...(shot.title || '')].length + 1))}
              onChange={(e) => upd({ title: e.target.value })}
              onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()}
            />
            <Button
              variant="ghost"
              className="round ed-rename"
              title={tr('trash.rename')}
              aria-label={tr('trash.rename')}
              onClick={(e) => {
                const input = e.currentTarget.parentElement.querySelector('.ed-title');
                input?.focus();
                input?.select();
              }}
            >
              <Pencil size={13} />
            </Button>
          </div>
        </div>
        {onWide && (
          <Button
            size="sm"
            // #232 (#46): a labelled button, not a bare icon, so the full-window editor is found.
            className="ed-expand"
            aria-label={tr(wide ? 'workbench.collapse' : 'workbench.expand')}
            title={tr(wide ? 'workbench.collapse' : 'workbench.expand')}
            onClick={onWide}
          >
            {wide ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
            {tr(wide ? 'workbench.collapse' : 'workbench.expand')}
          </Button>
        )}
        <TrashButton target={{ kind: 'entity', project_id: state.project.id, id: shot.id }} />
        {onClose && (
          <IconButton size="sm" label={tr('storyboard.closeEditor')} onClick={onClose}>
            <X size={16} />
          </IconButton>
        )}
      </header>

      <div className="ed-media">
        <div className="ed-preview" ref={mediaRef}>
          {['done', 'succeeded'].includes(viewing?.status) ? (
            <StoryboardVideo key={viewing.id} asset={viewing.asset || viewing} controls />
          ) : (
            <div className="ed-preview-empty">
              {queued
                ? tr('storyboard.queuedPreview')
                : busy
                  ? tr('storyboard.detail49')
                  : tr('storyboard.detail50')}
            </div>
          )}
          {viewing &&
            viewing.id !== shot.chosen &&
            ['done', 'succeeded'].includes(viewing.status) && (
              <Button
                variant="primary"
                className="use"
                onClick={() => dispatch({ type: 'choose', shot: shot.id, version: viewing.id })}
              >
                {tr('storyboard.finish20', { number: chosen?.n })}
              </Button>
            )}
        </div>
        <WorkbenchTakes
          shot={shot}
          wide={wide}
          onWide={onWide}
          previewId={viewing?.id}
          onPreview={(row) => setPreview(row.entity_id || row.id)}
        />
        {['done', 'succeeded'].includes(viewing?.status) && (
          <div className="row tight take-tools">
            {/* #232 (#53): downloads and "save frame" stay in view as compact buttons. */}
            <div className="take-tools-inline">
              <StoryboardDownloads version={viewing} title={shot.title} />
              <Button
                size="sm"
                disabled={saving}
                title={tr('storyboard.saveFrameHint')}
                onClick={() => {
                  // Save the frame on screen right away, in the background (owner, 2026-10-22).
                  const node = mediaRef.current?.querySelector('video');
                  node?.pause();
                  const at = Number((node?.currentTime || 0).toFixed(3));
                  setSaving(true);
                  setMsg(tr('storyboard.savingFrame', { seconds: at.toFixed(2) }));
                  Promise.resolve(
                    dispatch({
                      type: 'saveFrame',
                      shot: shot.id,
                      version: viewing.id,
                      artifactId: viewing.artifactId,
                      t: at,
                    }),
                  )
                    .then((receipt) =>
                      setMsg(
                        !receipt || receipt.ok === false
                          ? receipt?.error || tr('storyboard.detail43')
                          : tr('storyboard.frameSaved', { seconds: at.toFixed(2) }),
                      ),
                    )
                    .catch((error) => setMsg(error?.message || tr('storyboard.detail43')))
                    .finally(() => setSaving(false));
                }}
              >
                <Pin size={14} />
                {tr('storyboard.copy81')}
              </Button>
            </div>

            {msg && <small className="okmsg">{msg}</small>}
          </div>
        )}
        {['done', 'succeeded'].includes(viewing?.status) && !restoredExact && (
          <TakeNextStep
            shot={shot}
            take={viewing}
            mediaRef={mediaRef}
            busy={regenerating}
            onAnother={() => regenerate({ newSeed: true })}
            onMessage={setMsg}
          />
        )}

        {viewing?.id === shot.chosen && ['done', 'succeeded'].includes(viewing?.status) && (
          <>
            {/* With kept parts, the parts panel has its own selection bar (#512). */}
            {!shot.segments?.length && <ShotTrim shotId={shot.id} mediaRef={mediaRef} />}
            <ShotParts shot={shot} mediaRef={mediaRef} />
          </>
        )}
        {viewing?.artifactId && ['done', 'succeeded'].includes(viewing?.status) && (
          <TakeReviewSlot
            key={viewing.artifactId}
            shotId={shot.id}
            artifactId={viewing.artifactId}
            mediaRef={mediaRef}
            chosen={viewing.id === shot.chosen}
          />
        )}
      </div>
      <div className="ed-form">
        <ShotPlanPanel shot={shot} />
        <label className="field" data-guide-field="action">
          <span>
            {tr(restoredExact ? 'workbench.restoredPrompt' : 'storyboard.copy85')}{' '}
            <HelpTip text={tr('help.action')} />
          </span>
          <textarea
            rows={2}
            value={shot.prompt}
            onChange={(e) => upd({ prompt: e.target.value })}
            placeholder={tr('storyboard.finish11')}
          />
        </label>
        {!restoredExact && <ShotLook shot={shot} onChange={upd} />}

        {restoredExact ? (
          <p>{tr('workbench.restoredBindings')}</p>
        ) : (
          <Slot
            onFiles={(files) =>
              dispatch({
                type: 'uploadFiles',
                files,
                target: { kind: 'shot', shot: shot.id, slot: 'smart' },
              })
            }
            id={'slot:cast:' + shot.id}
            guide="cast"
            className="castslot"
            label={tr('storyboard.detail77')}
            hint={tr('storyboard.detail78')}
            help={tr('help.cast')}
            onPick={openPicker('cast')}
          >
            {shot.cast.length || shot.place || shot.props.length ? (
              <div className="castlist">
                {shot.cast.map((c) => {
                  const p = db.profile(c.id);
                  if (!p) return null;
                  const a = db.asset(variantOf(p, c.variant)?.images[0]);
                  return (
                    <div key={c.id} className="castitem character">
                      <AssetThumb asset={a} size={34} />
                      <div>
                        <Button
                          className="link"
                          title={tr('storyboard.finish12')}
                          onClick={() => dispatch({ type: 'openProfile', id: p.id })}
                        >
                          {p.name}
                        </Button>
                        <select
                          className="field-sm"
                          aria-label={p.name + tr('storyboard.detail57')}
                          value={variantOf(p, c.variant)?.id || ''}
                          onChange={(e) =>
                            dispatch({
                              type: 'castVariant',
                              shot: shot.id,
                              id: c.id,
                              variant: e.target.value,
                            })
                          }
                        >
                          {!p.variants.length && (
                            <option value="">{tr('storyboard.copy86')}</option>
                          )}
                          {p.variants.map((v) => (
                            <option key={v.id} value={v.id}>
                              {lookName(v, tr)}
                              {v.images.length ? '' : tr('storyboard.detail58')}
                            </option>
                          ))}
                        </select>
                      </div>
                      <IconButton
                        size="xs"
                        onClick={() => dispatch({ type: 'uncast', shot: shot.id, id: c.id })}
                        label={tr('storyboard.remove')}
                      >
                        <X size={14} />
                      </IconButton>
                    </div>
                  );
                })}
                {shot.place &&
                  (() => {
                    const p = db.profile(shot.place.id);
                    if (!p) return null;
                    return (
                      <div className="castitem location">
                        <AssetThumb asset={coverOf(db, p, shot.place.variant)} size={34} />
                        <div>
                          <Button
                            className="link"
                            title={tr('storyboard.finish12')}
                            onClick={() => dispatch({ type: 'openProfile', id: p.id })}
                          >
                            {p.name}
                          </Button>
                          <select
                            className="field-sm"
                            aria-label={p.name + tr('storyboard.detail59')}
                            value={variantOf(p, shot.place.variant)?.id || ''}
                            onChange={(e) =>
                              dispatch({
                                type: 'placeVariant',
                                shot: shot.id,
                                variant: e.target.value,
                              })
                            }
                          >
                            {!p.variants.length && (
                              <option value="">{tr('storyboard.copy87')}</option>
                            )}
                            {p.variants.map((v) => (
                              <option key={v.id} value={v.id}>
                                {v.name}
                              </option>
                            ))}
                          </select>
                        </div>
                        <IconButton
                          size="xs"
                          onClick={() => dispatch({ type: 'unplace', shot: shot.id })}
                          label={tr('storyboard.remove')}
                        >
                          <X size={14} />
                        </IconButton>
                      </div>
                    );
                  })()}
                {shot.props.map((id) => {
                  const p = db.profile(id);
                  return (
                    p && (
                      <div key={id} className="castitem prop">
                        <AssetThumb asset={coverOf(db, p)} size={34} />
                        <div>
                          <Button
                            className="link"
                            title={tr('storyboard.finish12')}
                            onClick={() => dispatch({ type: 'openProfile', id: p.id })}
                          >
                            {p.name}
                          </Button>
                          <small>{tr('storyboard.copy88')}</small>
                        </div>
                        <IconButton
                          size="xs"
                          onClick={() => dispatch({ type: 'unprop', shot: shot.id, id })}
                          label={tr('storyboard.remove')}
                        >
                          <X size={14} />
                        </IconButton>
                      </div>
                    )
                  );
                })}
              </div>
            ) : null}
          </Slot>
        )}

        {/* #232 (#49): the shot "script" note was never used for generation; only an old
            note stays editable, new shots don't show the box. */}
        {shot.script?.trim() && (
          <label className="field">
            <span>{tr('storyboard.copy84')}</span>
            <textarea
              rows={2}
              value={shot.script}
              onChange={(e) => upd({ script: e.target.value })}
            />
          </label>
        )}
        <div className="modetabs" role="tablist" aria-label={tr('storyboard.finish13')}>
          {['FL', 'REF'].map((k) => (
            <Button
              key={k}
              role="tab"
              title={k === 'FL' ? tr('storyboard.detail60') : tr('storyboard.detail61')}
              aria-selected={info.mode === k}
              className={'mt ' + k.toLowerCase() + (info.mode === k ? ' on' : '')}
              onClick={() => upd({ mode: k })}
            >
              <b>{MODE_LABEL[k]}</b>
            </Button>
          ))}
        </div>
        <p className="mode-help muted">
          {tr(info.mode === 'FL' ? 'storyboard.detail60' : 'storyboard.detail61')}
        </p>
        <div className={'modepanel ' + info.mode.toLowerCase()}>
          {info.frameInputs && (
            <>
              <ShotPicture shot={shot} hasPicture={!!sf?.asset} />
              <div className="slots" data-guide-field="frames">
                <Slot
                  onFiles={(files) =>
                    dispatch({
                      type: 'uploadFiles',
                      files,
                      target: { kind: 'shot', shot: shot.id, slot: 'start' },
                    })
                  }
                  id={'slot:start:' + shot.id}
                  help={tr('help.start')}
                  label={tr('storyboard.detail79')}
                  hint={tr('storyboard.detail81')}
                  onPick={openPicker('start')}
                  onClear={
                    shot.start || shot.continuePrev
                      ? () =>
                          upd(
                            restoredExact ? { start: null } : { start: null, continuePrev: false },
                          )
                      : null
                  }
                >
                  {sf &&
                    (sf.asset ? (
                      <figure>
                        <StoryboardAssetImage asset={sf.asset} />
                        <figcaption>{sf.label}</figcaption>
                      </figure>
                    ) : (
                      <div className="slot-empty">{sf.label}</div>
                    ))}
                </Slot>
                <Slot
                  onFiles={(files) =>
                    dispatch({
                      type: 'uploadFiles',
                      files,
                      target: { kind: 'shot', shot: shot.id, slot: 'end' },
                    })
                  }
                  id={'slot:end:' + shot.id}
                  help={tr('help.end')}
                  label={tr('storyboard.detail80')}
                  hint={tr('storyboard.detail81')}
                  onPick={openPicker('end')}
                  onClear={shot.end ? () => upd({ end: null }) : null}
                >
                  {endA && (
                    <figure>
                      <StoryboardAssetImage asset={endA} />
                      <figcaption>{endA.name}</figcaption>
                    </figure>
                  )}
                </Slot>
              </div>
              {index > 0 && !restoredExact && (
                <label className="check">
                  <input
                    type="checkbox"
                    checked={shot.continuePrev}
                    onChange={(e) =>
                      upd({
                        continuePrev: e.target.checked,
                        start: e.target.checked ? null : shot.start,
                      })
                    }
                  />
                  {tr('storyboard.finish21', {
                    title: shotName(state.shots[index - 1], index - 1, tr),
                  })}
                </label>
              )}
              <label className="field inline-num">
                <span>{tr('storyboard.durationLabel')}</span>
                <input
                  type="number"
                  min={durationRange?.minimum}
                  max={durationRange?.maximum}
                  step={durationRange?.step || 'any'}
                  value={Math.round(shot.duration * 100) / 100}
                  disabled={!durationRange}
                  onChange={(event) => {
                    const n = Number(event.target.value);
                    if (Number.isFinite(n) && n > 0) upd({ duration: n });
                  }}
                />
                {/* The allowed range sits with the field instead of a help icon (owner, 2026-10-27). */}
                <span className="muted">
                  {durationRange
                    ? tr('storyboard.secondsRange', {
                        min: durationRange.minimum,
                        max: durationRange.maximum,
                      })
                    : tr('storyboard.secondsUnit')}
                </span>
                {!durationRange && <small>{tr('storyboard.copy89')}</small>}
              </label>
            </>
          )}
          <ScriptWindows shot={shot} />
          {(info.mode === 'REF' || info.controlInputs) && (
            <>
              <Slot
                onFiles={(files) =>
                  dispatch({
                    type: 'uploadFiles',
                    files,
                    target: { kind: 'shot', shot: shot.id, slot: 'refs' },
                  })
                }
                id={'slot:refs:' + shot.id}
                guide="refs"
                help={tr(info.mode === 'FL' ? 'help.refsFl' : 'help.refsRef')}
                label={info.mode === 'FL' ? tr('storyboard.detail62') : tr('storyboard.detail63')}
                hint={info.mode === 'FL' ? tr('storyboard.detail64') : tr('storyboard.detail65')}
                onPick={openPicker('refs')}
              >
                {shot.refs.length > 0 && (
                  <div className="refs">
                    {shot.refs
                      .map((id) => db.asset(id))
                      .filter(Boolean)
                      .map((a) => (
                        <span key={a.id} className="ref">
                          <AssetThumb asset={a} size={28} />
                          {a.name}
                          <IconButton
                            size="xs"
                            onClick={() => dispatch({ type: 'unref', shot: shot.id, asset: a.id })}
                            label={tr('storyboard.remove')}
                          >
                            <X size={14} />
                          </IconButton>
                        </span>
                      ))}
                  </div>
                )}
              </Slot>
              {info.limitsLabel && <div className="limits">{info.limitsLabel}</div>}
            </>
          )}
        </div>

        <Disclosure className="sent" summary={tr('storyboard.details')}>
          <div className="sent-head">
            <b>{tr('storyboard.copy90')}</b>
            <ModeChip info={info} />
            <span className="muted">
              {info.machine} · {info.res} · {shortSeconds(info.sec)} {tr('storyboard.secondsUnit')}
            </span>
          </div>
          <div className="sent-prompt">
            {info.parts.map((x, i) => (
              <div key={i}>
                <em>{x.label}</em>
                {x.text}
              </div>
            ))}
          </div>
          {info.items.length > 0 && (
            <div className="sent-items">
              {info.items.map((x, i) => (
                <div key={i} className={'sent-item' + (x.sent ? '' : ' off')} title={x.reason}>
                  <AssetThumb asset={x.a} size={40} />
                  <span>
                    <b>{x.kind}</b>
                    <small>{x.why}</small>
                    {!x.sent && (
                      <small className="warn">
                        {tr('storyboard.finish23', { reason: x.reason })}
                      </small>
                    )}
                  </span>
                </div>
              ))}
            </div>
          )}
          {info.warnings.map((w) => (
            <div key={w} className="warn">
              <AlertTriangle size={13} /> {w}
            </div>
          ))}
          {info.notes.map((w) => (
            <div key={w} className="note">
              · {w}
            </div>
          ))}
        </Disclosure>

        {/* No model parameters in the shot editor, for anyone (owner, 2026-10-27): quality,
            shape, sound, seed and copies below are the creator's; raw values belong in admin. */}
        {/* Quality, copies and Generate stay in view at the bottom (owner, 2026-10-24). */}
        <div className="ed-footer">
          <div className="row quality-row">
            <Segmented
              options={['draft', 'final'].map((q) => ({ value: q, label: QUALITY_LABEL[q] }))}
              value={shownQuality}
              onChange={chooseQuality}
            />
            <HelpTip text={tr('storyboard.help.quality')} />
            <small className="muted">
              {info.res} · {shortSeconds(info.sec)} {tr('storyboard.secondsUnit')}
            </small>
          </div>
          <div className="row ed-gen-options">
            <span className="row tight">
              <Segmented
                label={tr('storyboard.aspect')}
                options={[
                  ['16:9', 'storyboard.aspectWide'],
                  ['9:16', 'storyboard.aspectTall'],
                  ['1:1', 'storyboard.aspectSquare'],
                ].map(([value, key]) => ({ value, label: tr(key) }))}
                value={shot.aspect}
                onChange={(aspect) => upd({ aspect })}
              />
              <HelpTip text={tr('storyboard.help.aspect')} />
            </span>
            <label className="check">
              <input
                type="checkbox"
                checked={shot.keepAudio}
                onChange={(e) => upd({ keepAudio: e.target.checked })}
              />
              {tr('storyboard.keepAudio')}
              <HelpTip text={tr('storyboard.keepAudioHelp')} />
            </label>
          </div>
          {/* Takes sit right above Generate (owner, 2026-10-26). Seeds are not a
              creator choice (owner, 2026-11-01); the API keeps them. */}
          <div className="row ed-gen-options">
            <WorkbenchCopies value={copies} onChange={setCopies} max={state.copiesMax} />
          </div>
          {/* Advice right above Generate, always visible (h3-studio#739). */}
          {[...(info.hints || []), ...missing].map((hint) => (
            <p key={hint} className="ed-hint">
              {hint}
            </p>
          ))}
          <PlanMissing shot={shot} editorRef={editorRef} />
          <div className="ed-actions">
            {/* After a take: another version (new seed, same settings) or the same in HD. */}
            {made && (
              <Tooltip content={tr('storyboard.help.anotherVersion')}>
                <Button disabled={regenerating} onClick={() => regenerate({ newSeed: true })}>
                  {tr('storyboard.anotherVersion')}
                </Button>
              </Tooltip>
            )}
            {made && shownQuality !== 'final' && (
              <Tooltip content={tr('storyboard.help.remakeSharp')}>
                <Button disabled={regenerating} onClick={() => regenerate({ quality: 'final' })}>
                  {tr('storyboard.regenerateHd')}
                </Button>
              </Tooltip>
            )}
            <Button
              variant="primary"
              disabled={
                busy ||
                !Number.isInteger(Number(copies)) ||
                Number(copies) < 1 ||
                Number(copies) > state.copiesMax
              }
              onClick={() =>
                dispatch({ type: 'workbenchGenerate', ids: [shot.id], copies: Number(copies) })
              }
            >
              {made ? tr('storyboard.detail67') : tr('storyboard.detail68')} · {qualityLabel}
            </Button>
          </div>
        </div>
        {picker && <AssetPicker shotId={shot.id} {...picker} onClose={() => setPicker(null)} />}
      </div>
    </section>
  );
}

/* ---------- player + timeline ---------- */

export function usePlayer(shots) {
  const segs = useMemo(() => segments(shots), [shots]);
  const total = segs.reduce((n, s) => n + s.dur, 0);
  const [t, setT] = useState(0),
    [playing, setPlaying] = useState(false);
  useEffect(() => {
    if (!playing) return;
    let last = performance.now(),
      raf;
    const step = (now) => {
      const dt = (now - last) / 1000;
      last = now;
      setT((x) => {
        const nx = x + dt;
        if (nx >= total) {
          setPlaying(false);
          return 0;
        }
        return nx;
      });
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [playing, total]);
  const idx = Math.max(
    0,
    segs.findIndex((s) => t >= s.start && t < s.start + s.dur),
  );
  return { segs, total, t, setT, playing, setPlaying, seg: segs[idx], idx };
}

export function Screen({ player, big, style }) {
  const { t: tr } = useI18n();
  const { seg, t, playing } = player,
    ref = useRef(),
    playback = useRef(null),
    local = seg ? t - seg.start : 0;
  const [shapes, setShapes] = useState({}),
    keyOf = (s) => s.shot.id + ':' + s.version?.id,
    mediaKey = seg ? keyOf(seg) : '';
  const aspect = shapes[mediaKey] || 16 / 9;
  // Double buffer (owner, 2026-10-24): the next shot's video is mounted hidden and loads
  // while this one plays, so the cut swaps to an already-buffered element, not black.
  const index = seg ? player.segs.findIndex((s) => s.shot.id === seg.shot.id) : -1,
    following = index >= 0 ? player.segs[index + 1] : null,
    slots = [seg, following].filter(
      (s, i, all) => s?.version && all.findIndex((o) => o && keyOf(o) === keyOf(s)) === i,
    );
  if (!playback.current) playback.current = createStoryboardPlayback();
  const sync = () =>
    playback.current.sync({
      video: ref.current,
      // A shot with kept parts plays them in order from the same source (#512).
      time: sourceTime(seg?.shot.segments, local),
      playing,
      onFailure: () => player.setPlaying(false),
    });
  // Re-sync when the take, playing state, position or the shot's kept parts change; the event
  // reads the latest player and segments without re-running on every render.
  const syncFromEffect = useEffectEvent(sync),
    partsKey = JSON.stringify(seg?.shot.segments || null);
  useEffect(() => syncFromEffect(), [seg?.version?.id, seg?.shot.id, playing, local, partsKey]);
  useEffect(() => () => playback.current.reset(), []);
  if (!seg)
    return (
      <div className="screen" style={style}>
        <div className="screen-empty">{tr('storyboard.copy93')}</div>
      </div>
    );
  const caption = (
    <div className="screen-cap">
      {seg.shot.title}
      <small>{seg.shot.script}</small>
    </div>
  );
  return (
    <div className={'screen' + (big ? ' big' : '')} style={{ ...style, '--media-aspect': aspect }}>
      {seg.version ? (
        <div className="screen-picture">
          {slots.map((slot) => {
            const key = keyOf(slot),
              active = key === mediaKey;
            return (
              <StoryboardVideo
                key={key}
                className={active ? '' : 'screen-buffer'}
                aria-hidden={active ? undefined : true}
                asset={slot.version.asset || slot.version}
                videoRef={active ? ref : undefined}
                preload="auto"
                onLoadedMetadata={(event) => {
                  const video = event.currentTarget;
                  if (video.videoWidth > 0 && video.videoHeight > 0)
                    setShapes((old) => ({ ...old, [key]: video.videoWidth / video.videoHeight }));
                  if (active) sync();
                }}
                onCanPlay={active ? sync : undefined}
              />
            );
          })}
          {caption}
        </div>
      ) : (
        <>
          <div className="screen-empty">
            <b>{seg.shot.title}</b>
            <span>{tr('storyboard.copy94')}</span>
          </div>
          {caption}
        </>
      )}
    </div>
  );
}

export function Transport({ player }) {
  const { t: tr } = useI18n();
  const { playing, setPlaying, t, total, setT } = player;
  return (
    <div className="transport">
      <Button
        variant="primary"
        className="round"
        aria-label={playing ? tr('storyboard.detail69') : tr('storyboard.detail70')}
        disabled={!total}
        onClick={() => setPlaying(!playing)}
      >
        {playing ? <Pause size={16} /> : <Play size={16} />}
      </Button>
      <Button
        variant="ghost"
        aria-label={tr('storyboard.restart')}
        onClick={() => {
          setPlaying(false);
          setT(0);
        }}
      >
        <SkipBack size={16} />
      </Button>
      <span className="time">
        {fmt(t)} / {fmt(total)}
      </span>
    </div>
  );
}

function Segment({
  seg,
  index,
  chapterStart,
  selected,
  onPick,
  compact,
  total,
  numbered,
  tiles = 1,
}) {
  const { t: tr, locale } = useI18n();
  const { state } = useStore();
  const status = shotStatus(seg.shot, queuePositions(state.shots)[seg.shot.id], locale);
  // Drag a segment onto another to reorder the story; a plain click still opens the shot.
  // Only the mouse drags it: Enter opens the shot (cards reorder from their handle).
  const drag = useDraggable({ id: 'seg:' + seg.shot.id }),
    drop = useDroppable({ id: 'segto:' + seg.shot.id });
  const name = shotName(seg.shot, index, tr);
  return (
    <Button
      ref={(node) => {
        drag.setNodeRef(node);
        drop.setNodeRef(node);
      }}
      onPointerDown={drag.listeners?.onPointerDown}
      aria-label={tr('storyboard.detail71') + name}
      // Placed on true time so the ruler, playhead and selection always agree.
      style={{
        left: `${total ? (seg.start / total) * 100 : 0}%`,
        width: `${total ? (seg.dur / total) * 100 : 0}%`,
      }}
      className={
        'tseg' +
        (compact ? ' compact ' + status.tone : '') +
        (chapterStart ? ' chapter-start' : '') +
        (selected ? ' selected' : '') +
        (drag.isDragging ? ' dragging' : '') +
        (drop.isOver && !drag.isDragging ? ' drop-target' : '')
      }
      onClick={(event) => onPick(seg, event)}
      title={`${String(index + 1).padStart(2, '0')} · ${name} · ${fmt(seg.dur)} · ${status.label}`}
    >
      {compact ? (
        // Too many shots for pictures: a coloured square per shot, number when it fits.
        numbered && <span className="tseg-n">{index + 1}</span>
      ) : (
        <>
          {seg.version ? (
            // #232 (#55): a filmstrip of same-size frames, never one stretched picture.
            <span className="tseg-strip">
              {Array.from({ length: tiles }, (_, i) => (
                <StoryboardAssetImage key={i} asset={seg.version.asset || seg.version} />
              ))}
            </span>
          ) : (
            <div className="tseg-empty" />
          )}
          <span className="tseg-title">
            <b>{String(index + 1).padStart(2, '0')}</b> {String(seg.shot.title || '').trim()}
          </span>
          <span className={'tseg-dot ' + status.tone} />
        </>
      )}
    </Button>
  );
}

// Seconds between ruler labels: the smallest of these that keeps labels ~56px apart.
const RULER_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300];

export function Timeline({ player, selectedId, onPick, chapterStarts }) {
  const { t: tr } = useI18n();
  const { segs, total, t, setT } = player;
  const strip = useRef(null),
    [width, setWidth] = useState(0);
  useEffect(() => {
    const el = strip.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const watch = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    watch.observe(el);
    return () => watch.disconnect();
  }, []);
  // Pictures only when an average shot gets at least ~44px; otherwise small squares.
  const compact = width > 0 && segs.length > 0 && width / segs.length < 44;
  // #232 (#51): a click on a shot jumps to that exact moment, not just the shot's start.
  const pick = (seg, event) => {
    const r = strip.current?.getBoundingClientRect();
    setT(
      r?.width && event?.clientX
        ? Math.max(
            seg.start,
            Math.min(seg.start + seg.dur, ((event.clientX - r.left) / r.width) * total),
          )
        : seg.start,
    );
    onPick?.(seg.shot.id);
  };
  // The ruler is the one time bar: press and drag on it to scrub (owner, 2026-10-24); the
  // empty track behind the shots scrubs the same way.
  // `area` is the bar the position is measured on; the playhead itself measures on the strip.
  const scrub = (event, area = event.currentTarget) => {
    if (!total || event.button > 0) return;
    const el = event.currentTarget,
      r = area.getBoundingClientRect();
    const at = (x) => Math.max(0, Math.min(total, ((x - r.left) / r.width) * total));
    el.setPointerCapture(event.pointerId);
    setT(at(event.clientX));
    const move = (e) => setT(at(e.clientX));
    const up = () => {
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  };
  const step =
    RULER_STEPS.find((s) => !width || !total || (s / total) * width >= 56) || RULER_STEPS.at(-1);
  const marks = [];
  for (let at = 0; total && at <= total + 0.001; at += step) marks.push(at);
  const at = `${total ? (t / total) * 100 : 0}%`;
  return (
    <div className={'timeline' + (compact ? ' compact' : '')}>
      <div
        className="track video"
        ref={strip}
        onPointerDown={(event) => event.target === event.currentTarget && scrub(event)}
      >
        {segs.map((seg, index) => (
          <Segment
            key={seg.shot.id}
            seg={seg}
            index={index}
            compact={compact}
            total={total}
            // A number only where the segment is wide enough to show it whole.
            numbered={!!total && (seg.dur / total) * width >= 26}
            // One 16:9 frame per ~89 px of segment (track height 50), at most 24.
            tiles={
              total ? Math.min(24, Math.max(1, Math.round(((seg.dur / total) * width) / 89))) : 1
            }
            chapterStart={index > 0 && !!chapterStarts?.has(seg.shot.id)}
            selected={seg.shot.id === selectedId}
            onPick={pick}
          />
        ))}
        {/* One playhead element (owner 2026-10-31): its grip sits in the lane above the
            pictures and its line runs down the track; nothing is drawn on the ruler. */}
        <div
          className="playhead"
          style={{ left: at }}
          role="presentation"
          onPointerDown={(event) => {
            event.stopPropagation();
            scrub(event, strip.current);
          }}
        />
      </div>
      <div
        className="ruler"
        role="slider"
        tabIndex={0}
        aria-label={tr('storyboard.scrub')}
        aria-valuemin={0}
        aria-valuemax={Math.round(total)}
        aria-valuenow={Math.round(t)}
        onPointerDown={scrub}
        onKeyDown={(event) => {
          const delta = event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowRight' ? 1 : 0;
          if (!delta) return;
          event.preventDefault();
          setT(Math.max(0, Math.min(total, t + delta)));
        }}
      >
        {marks.map((value) => (
          <span key={value} style={{ left: `${(value / total) * 100}%` }}>
            {fmt(value)}
          </span>
        ))}
      </div>
    </div>
  );
}
