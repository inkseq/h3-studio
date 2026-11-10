// The World and Library steps as whole pages (owner, 2026-11-10: "the layout was never
// designed"). One toolbar row (title and count, search, categories, New, ⋯), then one card
// grid: a fixed-shape picture, the name, and a line of plain facts. People, places and props
// share the card; the library's files use the same grid. Groups (三兄弟) are a labelled row whose
// members sit in the grid's own columns. Cards keep their size whatever loads or fails.
import React, { useRef, useState } from 'react';
import {
  Box,
  Download,
  Film,
  Image as ImageIcon,
  Inbox,
  MapPin,
  Mic,
  MoreHorizontal,
  Music,
  Play,
  Plus,
  Shirt,
  Upload,
  UserRound,
} from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI as useStore } from './storyboard-ui-context.jsx';
import { Button, IconButton } from './ui/Button.jsx';
import { Dialog } from './ui/Dialog.jsx';
import { Menu, MenuItem } from './ui/Menu.jsx';
import { EmptyState, Note } from './ui/Note.jsx';
import { Segmented } from './ui/Segmented.jsx';
import { StoryboardAssetImage, StoryboardPlayButton, StoryboardVideo } from './StoryboardMedia.jsx';
import { RenameDialog, TrashMenu } from './UniversalTrash.jsx';
import { StoryboardNameDialog } from './StoryboardDialog.jsx';
import { InboxRow } from './StoryboardComponents.jsx';
import { useBibleIO } from './BibleImport.jsx';
import { useSpeaking } from './ProfileBible.jsx';
import { openKey, useCardGrid } from './card-grid.js';
import { assetLabel, secondsLabel } from './storyboard-a11y.js';
import { characterGroups } from './story-bible.js';
import {
  cardFacts,
  coverId,
  duplicateFiles,
  fileOwners,
  libraryOrder,
  looksLikeFileName,
  memberCrop,
  sharedCovers,
} from './stage-page.js';
import './stage-page.css';

const PROFILE_KINDS = ['character', 'location', 'prop'];
// Width / height of each kind's picture frame (stage-page.css uses the same shapes).
const FRAME = { character: 3 / 4, location: 16 / 9, prop: 1, file: 4 / 3 };

function tabsFor(t, page, state) {
  const inbox = state.assets.filter((a) => a.inbox);
  const tabs =
    page === 'world'
      ? [
          ['character', t('storyboard.detail10'), UserRound],
          ['location', t('storyboard.detail11'), MapPin],
          ['prop', t('storyboard.copy88'), Box],
        ]
      : [
          ['image', t('storyboard.detail9'), ImageIcon],
          ['video', t('library.tab.video'), Film],
          ['audio', t('storyboard.copy51'), Music],
          ['inbox', t('storyboard.detail13'), Inbox],
        ];
  const count = (k) =>
    k === 'inbox'
      ? inbox.length
      : PROFILE_KINDS.includes(k)
        ? state.profiles.filter((p) => p.type === k).length
        : state.assets.filter((a) => a.kind === k && !a.inbox).length;
  return { tabs, count, inbox };
}

export function StagePage({ page }) {
  const { t } = useI18n();
  const { state, dispatch } = useStore();
  // Each page keeps its own category and search while the story is open.
  const [tabs, setTabs] = useState({}),
    [searches, setSearches] = useState({}),
    [zoom, setZoom] = useState(null),
    [newType, setNewType] = useState(null);
  const { tabs: TABS, count, inbox } = tabsFor(t, page, state);
  const tab = TABS.some(([k]) => k === tabs[page]) ? tabs[page] : TABS[0][0],
    q = searches[page] || '',
    setTab = (value) => setTabs((old) => ({ ...old, [page]: value })),
    setQ = (value) => setSearches((old) => ({ ...old, [page]: value }));
  const grid = useCardGrid('[data-grid-card]');
  const upload = useRef(null);
  const io = useBibleIO();
  const world = page === 'world';
  const kindLabel = (k) => t('storyboard.labels.TYPE_LABEL.' + k);
  const newLabel = world ? t('storyboard.mixed16', { kind: kindLabel(tab) }) : '';
  const heading = t(world ? 'stage.world' : 'stage.library');
  const total = world ? state.profiles.length : state.assets.length;
  const totalLabel = world
    ? t(total === 1 ? 'storyboard.oneProfile' : 'storyboard.profiles', { count: total })
    : t(total === 1 ? 'storyboard.oneFile' : 'storyboard.files', { count: total });
  const match = (text) => !q || text.toLowerCase().includes(q.trim().toLowerCase());
  const create = () => {
    if (!state.busy) setNewType(tab);
  };
  const tabLabel = TABS.find(([k]) => k === tab)?.[1] || '';

  return (
    <section className="stage-page" data-page={page} aria-label={heading}>
      <header className="sp-bar">
        <div className="sp-title">
          <h2>{heading}</h2>
          <small>{totalLabel}</small>
        </div>
        <input
          className="sp-search"
          type="search"
          aria-label={t(world ? 'stage.searchWorld' : 'stage.searchFiles')}
          placeholder={t(world ? 'stage.searchWorld' : 'stage.searchFiles')}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <Segmented
          tabs
          className="sp-tabs"
          label={t('stage.categories')}
          value={tab}
          onChange={setTab}
          options={TABS.map(([k, label, Icon]) => ({
            value: k,
            title: label,
            label: (
              <span className="sp-tab">
                <Icon size={14} aria-hidden="true" />
                <span>{label}</span>
                <small className={k === 'inbox' && count(k) ? 'sp-count alert' : 'sp-count'}>
                  {count(k)}
                </small>
              </span>
            ),
          }))}
        />
        <div className="sp-actions">
          {world ? (
            <>
              <Button
                variant="primary"
                size="sm"
                className="sp-new"
                aria-label={newLabel}
                title={newLabel}
                onClick={create}
              >
                <Plus size={14} aria-hidden="true" />
                {t('stage.new')}
              </Button>
              <Menu
                label={t('stage.more')}
                trigger={
                  <IconButton size="sm" label={t('stage.more')}>
                    <MoreHorizontal size={16} aria-hidden="true" />
                  </IconButton>
                }
              >
                <MenuItem icon={Upload} onSelect={io.choose}>
                  {t('bible.import')}
                </MenuItem>
                <MenuItem icon={Download} onSelect={io.download}>
                  {t('bible.export')}
                </MenuItem>
              </Menu>
            </>
          ) : (
            <Button
              variant="primary"
              size="sm"
              className="sp-new"
              onClick={() => upload.current?.click()}
            >
              <Upload size={14} aria-hidden="true" />
              {t('storyboard.copy42')}
            </Button>
          )}
          <input
            ref={upload}
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
      </header>

      <div className="sp-body" role="tabpanel" aria-label={tabLabel}>
        {PROFILE_KINDS.includes(tab) ? (
          <WorldGrid
            kind={tab}
            gridRef={grid}
            q={q}
            match={match}
            onNew={create}
            newLabel={newLabel}
          />
        ) : tab === 'inbox' ? (
          <div className="sp-inbox">
            {inbox.length ? (
              <>
                <Note className="sp-inbox-help">{t('stage.inboxHelp')}</Note>
                <div className="inbox">
                  {inbox.map((a) => (
                    <InboxRow key={a.id} a={a} />
                  ))}
                </div>
              </>
            ) : (
              <EmptyState icon={<Inbox size={22} />} title={t('storyboard.copy45')} />
            )}
          </div>
        ) : (
          <FileGrid kind={tab} gridRef={grid} q={q} match={match} onZoom={setZoom} />
        )}
      </div>

      {world && io.node}
      {zoom && (
        // A real dialog: Escape or a click outside closes it and focus goes back to the file.
        <Dialog
          title={assetLabel(zoom, t, { full: true })}
          eyebrow={t('storyboard.detail74')}
          onClose={() => setZoom(null)}
          portal
          wide
          className="sb-portal density-compact lib-preview"
        >
          <div className="lib-preview-media">
            {zoom.kind === 'video' ? (
              <StoryboardVideo asset={zoom} controls />
            ) : zoom.kind === 'audio' ? (
              <StoryboardPlayButton asset={zoom} label={t('library.play')} />
            ) : (
              <StoryboardAssetImage asset={zoom} />
            )}
          </div>
        </Dialog>
      )}
      {newType && (
        <StoryboardNameDialog
          title={t('storyboard.mixed9', { p0: kindLabel(newType) })}
          label={t('storyboard.mixed10', { p0: kindLabel(newType) })}
          onSubmit={(name) => dispatch({ type: 'newProfile', ptype: newType, name })}
          onClose={() => setNewType(null)}
        />
      )}
    </section>
  );
}

const TIPS = {
  character: 'stage.worldCharacterTip',
  location: 'storyboard.detail19',
  prop: 'storyboard.detail20',
};

function WorldGrid({ kind, gridRef, q, match, onNew, newLabel }) {
  const { t } = useI18n();
  const { state, dispatch, db } = useStore();
  const all = state.profiles.filter((p) => p.type === kind);
  const shown = all.filter((p) => match(p.name + ' ' + (p.desc || '')));
  const shared = sharedCovers(all, (id) => db.asset(id));
  const inShots = new Set(
    state.shots.flatMap((s) => [...s.cast.map((c) => c.id), s.place?.id, ...s.props]),
  );
  if (!all.length)
    return (
      <EmptyState
        className="sp-empty"
        title={t('storyboard.libEmpty', { kind: t('storyboard.labels.TYPE_LABEL.' + kind) })}
        action={
          <Button variant="primary" size="sm" onClick={onNew}>
            <Plus size={14} aria-hidden="true" />
            {newLabel}
          </Button>
        }
      >
        {t(TIPS[kind])}
      </EmptyState>
    );
  if (!shown.length)
    return <EmptyState className="sp-empty" title={t('stage.noMatch', { q: q.trim() })} />;
  const card = (p) => (
    <WorldCard
      key={p.id}
      p={p}
      shared={shared.get(p.id)}
      used={inShots.has(p.id)}
      onOpen={() => dispatch({ type: 'openProfile', id: p.id })}
    />
  );
  return (
    <div
      className="sp-grid"
      data-kind={kind}
      ref={gridRef}
      onContextMenu={(event) => {
        // Right-click on empty grid space: a new card of this kind (a shortcut to New).
        if (event.target.closest('[data-grid-card]')) return;
        event.preventDefault();
        onNew();
      }}
    >
      {characterGroups(shown).map(({ group, members }) => {
        if (!group) return card(members[0]);
        // One shared picture among all of them: say so once, on the group.
        const together = members.every((p) => shared.get(p.id)?.n >= members.length);
        return (
          <div
            key={'group:' + group}
            className="sp-group"
            role="group"
            aria-label={group + ' · ' + t('bible.groupCount', { count: members.length })}
          >
            <div className="sp-group-head">
              <b>{group}</b>
              <small>{t('bible.groupCount', { count: members.length })}</small>
              {together && <small className="sp-group-note">{t('stage.groupShared')}</small>}
            </div>
            {members.map(card)}
          </div>
        );
      })}
    </div>
  );
}

function Fact({ icon: Icon, n, text }) {
  return (
    <span className="sp-fact" title={text}>
      <Icon size={12} aria-hidden="true" />
      <span aria-hidden="true">{n}</span>
      <span className="visually-hidden">{text}</span>
    </span>
  );
}

// A person, place or prop. Enter or Space opens it (card-grid.js moves between cards).
function WorldCard({ p, shared, used, onOpen }) {
  const { t } = useI18n();
  const { db, state } = useStore();
  const speaking = useSpeaking(p);
  const facts = cardFacts(p, { speaking, used });
  const cover = db.asset(coverId(p));
  const meta = cover?.entity?.data?.metadata || {};
  const crop =
    shared &&
    memberCrop({
      n: shared.n,
      index: shared.index,
      width: meta.width,
      height: meta.height,
      frame: FRAME[p.type],
    });
  // Without the picture's size, still lean towards this person's part of a shared picture.
  // As variables, so a picture that fails to load keeps the plain frame and its icon.
  const style = crop
    ? {
        '--crop-left': crop.left + '%',
        '--crop-top': crop.top + '%',
        '--crop-width': crop.width + '%',
        '--crop-height': crop.height + '%',
      }
    : shared
      ? { objectPosition: ((shared.index + 0.5) / shared.n) * 100 + '% 0%' }
      : undefined;
  const typeLabel = t('storyboard.labels.TYPE_LABEL.' + p.type);
  const checklist = facts.items
    .map((item) => (item.done ? '✓ ' : '· ') + t('bible.check.' + p.type + '.' + item.key))
    .join('\n');
  const looks =
    p.type === 'character'
      ? t(facts.looks === 1 ? 'storyboard.oneLook' : 'storyboard.looks', { count: facts.looks })
      : t(facts.looks === 1 ? 'storyboard.oneVersion' : 'storyboard.versions', {
          count: facts.looks,
        });
  return (
    <div
      role="group"
      aria-label={t('library.profileCard', { type: typeLabel, name: p.name })}
      data-grid-card={p.id}
      className={'sp-card ' + p.type}
      onKeyDown={(event) => openKey(event, onOpen)}
      onClick={onOpen}
      title={p.desc || undefined}
    >
      <div className="sp-media" data-frame={p.type}>
        {cover ? (
          <StoryboardAssetImage asset={cover} className={crop ? 'sp-crop' : ''} style={style} />
        ) : (
          <span className="sp-initial" aria-hidden="true">
            {Array.from((p.name || '').trim())[0]?.toLocaleUpperCase() || '·'}
          </span>
        )}
        {shared && (
          <span className="sp-media-tag" title={t('stage.sharedPhotoHelp', { count: shared.n })}>
            {t('stage.sharedPhoto')}
            <span className="visually-hidden">
              {t('stage.sharedPhotoHelp', { count: shared.n })}
            </span>
          </span>
        )}
      </div>
      <div className="sp-card-body">
        <div className="sp-name">
          <b title={p.name}>{p.name}</b>
          {p.bible?.speaker && <span className="bible-speaker">{p.bible.speaker}</span>}
          <TrashMenu
            target={{ kind: 'entity', project_id: state.project.id, id: p.id }}
            onRename={onOpen}
            onEdit={onOpen}
          />
        </div>
        <div className="sp-facts">
          {p.type !== 'prop' && <Fact icon={Shirt} n={facts.looks} text={looks} />}
          <Fact
            icon={ImageIcon}
            n={facts.photos}
            text={t(facts.photos === 1 ? 'storyboard.oneImage' : 'storyboard.images', {
              count: facts.photos,
            })}
          />
          {p.type === 'character' && (
            <Fact
              icon={Mic}
              n={facts.voices}
              text={t(facts.voices === 1 ? 'storyboard.oneVoice' : 'storyboard.voices', {
                count: facts.voices,
              })}
            />
          )}
        </div>
        {/* What is missing, in words; how much of the setup is done sits at the end. */}
        <div className="sp-flags">
          {facts.gaps.map((gap) => (
            <span key={gap} className="sp-flag" title={t('stage.gapHelp.' + gap)}>
              {t('stage.gap.' + gap)}
            </span>
          ))}
          <span className="sp-setup" title={checklist}>
            {t('stage.setup', { done: facts.done, total: facts.total })}
          </span>
        </div>
      </div>
    </div>
  );
}

function FileGrid({ kind, gridRef, q, match, onZoom }) {
  const { t } = useI18n();
  const { state } = useStore();
  const owners = fileOwners(state.profiles);
  const all = state.assets.filter((a) => a.kind === kind && !a.inbox);
  const duplicates = duplicateFiles(all);
  const names = (a) => (owners.get(a.id) || []).map((o) => o.name).join(t('bible.listSep'));
  const kinds = (a) =>
    [
      ...new Set((owners.get(a.id) || []).map((o) => t('storyboard.labels.TYPE_LABEL.' + o.type))),
    ].join(t('bible.listSep'));
  const title = (a) =>
    looksLikeFileName(a.name) && names(a)
      ? t(a.kind === 'audio' ? 'stage.soundOf' : 'stage.photoOf', { names: names(a) })
      : assetLabel(a, t);
  const shown = libraryOrder(
    all.filter((a) => match(a.name + ' ' + title(a) + ' ' + names(a))),
    duplicates,
  );
  if (!all.length)
    return (
      <EmptyState
        className="sp-empty"
        title={t('storyboard.libEmpty', {
          kind: t(
            kind === 'image'
              ? 'storyboard.detail9'
              : kind === 'video'
                ? 'library.tab.video'
                : 'storyboard.copy51',
          ),
        })}
      />
    );
  if (!shown.length)
    return <EmptyState className="sp-empty" title={t('stage.noMatch', { q: q.trim() })} />;
  return (
    <div className="sp-grid" data-kind="file" ref={gridRef}>
      {shown.map((a) => (
        <FileCard
          key={a.id}
          asset={a}
          title={title(a)}
          // Under a friendly name the file's own name stays readable; else which cards use it.
          detail={
            title(a) !== assetLabel(a, t)
              ? a.name
              : !names(a)
                ? ''
                : names(a) === title(a)
                  ? t(a.kind === 'audio' ? 'stage.onCardSound' : 'stage.onCard', { kind: kinds(a) })
                  : t('stage.usedBy', { names: names(a) })
          }
          duplicate={duplicates.get(a.id)}
          onZoom={onZoom}
        />
      ))}
    </div>
  );
}

// A library file. Enter, Space or a click opens its preview.
function FileCard({ asset, title, detail, duplicate, onZoom }) {
  const { t } = useI18n();
  const { state, dispatch } = useStore();
  const [renaming, setRenaming] = useState(false);
  const length = secondsLabel(asset.seconds, t);
  const full = assetLabel(asset, t, { full: true });
  // The rename dialog sits beside the card, not in it, so its clicks never open the preview.
  return (
    <>
      <div
        role="group"
        aria-label={title === assetLabel(asset, t) ? full : title + ' · ' + full}
        data-grid-card={asset.id}
        className="sp-card file"
        onClick={() => onZoom(asset)}
        onKeyDown={(event) => openKey(event, () => onZoom(asset))}
        title={full}
      >
        <div className="sp-media" data-frame={asset.kind === 'audio' ? 'sound' : 'file'}>
          {asset.kind === 'audio' ? (
            <span className="sp-initial sp-audio" aria-hidden="true">
              <Music size={28} />
            </span>
          ) : (
            <StoryboardAssetImage asset={asset} />
          )}
          {asset.kind === 'video' && length && (
            <span className="sp-media-tag end">
              <Play size={11} aria-hidden="true" /> {length}
            </span>
          )}
          {duplicate && (
            <span
              className="sp-media-tag warn"
              title={t('stage.duplicateHelp', { count: duplicate.n })}
            >
              {t('stage.duplicate', { count: duplicate.n })}
              <span className="visually-hidden">
                {t('stage.duplicateHelp', { count: duplicate.n })}
              </span>
            </span>
          )}
        </div>
        <div className="sp-card-body">
          <div className="sp-name">
            {asset.kind === 'audio' && (
              // Playing a sound is not opening the card.
              <span className="sp-play" onClick={(event) => event.stopPropagation()}>
                <StoryboardPlayButton asset={asset} />
              </span>
            )}
            <b title={title}>{title}</b>
            {asset.entity?.id && (
              <TrashMenu
                target={{ kind: 'entity', project_id: state.project.id, id: asset.entity.id }}
                onRename={() => setRenaming(true)}
                onEdit={() => setRenaming(true)}
              />
            )}
          </div>
          <div className="sp-facts">
            <span className="sp-detail" title={detail || undefined}>
              {detail || (asset.kind === 'audio' ? length : '')}
            </span>
          </div>
        </div>
      </div>
      {renaming && (
        <RenameDialog
          key={state.contextId + ':' + asset.id}
          title={asset.name}
          onClose={() => setRenaming(false)}
          onSave={async (name) => {
            const result = await dispatch({ type: 'assetTitle', asset: asset.id, title: name });
            if (result?.ok === false || result === false) throw Error('trash.changed');
          }}
        />
      )}
    </>
  );
}
