import { FloatingMessage } from './floating-notice.js';
import { Columns2, MoreHorizontal, RefreshCw, Sparkles, Star, Trash2 } from 'lucide-react';
import { profileLabel } from './model-visibility.js';
import { isActiveTrashItem } from './trash-commands.js';
import { TrashMenu, RenameDialog, useTrash } from './UniversalTrash.jsx';
import TakeRecipe from './QuickChatTakeRecipe.jsx';
import GenerationControl from './GenerationControl.jsx';
import { effectiveControlSchema, recipeFor } from './quick-chat-model.js';
import React, { useEffect, useRef, useState } from 'react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { useStoryboardUI } from './storyboard-ui-context.jsx';
import { StoryboardVideo, StoryboardAssetImage } from './StoryboardMedia.jsx';
import { recipeChanges } from './storyboard-selection.js';
import { segmentsAction } from './shot-segments.js';
import { Dialog, DialogActions } from './ui/Dialog.jsx';
import { Button } from './ui/Button.jsx';
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
  useEffect(() => {
    setRenaming(null);
    setData(null);
    setCompare([]);
    setOnlyStar(false);
    setComparing(false);
    if (adapter.readVersions) load();
    return () => {
      generation.current++;
    };
  }, [scope, state.projectVersion]);
  useEffect(() => {
    const changed = (e) => {
      if (e.detail?.items?.some((row) => row.target.project_id === state.project.id)) {
        setCompare([]);
        load();
      }
    };
    window.addEventListener('inkseq-trash-changed', changed);
    return () => window.removeEventListener('inkseq-trash-changed', changed);
  }, [scope, state.projectVersion]);
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
      {confirmTake && (
        <Dialog title={t('segments.changeTitle')} onClose={() => setConfirmTake(null)}>
          <p>{t('segments.changeBody', { count: shot.segments.length })}</p>
          <DialogActions>
            <Button onClick={() => setConfirmTake(null)}>{t('segments.changeCancel')}</Button>
            <Button
              variant="primary"
              onClick={() => {
                const row = confirmTake;
                setConfirmTake(null);
                action([
                  segmentsAction(shot.id, []),
                  { op: 'storyboard.select', artifact_id: row.id, shot_id: shot.id },
                ]);
              }}
            >
              {t('segments.changeConfirm')}
            </Button>
          </DialogActions>
        </Dialog>
      )}
      <div className="takes-head">
        <b>{t('takes.title')}</b>
        <small className="muted">{versions.length}</small>
        <div className="spacer" />
        {wide && versions.length > 1 && (
          <button
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
          </button>
        )}
        {versions.some((row) => row.starred) && (
          <button
            className={'small ghost' + (onlyStar ? ' on' : '')}
            aria-pressed={onlyStar}
            onClick={() => setOnlyStar((value) => !value)}
          >
            <Star size={13} />
            {t('workbench.star')}
          </button>
        )}
        <button
          className="small ghost"
          disabled={loading}
          onClick={() => load()}
          aria-label={t('workbench.refresh')}
          title={t('workbench.refresh')}
        >
          <RefreshCw size={13} />
        </button>
      </div>
      {loading && <p role="status">{t('workbench.loading')}</p>}
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
                      <button
                        className="small primary"
                        disabled={state.busy || !!state.workbenchPending || !row}
                        onClick={() => choose(row)}
                      >
                        {t('workbench.use')}
                      </button>
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
                <button
                  type="button"
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
                  {row.selected && <em>{t('takes.selected')}</em>}
                  {/* A take adopted from an old Quick Create conversation (#481). */}
                  {row.aliased && <em className="ver-legacy">{t('workbench.fromQuickCreate')}</em>}
                </button>
                {/* The narrow panel needs its own "use" action: cloud takes are not always local
                  versions, so the preview above cannot offer one (found in #347 staging check). */}
                {!row.selected && !comparing && (
                  <button
                    type="button"
                    className="small ver-use"
                    disabled={state.busy || !!state.workbenchPending}
                    onClick={() => choose(row)}
                  >
                    {t('workbench.use')}
                  </button>
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
                <button
                  className={'take-star' + (row.starred ? ' on' : '')}
                  aria-label={t(row.starred ? 'takes.unstar' : 'takes.star')}
                  aria-pressed={row.starred}
                  disabled={state.busy || !!state.workbenchPending}
                  onClick={() =>
                    action({
                      op: 'storyboard.star',
                      shot_id: shot.id,
                      artifact_id: row.id,
                      starred: !row.starred,
                    })
                  }
                >
                  <Star size={13} fill={row.starred ? 'currentColor' : 'none'} />
                </button>
              </div>
              <div className="take-info">
                <b>
                  {row.title ||
                    (Number.isInteger(v.n) ? 'v' + v.n : versionTime(row.created_at, locale, t))}
                </b>
                <small className="muted">{versionTime(row.created_at, locale, t)}</small>
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
                    : t('workbench.unknown')}
                </small>
                <div className="take-acts">
                  <button
                    className="small"
                    disabled={state.busy || !!state.workbenchPending}
                    aria-pressed={row.selected}
                    onClick={() => choose(row)}
                  >
                    {t('workbench.use')}
                  </button>
                  <button
                    className="small ghost"
                    disabled={
                      state.busy || !!state.workbenchPending || row.recipe_status !== 'known'
                    }
                    title={t('workbench.restoreHint')}
                    onClick={() =>
                      action({ op: 'storyboard.restore', shot_id: shot.id, artifact_id: row.id })
                    }
                  >
                    {t('workbench.settings')}
                  </button>
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
                  <TrashMenu
                    target={{ kind: 'artifact', project_id: state.project.id, id: row.id }}
                    onRename={() =>
                      setRenaming({ row, scope, projectVersion: state.projectVersion })
                    }
                    onEdit={
                      row.recipe_status === 'known'
                        ? () =>
                            action({
                              op: 'storyboard.restore',
                              shot_id: shot.id,
                              artifact_id: row.id,
                            })
                        : undefined
                    }
                  />
                </div>
              </div>
            </article>
          );
        })}
      </div>
      {Number.isInteger(data?.next_offset) && (
        <button disabled={loading} onClick={() => load(true)}>
          {t('workbench.more')}
        </button>
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
  const locked = state.busy || !!state.workbenchPending;
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
          <button type="button" className="link" onClick={onSelectAll}>
            {t('workbench.all')}
          </button>
          {pendingCount > 0 && (
            <button type="button" className="link" onClick={onSelectPending}>
              {t('workbench.onlyPending', { count: pendingCount })}
            </button>
          )}
          <button type="button" className="link" onClick={onClear}>
            {t('workbench.clear')}
          </button>
        </span>
        <span className="workbench-bulk-spacer" />
        <button
          className="primary"
          disabled={!valid || locked}
          onClick={() =>
            dispatch({ type: 'workbenchGenerate', ids: [...ids], copies: Number(copies) })
          }
        >
          <Sparkles size={14} />
          {t('workbench.generateSelected', { count: ids.length })}
        </button>
        {trash?.account && (
          <button
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
          </button>
        )}
        <button
          type="button"
          className="ghost"
          aria-expanded={more}
          aria-label={t('workbench.more')}
          title={t('workbench.more')}
          onClick={() => setMore((v) => !v)}
        >
          <MoreHorizontal size={16} />
        </button>
      </div>
      {more && (
        <div className="workbench-bulk-more">
          <WorkbenchCopies value={copies} onChange={setCopies} max={state.copiesMax} />
          {!state.copiesMax && (
            <button onClick={() => dispatch({ type: 'workbenchRefresh' })}>
              {t('workbench.refresh')}
            </button>
          )}
          <span className="workbench-bulk-group">
            {['draft', 'final'].map((operation) => (
              <button
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
              </button>
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
            <button
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
            </button>
          </span>
          <WorkbenchFork ids={ids} />
          <small className="muted">{t('workbench.qualityHint')}</small>
        </div>
      )}
    </div>
  );
}

export function WorkbenchDefaults({ shotId, initialTarget = 'shot' }) {
  const { t, locale } = useI18n(),
    { state, adapter, dispatch } = useStoryboardUI();
  const [data, setData] = useState(null),
    [target, setTarget] = useState('shot'),
    [draft, setDraft] = useState(null),
    [max, setMax] = useState(''),
    [error, setError] = useState('');
  const generation = useRef(0),
    panel = useRef(null),
    scope = state.contextId + ':' + shotId;
  async function load() {
    const n = ++generation.current;
    setError('');
    try {
      const [versions, prefs, copies] = await Promise.all([
        adapter.readVersions(shotId),
        adapter.readPreferences(),
        adapter.readCopies(),
      ]);
      if (n !== generation.current) return;
      setData({ versions, prefs, copies });
      setMax(String(copies.copies_max));
      setDraft(null);
    } catch (error) {
      if (n === generation.current)
        setError(
          t(error.status === 404 ? 'workbench.shotUnavailable' : 'workbench.versionsReadFailed'),
        );
    }
  }
  useEffect(() => {
    setDraft(null);
    setData(null);
    setTarget(initialTarget);
    if (initialTarget === 'account' && panel.current) panel.current.open = true;
    load();
    return () => {
      generation.current++;
    };
  }, [scope, initialTarget]);
  const shot = state.shots.find((row) => row.id === shotId);
  const authored =
    target === 'account'
      ? data?.prefs?.defaults
      : target === 'story'
        ? data?.versions?.defaults?.story
        : data?.versions?.defaults?.shot;
  const value = draft || authored || {},
    effective =
      (target === 'account'
        ? {}
        : target === 'story'
          ? data?.versions?.defaults?.global
          : data?.versions?.defaults?.effective) || {},
    display = { ...effective, ...value, controls: { ...effective.controls, ...value.controls } };
  const recipe = recipeFor(state.capabilities, display),
    profiles = state.capabilities?.deployment_profiles || [];
  const field = (key, v) => setDraft((old) => ({ ...structuredClone(old || value), [key]: v }));
  const control = (key, v) =>
    setDraft((old) => ({
      ...structuredClone(old || value),
      controls: { ...(old || value).controls, [key]: v },
    }));
  const inherit = (key) =>
    setDraft((old) => {
      const next = structuredClone(old || value);
      if (key === 'recipe_id' || key === 'deployment_profile_id') delete next[key];
      else if (next.controls) delete next.controls[key];
      return next;
    });
  async function save() {
    if (!data) return;
    const frozen = structuredClone(value);
    if (target !== 'shot') {
      delete frozen.controls?.duration;
      delete frozen.controls?.seed;
    }
    for (const key of ['recipe_id', 'deployment_profile_id']) if (!frozen[key]) delete frozen[key];
    const n = generation.current;
    const result = await dispatch(
      target === 'account'
        ? {
            type: 'workbenchPreferences',
            body: { expected_version: data.prefs.version, defaults: frozen },
          }
        : target === 'story'
          ? {
              type: 'workbenchAction',
              expectedVersion: data.versions.project_version,
              action: { op: 'storyboard.defaults', value: frozen },
            }
          : {
              type: 'workbenchShotSettings',
              shotId,
              expectedVersion: data.versions.project_version,
              value: frozen,
            },
    );
    if (n === generation.current && result?.ok !== false) await load();
  }
  async function saveCopies() {
    const n = generation.current;
    const result = await dispatch({
      type: 'workbenchCopies',
      body: {
        expected_version: data.copies.version,
        copies_max: Number(max),
        takes_fold_count: data.copies.takes_fold_count,
      },
    });
    if (n === generation.current && result?.ok !== false)
      setData((old) => ({ ...old, copies: result.receipt }));
  }
  return (
    <details ref={panel} className="workbench-defaults">
      <summary>{t('workbench.defaults')}</summary>
      <p>{t('workbench.durationSeed')}</p>
      {error && <p role="alert">{error}</p>}
      <button disabled={state.busy || !!state.workbenchPending} onClick={load}>
        {t('workbench.reloadDefaults')}
      </button>
      <fieldset disabled={!data || state.busy || !!state.workbenchPending}>
        <select
          aria-label={t('workbench.defaults')}
          value={target}
          onChange={(e) => {
            setTarget(e.target.value);
            setDraft(null);
          }}
        >
          {['shot', 'story', 'account'].map((key) => (
            <option value={key} key={key}>
              {t('workbench.' + key)}
            </option>
          ))}
        </select>
        <label>
          {t('workbench.profile')}
          <select
            value={value.deployment_profile_id || ''}
            onChange={(e) => field('deployment_profile_id', e.target.value)}
          >
            <option value="">{t('workbench.followDefaults')}</option>
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {profileLabel(p, locale)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('workbench.mode')}
          <select
            value={value.recipe_id || ''}
            onChange={(e) => field('recipe_id', e.target.value)}
          >
            <option value="">{t('workbench.followDefaults')}</option>
            {state.capabilities?.recipes
              ?.filter((r) => ['h3-base-fl2va-v1', 'h3-base-ref2va-v1'].includes(r.id))
              .map((r) => (
                <option key={r.id} value={r.id}>
                  {t(r.mode === 'ref' ? 'workspace.status14' : 'workspace.status13')}
                </option>
              ))}
          </select>
        </label>
        {Object.entries(effectiveControlSchema(recipe))
          .filter(([key]) => target === 'shot' || !['duration', 'seed'].includes(key))
          .map(([key, schema]) => (
            <div key={key}>
              <GenerationControl
                field={key}
                schema={schema}
                value={display.controls?.[key] ?? schema.default}
                onChange={(v) => control(key, v)}
              />
              {!['duration', 'seed'].includes(key) && (
                <button type="button" onClick={() => inherit(key)}>
                  {t('workbench.followDefaults')}
                </button>
              )}
              {target === 'shot' && (
                <small>
                  {t('workbench.inherited', {
                    source: t(
                      'workbench.' +
                        (data?.versions?.defaults?.provenance?.['controls.' + key] === 'global'
                          ? 'account'
                          : data?.versions?.defaults?.provenance?.['controls.' + key] || 'shot'),
                    ),
                  })}
                </small>
              )}
            </div>
          ))}
        <button onClick={save}>
          {t(target === 'story' ? 'workbench.saveStory' : 'workbench.save')}
        </button>
      </fieldset>
      {target === 'account' && (
        <div>
          <label>
            {t('workbench.maxCopies')}
            <input
              type="number"
              min="1"
              step="1"
              value={max}
              disabled={state.busy || !!state.workbenchPending}
              onChange={(e) => setMax(e.target.value)}
            />
          </label>
          <button
            disabled={!data || state.busy || !Number.isInteger(Number(max)) || Number(max) < 1}
            onClick={saveCopies}
          >
            {t('workbench.saveLimit')}
          </button>
        </div>
      )}
    </details>
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
    <details>
      <summary>{t('workbench.fork')}</summary>
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
      <button
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
      </button>
    </details>
  );
}
