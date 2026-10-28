import { useQuery } from '@tanstack/react-query';
import { Disclosure } from './ui/Disclosure.jsx';
import { Columns2, MoreHorizontal, Pencil, RefreshCw, Sparkles, Star, Trash2 } from 'lucide-react';
import { profileLabel } from './model-visibility.js';
import { isActiveTrashItem } from './trash-commands.js';
import { RenameDialog, useTrash } from './UniversalTrash.jsx';
import TakeRecipe from './QuickChatTakeRecipe.jsx';
import React, { useEffect, useEffectEvent, useRef, useState } from 'react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI } from './storyboard-ui-context.jsx';
import { StoryboardVideo, StoryboardAssetImage } from './StoryboardMedia.jsx';
import { recipeChanges } from './storyboard-selection.js';
import { segmentsAction } from './shot-segments.js';
import { Dialog, DialogActions } from './ui/Dialog.jsx';
import { Button, IconButton } from './ui/Button.jsx';
function versionTime(value, locale, t) {
  if (value === null || value === undefined) return t('workbench.unknownStatus');
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isFinite(date.getTime())
    ? date.toLocaleString(locale === 'zh' ? 'zh-CN' : 'en', {
        dateStyle: 'medium',
        timeStyle: 'medium',
      })
    : t('workbench.unknownStatus');
}
export function WorkbenchCopies({ value, onChange, max }) {
  const { t } = useI18n();
  return (
    <label className="copies-field">
      {t('workbench.copies')}{' '}
      <input
        type="number"
        min="1"
        max={max}
        step="1"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        style={{ width: 72 }}
      />
    </label>
  );
}
// #575 (owner, 2026-10-26): burned-in subtitles are flagged, never re-rolled automatically.
const hasSubtitles = (row) =>
  Array.isArray(row?.quality_flags) && row.quality_flags.includes('burned_subtitles');

// Usable/redo verdict and mark count from the batch review summary (#630); shown on both the
// narrow and the wide take cards.
// Kept parts belong to the chosen take (#512): choosing another clears them, after this confirm.
function ChangeTakeDialog({ shot, row, onClose, onConfirm }) {
  const { t } = useI18n();
  if (!row) return null;
  return (
    <Dialog title={t('segments.changeTitle')} onClose={onClose}>
      <p>{t('segments.changeBody', { count: shot.segments.length })}</p>
      <DialogActions>
        <Button onClick={onClose}>{t('segments.changeCancel')}</Button>
        <Button
          variant="primary"
          onClick={() => {
            onClose();
            onConfirm(row);
          }}
        >
          {t('segments.changeConfirm')}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

// Take actions wait while the story is saving or another take action is pending.
function isLocked(state) {
  return state.busy || !!state.workbenchPending;
}

// A shot with exactly one take uses it (owner 2026-10-28): choose it once, after the list is read
// and nothing else is pending. Never re-chooses after the person picks or removes takes.
function useOnlyTakeChosen(versions, data, locked, choose) {
  const done = useRef('');
  const only = data && versions.length === 1 && !versions[0].selected ? versions[0] : null;
  const pick = useEffectEvent((row) => choose(row));
  useEffect(() => {
    if (!only || locked || done.current === only.id) return;
    done.current = only.id;
    pick(only);
  }, [only, locked]);
}

// The shot's take ids and chosen take: the only shot fields the takes list depends on.
function takesKeyOf(shot) {
  return (
    (shot.versions || []).map((v) => v.artifactId || v.id).join(',') + '|' + (shot.chosen || '')
  );
}

// Asks before a take moves to Trash (owner 2026-10-28: delete shows on hover, then confirms).
function DeleteTakeDialog({ project, row, onClose }) {
  const { t } = useI18n(),
    trash = useTrash();
  if (!row) return null;
  const target = { kind: 'artifact', project_id: project.id, id: row.id },
    waiting = !trash || trash.busy || !!trash.pending;
  return (
    <Dialog title={t('workbench.deleteTitle')} onClose={onClose}>
      <p>{t('workbench.deleteBody')}</p>
      <DialogActions>
        <Button onClick={onClose}>{t('segments.changeCancel')}</Button>
        <Button
          variant="danger"
          disabled={waiting}
          onClick={async () => {
            onClose();
            await trash.request('trash', [target]);
          }}
        >
          {t('trash.move')}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

function ReviewBadges({ review }) {
  const { t } = useI18n();
  if (!review || (!review.verdict && !review.mark_count)) return null;
  return (
    <small className="take-review-badges">
      {review.verdict && (
        <span className={'take-verdict ' + review.verdict}>
          {t('review.badge.' + review.verdict)}
        </span>
      )}
      {review.mark_count > 0 && (
        <span className="take-marks">{t('review.badge.marks', { count: review.mark_count })}</span>
      )}
    </small>
  );
}

export function WorkbenchTakes({ shot, wide = false, onPreview, previewId }) {
  const { t, locale } = useI18n(),
    { state, adapter, dispatch } = useStoryboardUI();
  const [renaming, setRenaming] = useState(null),
    [data, setData] = useState(null),
    [compare, setCompare] = useState([]),
    [onlyStar, setOnlyStar] = useState(false),
    [comparing, setComparing] = useState(false),
    [loading, setLoading] = useState(false),
    [error, setError] = useState('');
  const generation = useRef(0);
  const scope = state.contextId + ':' + shot.id;
  // Takes change only when this shot gains/loses a take or its chosen take changes. Other saves
  // (trim, quality, aspect, text) don't touch the list, so they don't read it again (owner
  // 2026-10-28).
  const takesKey = takesKeyOf(shot);
  // Whole-take verdicts and mark counts for every take in one read (h3-studio#631).
  const reviews = useQuery({
    queryKey: ['take-review-summaries', state.contextId, shot.id],
    queryFn: () => adapter.readTakeReviewSummaries(shot.id),
    enabled: !!adapter.readTakeReviewSummaries,
  });
  const reviewOf = (id) => reviews.data?.find((row) => row.artifact_id === id);
  async function load(more = false) {
    const n = ++generation.current;
    setLoading(true);
    setError('');
    try {
      const value = await adapter.readVersions(shot.id, more ? { offset: data.next_offset } : {});
      if (n === generation.current && more && data.project_version !== value.project_version)
        throw Error('workbench.changed');
      if (n === generation.current)
        setData((old) =>
          more ? { ...value, versions: [...(old?.versions || []), ...value.versions] } : value,
        );
    } catch (error) {
      if (n === generation.current)
        setError(
          t(error.status === 404 ? 'workbench.shotUnavailable' : 'workbench.versionsReadFailed'),
        );
    } finally {
      if (n === generation.current) setLoading(false);
    }
  }
  // Reload when the shot or story version changes; the events read the latest adapter/load.
  const reload = useEffectEvent(() => {
    if (adapter.readVersions) load();
  });
  const trashChanged = useEffectEvent((e) => {
    if (e.detail?.items?.some((row) => row.target.project_id === state.project.id)) {
      setCompare([]);
      load();
    }
  });
  // A new shot starts empty; a new take on the same shot keeps the current list on screen while
  // it is read again, so the list never flashes "Reading".
  useEffect(() => {
    setRenaming(null);
    setData(null);
    setCompare([]);
    setOnlyStar(false);
    setComparing(false);
  }, [scope]);
  useEffect(() => {
    reload();
    // Bump on leave so an answer for the previous shot is ignored.
    const counter = generation;
    return () => {
      counter.current++;
    };
  }, [scope, takesKey]);
  useEffect(() => {
    const changed = (e) => trashChanged(e);
    window.addEventListener('inkseq-trash-changed', changed);
    return () => window.removeEventListener('inkseq-trash-changed', changed);
  }, [scope]);
  useEffect(() => {
    if (!wide) setComparing(false);
  }, [wide]);
  const versions = (data?.versions || []).filter(isActiveTrashItem),
    list = onlyStar ? versions.filter((row) => row.starred) : versions,
    media = (row) =>
      shot.versions.find((v) => v.artifactId === row.id || v.id === row.entity_id) || {
        id: row.entity_id,
        asset: { fileId: 'cloud_artifact_' + row.id, kind: row.kind },
      };
  async function action(op) {
    const n = generation.current;
    const result = await dispatch(
      Array.isArray(op)
        ? { type: 'workbenchAction', actions: op }
        : { type: 'workbenchAction', action: op },
    );
    if (n === generation.current && result?.ok !== false) load();
  }
  // Kept parts belong to the chosen take (#512): another take clears them, after a confirm,
  // in the same story edit as the choice.
  const [confirmTake, setConfirmTake] = useState(null);
  const [deleting, setDeleting] = useState(null);
  const locked = isLocked(state);
  useOnlyTakeChosen(versions, data, locked, (row) => choose(row));
  function choose(row) {
    const select = { op: 'storyboard.select', artifact_id: row.id, shot_id: shot.id };
    if (shot.segments?.length && row.id !== shot.chosen) setConfirmTake(row);
    else action(select);
  }
  return (
    <section
      className={'storyboard-takes' + (comparing ? ' comparing' : '')}
      aria-label={t('takes.title')}
    >
      <DeleteTakeDialog project={state.project} row={deleting} onClose={() => setDeleting(null)} />
      <ChangeTakeDialog
        shot={shot}
        row={confirmTake}
        onClose={() => setConfirmTake(null)}
        onConfirm={(row) =>
          action([
            segmentsAction(shot.id, []),
            { op: 'storyboard.select', artifact_id: row.id, shot_id: shot.id },
          ])
        }
      />
      <div className="takes-head">
        <b>{t('takes.title')}</b>
        <small className="muted">{versions.length}</small>
        <div className="spacer" />
        {wide && versions.length > 1 && (
          <Button
            className={'small ghost' + (comparing ? ' on' : '')}
            aria-pressed={comparing}
            onClick={() => {
              setComparing(!comparing);
              if (!comparing && compare.length !== 2)
                setCompare(versions.slice(0, 2).map((row) => row.id));
            }}
          >
            <Columns2 size={13} />
            {comparing ? t('workbench.cancel') : t('takes.compare')}
          </Button>
        )}
        {versions.some((row) => row.starred) && (
          <Button
            className={'small ghost' + (onlyStar ? ' on' : '')}
            aria-pressed={onlyStar}
            onClick={() => setOnlyStar((value) => !value)}
          >
            <Star size={13} />
            {t('workbench.star')}
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          disabled={loading}
          onClick={() => load()}
          aria-label={t('workbench.refresh')}
          title={t('workbench.refresh')}
        >
          <RefreshCw size={13} />
        </Button>
      </div>
      {loading && !data && <p role="status">{t('workbench.loading')}</p>}
      {error && <p role="alert">{error}</p>}
      {!loading && !error && !versions.length && <p className="muted">{t('workbench.empty')}</p>}
      {comparing && (
        <>
          <small className="muted">{t('workbench.compareHint')}</small>
          {compare.length === 2 && (
            <div className="takes-compare">
              {compare.map((id, index) => {
                const row = versions.find((v) => v.id === id),
                  v = row && media(row);
                return (
                  <section className="cmp-side" key={id}>
                    {v &&
                      (row.kind === 'image' ? (
                        <StoryboardAssetImage asset={v.asset || v} />
                      ) : (
                        <StoryboardVideo controls asset={v.asset || v} />
                      ))}
                    <div className="row tight">
                      <b>
                        {index ? 'B' : 'A'} ·{' '}
                        {row?.title ||
                          (Number.isInteger(v?.n)
                            ? 'v' + v.n
                            : versionTime(row?.created_at, locale, t))}
                      </b>
                      <div className="spacer" />
                      <Button
                        variant="primary"
                        size="sm"
                        disabled={state.busy || !!state.workbenchPending || !row}
                        onClick={() => choose(row)}
                      >
                        {t('workbench.use')}
                      </Button>
                    </div>
                    <TakeRecipe
                      take={{ ...row, seed: row?.recipe?.controls?.seed }}
                      state={{
                        capabilities: state.capabilities,
                        materials: state.assets
                          .filter((a) => a.entity?.data.cloudAssetId)
                          .map((a) => ({
                            asset_id: a.entity.data.cloudAssetId,
                            asset: { file_name: a.name },
                          })),
                      }}
                    />
                  </section>
                );
              })}
            </div>
          )}
        </>
      )}
      <div className={wide ? 'take-grid' : 'versions'}>
        {list.map((row) => {
          const index = versions.indexOf(row),
            previous = versions[index + 1],
            changes = recipeChanges(row.recipe, previous?.recipe),
            v = media(row),
            viewing = previewId === v.id;
          const preview = () => {
            if (comparing)
              setCompare((old) =>
                old.includes(row.id)
                  ? old.filter((id) => id !== row.id)
                  : [...old, row.id].slice(-2),
              );
            else onPreview?.(row);
          };
          if (!wide)
            return (
              <div className="ver-wrap" key={row.id}>
                <Button
                  className={'ver' + (row.selected ? ' chosen' : '') + (viewing ? ' viewing' : '')}
                  onClick={preview}
                >
                  {row.kind === 'image' ? (
                    <StoryboardAssetImage asset={v.asset || v} />
                  ) : (
                    <StoryboardVideo asset={v.asset || v} muted />
                  )}
                  <span>
                    {row.title ||
                      (Number.isInteger(v.n) ? 'v' + v.n : versionTime(row.created_at, locale, t))}
                    {row.starred && <Star size={11} aria-label={t('workbench.star')} />}
                  </span>
                  <ReviewBadges review={reviewOf(row.id)} />
                  {row.selected && <em>{t('takes.selected')}</em>}
                  {hasSubtitles(row) && (
                    <em className="ver-warn" title={t('takes.subtitlesHelp')}>
                      {t('takes.subtitles')}
                    </em>
                  )}
                  {/* A take adopted from an old Quick Create conversation (#481). */}
                  {row.aliased && <em className="ver-legacy">{t('workbench.fromQuickCreate')}</em>}
                </Button>
                {/* The narrow panel needs its own "use" action: cloud takes are not always local
                  versions, so the preview above cannot offer one (found in #347 staging check). */}
                {!row.selected && !comparing && (
                  <Button
                    size="sm"
                    className="ver-use"
                    disabled={locked}
                    onClick={() => choose(row)}
                  >
                    {t('workbench.use')}
                  </Button>
                )}
              </div>
            );
          return (
            <article
              className={
                'take' +
                (row.selected ? ' chosen' : '') +
                (viewing && !comparing ? ' viewing' : '') +
                (compare.includes(row.id) && comparing ? ' cmp' : '')
              }
              key={row.id}
            >
              <div
                className="take-thumb"
                role="button"
                tabIndex={0}
                aria-label={row.title || versionTime(row.created_at, locale, t)}
                onClick={preview}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    preview();
                  }
                }}
              >
                {row.kind === 'image' ? (
                  <StoryboardAssetImage asset={v.asset || v} />
                ) : (
                  <StoryboardVideo
                    asset={v.asset || v}
                    muted
                    onMouseEnter={(e) => e.currentTarget.play().catch(() => {})}
                    onMouseLeave={(e) => e.currentTarget.pause()}
                  />
                )}
              </div>
              <div className="take-top">
                {row.selected && <em>{t('takes.selected')}</em>}
                {row.aliased && <em className="ver-legacy">{t('workbench.fromQuickCreate')}</em>}
                {comparing && compare.includes(row.id) && (
                  <em className="cmp-n">{compare.indexOf(row.id) === 0 ? 'A' : 'B'}</em>
                )}
                <div className="spacer" />
                {/* Delete shows on hover and asks first (owner 2026-10-28). */}
                <IconButton
                  size="xs"
                  variant="on-media"
                  className="take-delete"
                  label={t('trash.move')}
                  disabled={locked}
                  onClick={() => setDeleting(row)}
                >
                  <Trash2 size={13} />
                </IconButton>
              </div>
              <div className="take-info">
                <span className="take-name">
                  <b>
                    {row.title ||
                      (Number.isInteger(v.n) ? 'v' + v.n : versionTime(row.created_at, locale, t))}
                  </b>
                  <IconButton
                    size="xs"
                    label={t('trash.rename')}
                    disabled={locked}
                    onClick={() =>
                      setRenaming({ row, scope, projectVersion: state.projectVersion })
                    }
                  >
                    <Pencil size={12} />
                  </IconButton>
                </span>
                <small className="muted">{versionTime(row.created_at, locale, t)}</small>
                {hasSubtitles(row) && (
                  <small className="take-warn" title={t('takes.subtitlesHelp')}>
                    {t('takes.subtitles')}
                  </small>
                )}
                <ReviewBadges review={reviewOf(row.id)} />
                <small className="take-diff">
                  {row.recipe_status === 'known'
                    ? changes === null
                      ? ''
                      : changes.length
                        ? t('workbench.changedFields', {
                            fields: changes
                              .map((k) =>
                                t(
                                  'workbench.' +
                                    ({
                                      recipe_id: 'mode',
                                      deployment_profile_id: 'profile',
                                      controls: 'controls',
                                      inputs: 'inputs',
                                      prompt: 'prompt',
                                    }[k] || 'settings'),
                                ),
                              )
                              .join(', '),
                          })
                        : t('workbench.noChanges')
                    : ''}
                </small>
                <div className="take-acts">
                  {/* One take per shot is used: a radio, already on when it is the only one. */}
                  <label className="take-use">
                    <input
                      type="radio"
                      name={'take-use-' + shot.id}
                      checked={!!row.selected}
                      disabled={locked}
                      onChange={() => choose(row)}
                    />
                    {t('workbench.use')}
                  </label>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={
                      state.busy || !!state.workbenchPending || row.recipe_status !== 'known'
                    }
                    title={t(
                      row.recipe_status === 'known'
                        ? 'workbench.restoreHint'
                        : 'workbench.recipeOld',
                    )}
                    onClick={() =>
                      action({ op: 'storyboard.restore', shot_id: shot.id, artifact_id: row.id })
                    }
                  >
                    {t('workbench.settings')}
                  </Button>
                  {comparing && (
                    <label>
                      <input
                        type="checkbox"
                        checked={compare.includes(row.id)}
                        disabled={!compare.includes(row.id) && compare.length === 2}
                        onChange={() =>
                          setCompare((old) =>
                            old.includes(row.id)
                              ? old.filter((id) => id !== row.id)
                              : [...old, row.id],
                          )
                        }
                      />
                      {t('workbench.compare')}
                    </label>
                  )}
                </div>
              </div>
            </article>
          );
        })}
      </div>
      {Number.isInteger(data?.next_offset) && (
        <Button disabled={loading} onClick={() => load(true)}>
          {t('workbench.more')}
        </Button>
      )}
      {renaming && (
        <RenameDialog
          key={renaming.row.id}
          title={renaming.row.title || ''}
          onClose={() => setRenaming(null)}
          onSave={async (title) => {
            if (renaming.scope !== scope) throw Error('workbench.changed');
            const r = await dispatch({
              type: 'workbenchAction',
              expectedVersion: renaming.projectVersion,
              action: {
                op: 'storyboard.star',
                shot_id: shot.id,
                artifact_id: renaming.row.id,
                title,
              },
            });
            if (r?.ok === false) throw Error('workbench.changed');
            await load();
          }}
        />
      )}
    </section>
  );
}
// Floating bar for selected shots (owner, 2026-10-21): quick selection as links, two main
// actions, and everything else under "More" so the row stays readable.
export function WorkbenchBulk({ ids, onClear, onSelectAll, onSelectPending, pendingCount = 0 }) {
  const { t, locale } = useI18n(),
    { state, dispatch } = useStoryboardUI(),
    trash = useTrash(),
    [copies, setCopies] = useState('1'),
    [scene, setScene] = useState(''),
    [profile, setProfile] = useState(''),
    [more, setMore] = useState(false);
  useEffect(() => {
    setCopies('1');
    setScene('');
    setProfile('');
  }, [state.contextId]);
  useEffect(() => {
    if (!ids.length) setMore(false);
  }, [ids.length]);
  const valid =
    Number.isInteger(Number(copies)) && Number(copies) >= 1 && Number(copies) <= state.copiesMax;
  const locked = isLocked(state);
  if (!ids.length) return null;
  return (
    <div
      className="workbench-bulk"
      role="toolbar"
      aria-label={t('workbench.selected', { count: ids.length })}
    >
      <div className="workbench-bulk-row">
        <b>{t('workbench.selected', { count: ids.length })}</b>
        <span className="workbench-bulk-links">
          <Button className="link" onClick={onSelectAll}>
            {t('workbench.all')}
          </Button>
          {pendingCount > 0 && (
            <Button className="link" onClick={onSelectPending}>
              {t('workbench.onlyPending', { count: pendingCount })}
            </Button>
          )}
          <Button className="link" onClick={onClear}>
            {t('workbench.clear')}
          </Button>
        </span>
        <span className="workbench-bulk-spacer" />
        <Button
          variant="primary"
          disabled={!valid || locked}
          onClick={() =>
            dispatch({ type: 'workbenchGenerate', ids: [...ids], copies: Number(copies) })
          }
        >
          <Sparkles size={14} />
          {t('workbench.generateSelected', { count: ids.length })}
        </Button>
        {trash?.account && (
          <Button
            disabled={locked || trash.busy || !!trash.pending}
            onClick={async () => {
              const receipt = await trash.request(
                'trash',
                ids.map((id) => ({ kind: 'entity', project_id: state.project.id, id })),
              );
              if (receipt?.state === 'completed') onClear();
            }}
          >
            <Trash2 size={14} />
            {t('workbench.trashSelected')}
          </Button>
        )}
        <Button
          variant="ghost"
          aria-expanded={more}
          aria-label={t('workbench.more')}
          title={t('workbench.more')}
          onClick={() => setMore((v) => !v)}
        >
          <MoreHorizontal size={16} />
        </Button>
      </div>
      {more && (
        <div className="workbench-bulk-more">
          <WorkbenchCopies value={copies} onChange={setCopies} max={state.copiesMax} />
          {!state.copiesMax && (
            <Button onClick={() => dispatch({ type: 'workbenchRefresh' })}>
              {t('workbench.refresh')}
            </Button>
          )}
          <span className="workbench-bulk-group">
            {['draft', 'final'].map((operation) => (
              <Button
                key={operation}
                disabled={locked}
                onClick={() =>
                  dispatch({
                    type: 'workbenchAction',
                    profileId: profile || undefined,
                    action: { op: 'storyboard.bulk', shot_ids: [...ids], operation },
                  })
                }
              >
                {t('workbench.' + operation)}
              </Button>
            ))}
            <select
              value={profile}
              onChange={(e) => setProfile(e.target.value)}
              aria-label={t('workbench.profile')}
            >
              <option value="">{t('workbench.keepProfile')}</option>
              {state.capabilities?.deployment_profiles?.map((p) => (
                <option key={p.id} value={p.id}>
                  {profileLabel(p, locale)}
                </option>
              ))}
            </select>
          </span>
          <span className="workbench-bulk-group">
            <select
              aria-label={t('workbench.scene')}
              value={scene}
              onChange={(e) => setScene(e.target.value)}
            >
              <option value="">{t('workbench.scene')}</option>
              {state.scenes?.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.title}
                </option>
              ))}
            </select>
            <Button
              disabled={!scene || locked}
              onClick={() =>
                dispatch({
                  type: 'workbenchAction',
                  action: {
                    op: 'storyboard.bulk',
                    shot_ids: [...ids],
                    operation: 'move',
                    parent_id: scene,
                  },
                })
              }
            >
              {t('workbench.move')}
            </Button>
          </span>
          <WorkbenchFork ids={ids} />
          <small className="muted">{t('workbench.qualityHint')}</small>
        </div>
      )}
    </div>
  );
}

export function WorkbenchFork({ ids }) {
  const { t } = useI18n(),
    { state, dispatch } = useStoryboardUI(),
    [profile, setProfile] = useState('');
  const used = (p) => state.profileUsage?.[p.id] ?? 0;
  useEffect(() => setProfile(''), [state.contextId]);
  if (!ids.length) return null;
  return (
    <Disclosure summary={t('workbench.fork')}>
      <p>{t('workbench.forkHint')}</p>
      <select
        value={profile}
        onChange={(e) => setProfile(e.target.value)}
        aria-label={t('workbench.fork')}
      >
        <option value="">—</option>
        {state.profiles.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name} · {t('workbench.localUsage', { count: used(p) })}
          </option>
        ))}
      </select>
      <Button
        disabled={!profile || !ids.length || state.busy || !!state.workbenchPending}
        onClick={() =>
          dispatch({
            type: 'workbenchAction',
            action: {
              op: 'storyboard.fork_profile',
              profile_id: profile,
              shot_ids: [...ids],
              new_profile_id: 'profile-' + crypto.randomUUID(),
            },
          })
        }
      >
        {t('workbench.fork')}
      </Button>
    </Disclosure>
  );
}
