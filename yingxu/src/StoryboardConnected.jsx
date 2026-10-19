import TakeRecipe from './QuickChatTakeRecipe.jsx';
import StoryFilmControl from './StoryFilmControl.jsx';
import { formatMoneyMicros } from './money.js';
import { createQuickChatClient } from './quick-chat-client.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import React, { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { StoryboardWorkspace } from './StoryboardWorkspace.jsx';
import { store } from './store.js';
import { useCloud } from './CloudStudio.jsx';
import { cloudController as cloud } from './cloud-controller.js';
import { createP4OperatorClient } from './p4-operator-client.js';
import OperatorBudgetPanel from './OperatorBudgetPanel.jsx';
import {
  storyboardView,
  expandStoryboard,
  storyboardDrop,
  storyboardGenerationProfile,
  patchStoryboardShot,
} from './storyboard-domain.js';
import { applyStoryboardAction } from './storyboard-actions.js';
import { uploadLibraryFiles } from './media-operations.js';
import { uploadFile } from './media.js';
import { captureWorkspace, assertWorkspace } from './workspace-context.js';
import { estimateLabel, stableJSON, shotSnapshot } from './cloud-model.js';

function Confirmation({ ids, onClose }) {
  const { t: at, locale } = useI18n();
  const c = useCloud(),
    ref = useRef(),
    [submitting, setSubmitting] = useState(false),
    [error, setError] = useState('');
  const p = useSyncExternalStore(store.subscribe, store.getState, store.getState).project;
  useEffect(() => {
    ref.current.showModal();
    return () => ref.current?.close();
  }, []);
  const rows = ids.map((id) => ({
      id,
      shot: p.entities.find((e) => e.id === id),
      plan: c.plans[id],
      error: c.preflightErrors[id],
    })),
    ready = rows.filter(
      (r) =>
        r.plan?.status === 'ready' && r.plan.sourceSnapshot === stableJSON(shotSnapshot(p, r.id)),
    );
  return (
    <dialog
      className="modal wide sb-confirm"
      ref={ref}
      aria-label={at('adjunct.storyboardConnected.copy315')}
      onCancel={(event) => {
        event.preventDefault();
        if (!submitting) onClose();
      }}
    >
      <div className="modal-heading">
        <h2>{at('adjunct.storyboardConnected.copy316')}</h2>
        <button disabled={submitting} onClick={onClose}>
          {at('adjunct.storyboardConnected.copy317')}
        </button>
      </div>
      <p>{at('adjunct.storyboardConnected.copy318')}</p>
      {rows.map(({ id, shot, plan, error }) => (
        <section key={id} className="sb-confirm-row">
          <h3>
            {shot?.title || at('adjunct.storyboardConnected.copy319')} ·{' '}
            {plan?.status === 'ready'
              ? at('adjunct.storyboardConnected.copy320')
              : at('adjunct.storyboardConnected.copy321')}
          </h3>
          {error && <p role="alert">{error}</p>}
          {plan && (
            <>
              <p>
                {plan.effective_request.model} ·{' '}
                {plan.effective_request.mode === 'ref'
                  ? at('adjunct.storyboardConnected.copy322')
                  : at('adjunct.storyboardConnected.copy323')}{' '}
                · {plan.effective_request.duration}
                {at('adjunct.storyboardConnected.copy324')}
                {plan.effective_request.resolution} · {plan.effective_request.steps}
                {at('adjunct.storyboardConnected.copy325')}
              </p>
              <p>{estimateLabel(plan.estimate, locale)}</p>
              <details open>
                <summary>{at('adjunct.storyboardConnected.copy326')}</summary>
                <pre>{plan.effective_request.prompt}</pre>
                {plan.storyboardPreview?.references?.map((item, index) => (
                  <p key={index} className={item.sent ? '' : 'muted'}>
                    {item.sent
                      ? at('adjunct.storyboardConnected.copy327')
                      : at('adjunct.storyboardConnected.copy328')}
                    ：{p.entities.find((e) => e.id === item.entity_id)?.title || item.why} ·{' '}
                    {item.why}
                    {item.reason ? ' · ' + item.reason : ''}
                  </p>
                ))}
                {plan.storyboardPreview?.notes?.map((note) => (
                  <p key={note}>{note}</p>
                ))}
              </details>
              {plan.blockers?.map((block, index) => (
                <p role="alert" key={index}>
                  {typeof block === 'string'
                    ? block
                    : block.message || block.reason || block.code || JSON.stringify(block)}
                </p>
              ))}
              {plan.sourceSnapshot !== stableJSON(shotSnapshot(p, id)) && (
                <p role="alert">{at('adjunct.storyboardConnected.copy329')}</p>
              )}
            </>
          )}
        </section>
      ))}
      {error && <p role="alert">{error}</p>}
      <OperatorBudgetPanel account={c.account} compact />
      <div className="modal-actions">
        <button disabled={submitting} onClick={onClose}>
          {at('adjunct.storyboardConnected.copy330')}
        </button>
        <button
          className="primary"
          disabled={submitting || !ready.length || c.busy}
          onClick={async () => {
            setSubmitting(true);
            setError('');
            try {
              if (ready.length === 1) await cloud.submit(ready[0].id);
              else await cloud.submitBatch(ready.map((r) => r.id));
              onClose();
            } catch (e) {
              setError(e.message);
            } finally {
              setSubmitting(false);
            }
          }}
        >
          {submitting
            ? at('adjunct.storyboardConnected.copy331')
            : at('adjunct.storyboardConnected.copy332', { v0: ready.length })}
        </button>
      </div>
    </dialog>
  );
}

function planRecipe(plan, capabilities) {
  const request = plan?.effective_request;
  if (!request) return null;
  const recipe =
    capabilities?.recipes?.find((row) => row.id === plan.recipe_id) ||
    capabilities?.recipes?.find((row) => row.mode === request.mode);
  // Saved effective values remain visible even when today's schema differs.
  const controls = Object.fromEntries(
    Object.entries(request).filter(([key]) => !['model', 'mode', 'prompt', 'inputs'].includes(key)),
  );
  if (plan.authoring_recipe) return structuredClone(plan.authoring_recipe);
  const inputs = Object.fromEntries(
    Object.entries(request.inputs || {}).map(([role, value]) => [
      role,
      Array.isArray(value)
        ? value.map((item) =>
            typeof item === 'string'
              ? {
                  asset_id: item,
                  ...(role === 'videos'
                    ? { include_audio: request.video_audio?.[item] === true }
                    : {}),
                }
              : item,
          )
        : typeof value === 'string'
          ? { asset_id: value }
          : value,
    ]),
  );
  if (request.guides) inputs.guides = request.guides;
  return {
    recipe_id: recipe?.id,
    deployment_profile_id: plan.execution?.deployment_profile_id || plan.deployment_profile_id,
    model_id: request.model,
    prompt: request.prompt,
    controls,
    inputs,
  };
}
function WorkbenchConfirmation({ receipt, onClose }) {
  const { t, locale } = useI18n(),
    c = useCloud(),
    ref = useRef(),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  const s = useSyncExternalStore(store.subscribe, store.getState, store.getState),
    scope = useRef({ account: c.account, project: s.project.id });
  useEffect(() => {
    ref.current.showModal();
    return () => ref.current?.close();
  }, []);
  const same =
    scope.current.account === c.account &&
    scope.current.project === s.project.id &&
    receipt.project_version === s.workspace.serverVersion &&
    !s.workspace.dirty;
  const money =
    Number.isSafeInteger(receipt.estimate?.cost_microusd) &&
    receipt.estimate.cost_microusd >= 0 &&
    receipt.estimate.cost_microusd <= 9000000000000000
      ? formatMoneyMicros(receipt.estimate.cost_microusd)
      : t('workbench.unknown');
  return (
    <dialog
      className="modal wide sb-confirm"
      ref={ref}
      aria-label={t('workbench.confirm')}
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) onClose();
      }}
    >
      <h2>{t('workbench.confirm')}</h2>
      <p>{t('workbench.estimate', { amount: money })}</p>
      <p>
        {Number.isFinite(receipt.estimate?.machine_seconds)
          ? t('workbench.machine', { seconds: receipt.estimate.machine_seconds })
          : t('workbench.machineUnknown')}
      </p>
      <p>{t('workbench.notEta')}</p>
      {(receipt.items || []).map((item, i) => (
        <section key={item.plan_id || i}>
          <h3>
            {s.project.entities.find((e) => e.id === item.shot_id)?.title} ·{' '}
            {t('workbench.take', { number: item.index + 1 })}
          </h3>
          <TakeRecipe
            take={{ seed: item.seed, recipe: planRecipe(item.plan, c.capabilities) }}
            state={{
              capabilities: c.capabilities,
              materials: s.project.entities
                .filter((e) => e.data.cloudAssetId)
                .map((e) => ({ asset_id: e.data.cloudAssetId, asset: { file_name: e.title } })),
            }}
          />
        </section>
      ))}
      {!same && <p role="alert">{t('workbench.changed')}</p>}
      {error && <p role="alert">{error}</p>}
      <button disabled={busy} onClick={onClose}>
        {t('workbench.cancel')}
      </button>
      {receipt.closed_preflight && (
        <button
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await cloud.workbenchAbandonClosed(receipt.id);
              onClose();
            } catch {
              setError(t('workbench.requestFailed'));
            } finally {
              setBusy(false);
            }
          }}
        >
          {t('workbench.acknowledge')}
        </button>
      )}
      <button
        className="primary"
        disabled={busy || !same || !receipt.ready}
        onClick={async () => {
          setBusy(true);
          try {
            await cloud.workbenchCommand('batch', {
              projectVersion: receipt.project_version,
              plan_ids: receipt.items.map((i) => i.plan_id),
            });
            onClose();
          } catch (e) {
            setError(t(e.message.startsWith('workbench.') ? e.message : 'workbench.requestFailed'));
          } finally {
            setBusy(false);
          }
        }}
      >
        {t('workbench.submit')}
      </button>
    </dialog>
  );
}

export default function StoryboardConnected({ onLegacyGuide, onCanvas, onNewScene }) {
  const { t: at, locale } = useI18n();
  const s = useSyncExternalStore(store.subscribe, store.getState, store.getState),
    c = useCloud(),
    [queue, setQueue] = useState(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [confirmation, setConfirmation] = useState(null),
    [lastAdded, setLastAdded] = useState(null),
    serial = useRef(false);
  const [workbenchReceipt, setWorkbenchReceipt] = useState(null),
    [workbenchPreferences, setWorkbenchPreferences] = useState(null);
  useEffect(() => {
    let live = true;
    setWorkbenchPreferences(null);
    setWorkbenchReceipt(null);
    if (c.account && s.workspace.mode === 'cloud')
      cloud
        .workbenchPreferences()
        .then((value) => {
          if (live) setWorkbenchPreferences(value);
        })
        .catch(() => {});
    return () => {
      live = false;
    };
  }, [c.account, s.project.id, s.workspace.mode]);
  useEffect(() => {
    if (!c.account || !c.operatorAccess) {
      setQueue(null);
      return;
    }
    const api = createP4OperatorClient();
    api.setAccount(c.account);
    let stopped = false,
      timer;
    const read = async () => {
      try {
        const snapshot = await api.state();
        if (stopped) return;
        const rows = {};
        for (const inputMode of ['fl', 'ref']) {
          const q = snapshot.queue.find((q) => q.mode === inputMode) || {};
          rows[inputMode.toUpperCase()] = {
            waiting: q.waiting_jobs ?? q.waiting ?? q.queued ?? 0,
            ready: q.ready_machines ?? q.ready ?? 0,
            roughWaitSeconds: q.rough_wait_seconds,
          };
        }
        setQueue(rows);
      } catch {
        if (!stopped) setQueue(null);
      }
      if (!stopped) timer = setTimeout(read, 10000);
    };
    read();
    return () => {
      stopped = true;
      clearTimeout(timer);
      api.reset();
    };
  }, [c.account, c.operatorAccess]);
  const [creators, setCreators] = useState({});
  useEffect(() => {
    setCreators({});
    if (!c.account || s.workspace.mode !== 'cloud' || s.workspace.account !== c.account) return;
    let active = true;
    const api = createQuickChatClient();
    api.setAccount(c.account);
    api
      .project(s.project.id)
      .then((value) => {
        if (active && value.project?.id === s.project.id) setCreators(value.created_by || {});
      })
      .catch(() => {});
    return () => {
      active = false;
      api.reset();
    };
  }, [c.account, s.project.id, s.workspace.mode, s.workspace.account, s.workspace.serverVersion]);
  const baseView = useMemo(
    () => ({
      ...storyboardView(s.project, {
        chapterId: s.scopeId,
        capabilities: c.capabilities,
        jobs: c.jobs,
        queue,
        busy: busy || c.busy,
        error: error || c.error,
      }),
      lastAdded,
    }),
    [s.project, s.scopeId, c.capabilities, c.jobs, queue, busy, c.busy, error, c.error, lastAdded],
  );
  const view = useMemo(
    () => ({
      ...baseView,
      shots: baseView.shots.map((shot) => ({ ...shot, created_by: creators[shot.id] || null })),
    }),
    [baseView, creators],
  );
  const currentView = () =>
    storyboardView(store.getState().project, {
      chapterId: store.getState().scopeId,
      capabilities: cloud.getState().capabilities,
      jobs: cloud.getState().jobs,
    });
  const mutate = (action) => {
    let receipt;
    const live = currentView();
    const ok = store.editProject((p) => {
      receipt = applyStoryboardAction(p, action, { chapterId: live.project.chapterId, view: live });
      return receipt !== false;
    });
    if (!ok) throw Error(store.getState().notice || at('adjunct.storyboardConnected.copy333'));
    if (action.type === 'add') setLastAdded(receipt?.id);
    return receipt || { ok: true };
  };
  const materialize = (id, quality, source) => {
    const live = currentView(),
      shot = live.shots.find((s) => s.id === id);
    if (!shot) throw Error(at('adjunct.storyboardConnected.copy334'));
    const inputMode = source?.effectiveRequest?.mode || shot.mode.toLowerCase(),
      profile = storyboardGenerationProfile(cloud.getState().capabilities, shot, {
        quality,
        sourceRequest: source?.effectiveRequest,
      });
    if (!profile || profile.generation_support?.[inputMode]?.enabled !== true)
      throw Error(at('adjunct.storyboardConnected.copy335'));
    const ok = store.editProject((p) => {
      const entity = p.entities.find((e) => e.id === id),
        before = stableJSON({ storyboard: entity.data.storyboard, h3: entity.data.h3 });
      if (!entity.data.storyboard)
        patchStoryboardShot(p, id, {
          mode: shot.mode,
          cast: shot.cast,
          place: shot.place,
          props: shot.props,
          start: shot.start,
          end: shot.end,
          refs: shot.refs,
          continuePrev: shot.continuePrev,
        });
      if (quality) entity.data.storyboard.quality = quality;
      entity.data.storyboard.mode = inputMode;
      entity.data.h3 = {
        ...entity.data.h3,
        inputMode,
        recipeId: inputMode === 'ref' ? 'h3-base-ref2va-v1' : 'h3-base-fl2va-v1',
        deploymentProfileId: profile.id,
        controls: { ...entity.data.h3?.controls, duration: entity.data.seconds },
      };
      if (before !== stableJSON({ storyboard: entity.data.storyboard, h3: entity.data.h3 })) {
        entity.version++;
        if (entity.data.selectedAssetId) entity.status = 'review';
      }
    });
    if (!ok) throw Error(store.getState().notice);
  };
  const dispatch = async (action) => {
    if (action.type === 'workbenchShotSettings') {
      const context = captureWorkspace(store),
        live = store.getState();
      if (live.workspace.serverVersion !== action.expectedVersion || live.workspace.dirty)
        throw Error('workbench.changed');
      const shot = live.project.entities.find((row) => row.id === action.shotId);
      if (!shot || shot.type !== 'shot') throw Error('workbench.changed');
      const h3 = { ...shot.data.h3, controls: structuredClone(action.value.controls || {}) };
      delete h3.recipeId;
      delete h3.deploymentProfileId;
      delete h3.inputMode;
      if (action.value.recipe_id) {
        h3.recipeId = action.value.recipe_id;
        h3.inputMode = c.capabilities?.recipes?.find((row) => row.id === h3.recipeId)?.mode;
      }
      if (action.value.deployment_profile_id)
        h3.deploymentProfileId = action.value.deployment_profile_id;
      const data = {
        h3,
        ...(shot.data.storyboard && h3.inputMode
          ? { storyboard: { ...shot.data.storyboard, mode: h3.inputMode } }
          : {}),
        ...(Number.isFinite(h3.controls.duration) ? { seconds: h3.controls.duration } : {}),
      };
      if (!store.updateEntity(shot.id, { data })) throw Error('workbench.changed');
      await cloud.save();
      assertWorkspace(store, context);
      return { ok: true };
    }
    if (action.type === 'workbenchRefresh') {
      const context = captureWorkspace(store),
        value = await cloud.workbenchPreferences();
      assertWorkspace(store, context);
      setWorkbenchPreferences(value);
      return { ok: true };
    }
    if (action.type === 'workbenchCopies') {
      const context = captureWorkspace(store);
      const receipt = await cloud.workbenchCommand('copies', action.body);
      assertWorkspace(store, context);
      const value = await cloud.workbenchPreferences();
      assertWorkspace(store, context);
      setWorkbenchPreferences(value);
      return { ok: true, receipt };
    }
    if (action.type === 'workbenchPreferences') {
      const context = captureWorkspace(store);
      const value = await cloud.workbenchCommand('preferences', action.body);
      assertWorkspace(store, context);
      setWorkbenchPreferences(value);
      return { ok: true, receipt: value };
    }
    if (action.type === 'workbenchAction') {
      const actions = [action.action];
      if (
        action.profileId &&
        action.action.op === 'storyboard.bulk' &&
        ['draft', 'final'].includes(action.action.operation)
      ) {
        for (const id of action.action.shot_ids)
          actions.push({
            op: 'shot.configure_generation',
            shot_id: id,
            deployment_profile_id: action.profileId,
          });
      }
      await cloud.workbenchCommand('actions', {
        actions,
        ...(action.expectedVersion === undefined
          ? {}
          : { expected_version: action.expectedVersion }),
      });
      return { ok: true };
    }
    if (action.type === 'workbenchGenerate') {
      const context = captureWorkspace(store);
      const result = await cloud.workbenchCommand('preflight', {
        shot_ids: [...new Set(action.ids)],
        copies: action.copies,
        capabilities_version: c.capabilities?.capabilities_version,
      });
      assertWorkspace(store, context);
      setWorkbenchReceipt(result);
      return { ok: true };
    }
    if (action.type === 'workbenchRecover') {
      const context = captureWorkspace(store),
        pending = cloud.workbenchPending();
      const result = await cloud.workbenchCommand(null, null, { recover: true });
      assertWorkspace(store, context);
      if (pending?.kind === 'preflight') setWorkbenchReceipt(result);
      else if (['preferences', 'copies'].includes(pending?.kind)) {
        const value = await cloud.workbenchPreferences();
        assertWorkspace(store, context);
        setWorkbenchPreferences(value);
      }
      return { ok: true };
    }

    if (action.type === 'generate') {
      if (serial.current) throw Error(at('adjunct.storyboardConnected.copy336'));
      if (s.workspace.mode !== 'cloud') throw Error(at('adjunct.storyboardConnected.copy337'));
      serial.current = true;
      setBusy(true);
      setError('');
      const context = captureWorkspace(store),
        ids = [...new Set(action.ids)],
        errors = {},
        prepared = [];
      try {
        for (const id of ids) {
          assertWorkspace(store, context);
          try {
            const shot = currentView().shots.find((s) => s.id === id);
            let source =
              action.sourceVersion && shot?.versions.find((v) => v.id === action.sourceVersion);
            if (
              action.sourceVersion &&
              (!source?.jobId || shot.chosen !== source.id || source.status !== 'done')
            )
              throw Error(at('adjunct.storyboardConnected.copy338'));
            if (source && !source.effectiveRequest) {
              const receipt = await cloud.jobReceipt(source.jobId);
              assertWorkspace(store, context);
              if (
                receipt.status !== 'succeeded' ||
                receipt.client_ref?.shot_id !== id ||
                !receipt.effective_request
              )
                throw Error(at('adjunct.storyboardConnected.copy339'));
              source = { ...source, effectiveRequest: receipt.effective_request };
            }
            materialize(id, action.quality, source);
            await cloud.prepareStoryboard(id, { sourceJobId: source?.jobId });
            assertWorkspace(store, context);
            prepared.push(id);
          } catch (e) {
            assertWorkspace(store, context);
            errors[id] = e.message;
          }
        }
        assertWorkspace(store, context);
        if (Object.keys(errors).length) {
          setError(
            Object.entries(errors)
              .map(
                ([id, msg]) =>
                  (store.getState().project.entities.find((e) => e.id === id)?.title || id) +
                  '：' +
                  msg,
              )
              .join('；'),
          );
        }
        if (prepared.length) setConfirmation(prepared);
        return { ok: prepared.length > 0 };
      } finally {
        serial.current = false;
        setBusy(false);
      }
    }
    if (action.type === 'update' && Object.hasOwn(action.patch, 'quality'))
      return dispatch({
        type: 'workbenchAction',
        action: { op: 'storyboard.bulk', shot_ids: [action.id], operation: action.patch.quality },
      });
    if (action.type === 'cancel') {
      await cloud.cancel(action.jobId);
      return { ok: true };
    }
    if (action.type === 'choose') {
      const shot = currentView().shots.find((s) => s.id === action.shot),
        version = shot?.versions.find((v) => v.id === action.version);
      if (version?.status !== 'done' || !version.asset?.entity?.data.fileId)
        throw Error(at('adjunct.storyboardConnected.copy340'));
      if (
        !store.updateEntity(shot.id, {
          status: 'draft',
          data: {
            selectedAssetId: version.id,
            acceptPlaceholder: false,
            uxReview: {
              ...shot.entity.data.uxReview,
              chosen: null,
              status: 'selected',
              allRejected: false,
            },
          },
        })
      )
        throw Error(store.getState().notice);
      if (store.getState().workspace.mode === 'cloud') await cloud.save();
      return { ok: true };
    }
    if (action.type === 'saveFrame') {
      const context = captureWorkspace(store),
        id = await cloud.saveFrame(
          action.artifactId,
          action.selection === 'last'
            ? { position: 'last' }
            : { position: 'time', time_seconds: action.t },
        );
      assertWorkspace(store, context);
      if (action.pid) mutate({ type: 'fileImage', asset: id, pid: action.pid, vid: action.vid });
      if (store.getState().workspace.mode === 'cloud') await cloud.save();
      return { ok: true, asset: id };
    }
    if (action.type === 'uploadFiles') {
      if (serial.current) throw Error(at('adjunct.storyboardConnected.copy341'));
      serial.current = true;
      setBusy(true);
      const context = captureWorkspace(store);
      try {
        const ids = await uploadLibraryFiles({
          store,
          files: Array.from(action.files),
          upload: uploadFile,
        });
        assertWorkspace(store, context);
        for (const id of ids) {
          if (action.target?.kind === 'image')
            mutate({
              type: 'fileImage',
              asset: id,
              pid: action.target.pid,
              vid: action.target.vid,
            });
          else if (action.target?.kind === 'voice')
            mutate({ type: 'addVoice', asset: id, pid: action.target.pid });
          else if (action.target?.kind === 'ambient')
            mutate({ type: 'profile', id: action.target.pid, patch: { ambient: id } });
          else if (action.target?.kind === 'shot')
            mutate({
              type: 'drop',
              asset: id,
              shot: action.target.shot,
              slot: action.target.slot || 'smart',
            });
        }
        return { ok: true, ids };
      } finally {
        serial.current = false;
        setBusy(false);
      }
    }
    return mutate(action);
  };
  const adapter = {
    readCopies: () => cloud.workbenchCopies(),
    readPreferences: () => cloud.workbenchPreferences(),
    readVersions: async (id, { offset = 0 } = {}) => {
      const value = await cloud.workbenchRead(
        `shots/${encodeURIComponent(id)}/versions?limit=20&offset=${offset}`,
      );
      if (
        value.project_id !== s.project.id ||
        value.shot_id !== id ||
        !Array.isArray(value.versions)
      )
        throw Error('workbench.receipt');
      return value;
    },
    state: {
      ...view,
      workspaceMode: s.workspace.mode,
      contextId: c.account + ':' + s.project.id + ':' + s.workspaceEpoch,
      projectVersion: s.workspace.serverVersion,
      copiesMax: workbenchPreferences?.copies_max ?? null,
      scenes: s.project.entities.filter((e) => e.type === 'scene'),
      profileUsage: Object.fromEntries(
        view.profiles.map((profile) => [
          profile.id,
          new Set(
            s.project.entities
              .filter((e) => e.type === 'chapter')
              .flatMap(
                (chapter) =>
                  storyboardView(s.project, {
                    chapterId: chapter.id,
                    capabilities: c.capabilities,
                    jobs: c.jobs,
                  }).shots,
              )
              .filter(
                (shot) =>
                  shot.cast?.some((item) => item.id === profile.id) ||
                  shot.place?.id === profile.id ||
                  shot.props?.includes(profile.id),
              )
              .map((shot) => shot.id),
          ).size,
        ]),
      ),
      workbenchPending: cloud.workbenchPending(),
    },
    db: {
      state: view,
      asset: (id) => view.assets.find((a) => a.id === id),
      profile: (id) => view.profiles.find((p) => p.id === id),
    },
    dispatch,
    planDrop: (shot, item, slot) => storyboardDrop(shot, item, slot, view),
    expand: (shot, locale = 'zh') => {
      const expanded = expandStoryboard(shot, view, { locale }),
        plan = c.plans[shot.id];
      if (
        plan?.storyboardPreview &&
        plan.sourceSnapshot === stableJSON(shotSnapshot(s.project, shot.id))
      ) {
        const server = plan.storyboardPreview;
        expanded.parts =
          server.prompt_parts?.map((part) => ({ label: part.label, text: part.text })) ||
          expanded.parts;
        expanded.items =
          server.references?.map((ref) => ({
            a: view.assets.find((a) => a.id === ref.entity_id) || { name: ref.entity_id },
            kind:
              ref.slot === 'guides'
                ? at('adjunct.storyboardConnected.copy342')
                : ref.slot === 'images'
                  ? at('adjunct.storyboardConnected.copy343')
                  : ref.slot === 'videos'
                    ? at('adjunct.storyboardConnected.copy344')
                    : ref.slot === 'audios'
                      ? at('adjunct.storyboardConnected.copy345')
                      : ref.slot === 'first_frame'
                        ? at('adjunct.storyboardConnected.copy346')
                        : at('adjunct.storyboardConnected.copy347'),
            why: ref.why,
            sent: ref.sent,
            reason: ref.reason,
          })) || expanded.items;
        expanded.notes = server.notes || expanded.notes;
      }
      return expanded;
    },
  };
  return (
    <>
      <StoryboardWorkspace
        filmControl={
          <StoryFilmControl
            cloudState={c}
            workspaceState={s}
            onSave={cloud.save}
            otherBusy={busy || !!adapter.state.workbenchPending}
          />
        }
        onNewScene={onNewScene}
        adapter={adapter}
        onLegacyGuide={onLegacyGuide}
        onCanvas={onCanvas}
      />
      {workbenchReceipt && (
        <WorkbenchConfirmation
          receipt={workbenchReceipt}
          onClose={() => setWorkbenchReceipt(null)}
        />
      )}{' '}
      {confirmation && <Confirmation ids={confirmation} onClose={() => setConfirmation(null)} />}
    </>
  );
}
