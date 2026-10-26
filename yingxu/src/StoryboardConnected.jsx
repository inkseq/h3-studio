import { Dialog, DialogActions } from './ui/Dialog.jsx';
import { Button } from './ui/Button.jsx';
import TakeRecipe from './QuickChatTakeRecipe.jsx';
import StoryFilmControl from './StoryFilmControl.jsx';
import { formatMoneyMicros } from './money.js';
import { createQuickChatClient } from './quick-chat-client.js';
import { useI18n } from './i18n/I18nProvider.jsx';
import React, {
  useEffect,
  useEffectEvent,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import { StoryboardWorkspace } from './StoryboardWorkspace.jsx';
import { store } from './store.js';
import { useCloud } from './CloudStudio.jsx';
import { cloudController as cloud } from './cloud-controller.js';
import { createP4OperatorClient } from './p4-operator-client.js';
import { storyboardView, expandStoryboard, storyboardDrop } from './storyboard-domain.js';
import { applyStoryboardAction } from './storyboard-actions.js';
import { uploadLibraryFiles } from './media-operations.js';
import { uploadFile } from './media.js';
import { captureWorkspace, assertWorkspace } from './workspace-context.js';
import { isActiveTrashItem } from './trash-commands.js';
import { stableJSON, shotSnapshot } from './cloud-model.js';

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
  const { t } = useI18n(),
    c = useCloud(),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  const s = useSyncExternalStore(store.subscribe, store.getState, store.getState),
    scope = useRef({ account: c.account, project: s.project.id });
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
    <Dialog
      title={t('workbench.confirm')}
      onClose={onClose}
      busy={busy}
      wide
      className="sb-confirm"
    >
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
      <DialogActions>
        <Button disabled={busy} onClick={onClose}>
          {t('workbench.cancel')}
        </Button>
        {receipt.closed_preflight && (
          <Button
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
          </Button>
        )}
        <Button
          variant="primary"
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
              setError(
                t(e.message.startsWith('workbench.') ? e.message : 'workbench.requestFailed'),
              );
            } finally {
              setBusy(false);
            }
          }}
        >
          {t('workbench.submit')}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

export default function StoryboardConnected({ onLegacyGuide, onCanvas, onNewScene }) {
  const { t: at } = useI18n();
  const s = useSyncExternalStore(store.subscribe, store.getState, store.getState),
    c = useCloud(),
    [queue, setQueue] = useState(null),
    [busy, setBusy] = useState(false),
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
  // An unconfirmed edit (no generation, no money) replays itself with its original key:
  // the server returns the first result or proves nothing was written (owner, 2026-10-25).
  // Preflight and batch submissions still wait for the person to check them.
  const pendingKind =
    c.account && s.workspace.mode === 'cloud' && !cloud.workbenchRunning()
      ? cloud.workbenchPending()?.kind
      : null;
  const recoverPending = useEffectEvent(() =>
    dispatch({ type: 'workbenchRecover' }).catch(() => {}),
  );
  useEffect(() => {
    if (!['actions', 'preferences', 'copies'].includes(pendingKind)) return;
    void recoverPending();
  }, [pendingKind, s.project.id]);
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
      // Queue hints read the whole GPU workspace: skip hidden tabs, 30 s apart.
      if (globalThis.document?.visibilityState === 'hidden') {
        if (!stopped) timer = setTimeout(read, 30000);
        return;
      }
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
      if (!stopped) timer = setTimeout(read, 30000);
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
        allChapters: true,
        capabilities: c.capabilities,
        jobs: c.jobs,
        queue,
        // Only page-switching cloud work locks the storyboard; generate and upload guard
        // their own second press (serial) instead of greying out everything (owner, 2026-10-26).
        busy: c.busy,
        error: c.error,
      }),
      lastAdded,
    }),
    [s.project, s.scopeId, c.capabilities, c.jobs, queue, c.busy, c.error, lastAdded],
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
      allChapters: true,
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
      // One action, or several applied together in one story edit (e.g. clear segments + choose).
      const actions = action.actions || [action.action];
      if (
        action.action &&
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
    if (action.type === 'workbenchUseLatest') {
      // Choose each shot's newest finished video in one story edit; shots without one are left alone.
      const context = captureWorkspace(store);
      const entities = store.getState().project?.entities || [];
      const keeps = (id) =>
        (entities.find((e) => e.id === id)?.data?.videoSegments || []).length > 0;
      const picks = await Promise.all(
        action.ids.map(async (id) => {
          // Kept parts belong to the chosen take (#512); those shots keep it.
          if (keeps(id)) return null;
          const value = await adapter.readVersions(id);
          const row = value.versions.find((v) => v.kind === 'video' && isActiveTrashItem(v));
          return row ? { op: 'storyboard.select', shot_id: id, artifact_id: row.id } : null;
        }),
      );
      assertWorkspace(store, context);
      const actions = picks.filter(Boolean);
      if (actions.length) await cloud.workbenchCommand('actions', { actions });
      return { ok: true, count: actions.length };
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
      const review = {
        ...shot.entity.data.uxReview,
        chosen: null,
        status: 'selected',
        allRejected: false,
      };
      // A clean cloud story chooses through /actions, so the server also binds that take's
      // own sound when clip sound is on (h3-studio#611). Unsaved edits keep the old path.
      const ws = store.getState().workspace;
      if (ws.mode === 'cloud' && !ws.dirty) {
        await cloud.workbenchCommand('actions', {
          actions: [
            { op: 'shot.select', shot_id: shot.id, entity_id: version.id },
            {
              op: 'entity.update',
              entity_id: shot.id,
              patch: { status: 'draft', data: { acceptPlaceholder: false, uxReview: review } },
            },
          ],
        });
        return { ok: true };
      }
      if (
        !store.updateEntity(shot.id, {
          status: 'draft',
          data: {
            selectedAssetId: version.id,
            acceptPlaceholder: false,
            uxReview: review,
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
  // A shot with no chosen take and exactly one finished take uses it without a click
  // (owner, 2026-10-25). An existing choice is never replaced by a newer take.
  const autoPicked = useRef(new Set());
  const choose = useEffectEvent((shot, version) =>
    dispatch({ type: 'choose', shot, version }).catch(() => {}),
  );
  useEffect(() => {
    if (busy || serial.current) return;
    const shot = view.shots.find((item) => {
      if (item.chosen || autoPicked.current.has(item.id)) return false;
      const done = item.versions.filter((v) => v.status === 'done');
      return (
        done.length === 1 &&
        item.versions.every((v) => v.status === 'done' || v.status === 'failed') &&
        done[0].asset?.entity?.data.fileId
      );
    });
    if (!shot) return;
    autoPicked.current.add(shot.id);
    const version = shot.versions.find((v) => v.status === 'done');
    void choose(shot.id, version.id);
  }, [view, busy]);
  const adapter = {
    previewScriptWindows: (id, options) => cloud.previewScriptWindows(id, options),
    readTakeReview: (shot, artifact) => cloud.readTakeReview(shot, artifact),
    readTakeReviewSummaries: (shot) => cloud.readTakeReviewSummaries(shot),
    saveTakeReview: (shot, artifact, body, key) => cloud.saveTakeReview(shot, artifact, body, key),
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
      workbenchPending: cloud.workbenchRunning() ? null : cloud.workbenchPending(),
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
      )}
    </>
  );
}
