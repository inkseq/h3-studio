import { Clapperboard } from 'lucide-react';
import { Dialog, DialogActions } from './ui/Dialog.jsx';
import { Button } from './ui/Button.jsx';
import TakeRecipe from './QuickChatTakeRecipe.jsx';
import StoryFilmControl from './StoryFilmControl.jsx';
import { formatMoneyMicros } from './money.js';
import { CreditUsageBar, creditShortfall, useCredits } from './CreditsUI.jsx';
import { storyboardPresentation } from './i18n/legacyPresentation.js';
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
import { useCloud } from './use-cloud.js';
import { cloudController as cloud } from './cloud-controller.js';
import {
  storyboardView,
  expandStoryboard,
  storyboardDrop,
  shotsTooShort,
  SHORTEST_SHOT_SECONDS,
} from './storyboard-domain.js';
import { applyStoryboardAction } from './storyboard-actions.js';
import { uploadAttachRefusal, uploadLibraryFiles } from './media-operations.js';
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
// What the user pays: credits when the server quotes credits, dollars only on the USD path
// (staging and operators). Machine-time limits are an internal guard, not shown here.
function receiptCost(receipt) {
  const plans = (receipt.items || []).map((item) => item.plan || {});
  if (plans.length && plans.every((plan) => plan.admission === 'credits'))
    return plans.every((plan) => Number.isSafeInteger(plan.credits))
      ? { credits: plans.reduce((sum, plan) => sum + plan.credits, 0) }
      : {};
  const micros = receipt.estimate?.cost_microusd;
  return Number.isSafeInteger(micros) && micros >= 0 && micros <= 9000000000000000
    ? { money: formatMoneyMicros(micros) }
    : {};
}
// Name the shot an estimate refused and say why, instead of a generic "fix something".
function shotRefusal(problem, live, t, locale) {
  if (problem?.message !== 'workbench.inputsInvalid') return problem;
  const index = live.shots.findIndex((shot) => shot.id === problem.shotId);
  // Never show the raw key: without the shot, say the general thing.
  if (index < 0) return Error(t('workbench.inputsInvalid'));
  const shot = live.shots[index],
    why = storyboardPresentation(expandStoryboard(shot, live, { locale }), locale).warnings[0];
  return Error(
    t('workbench.shotInvalid', {
      shot: shot.title || t('workbench.shotN', { number: index + 1 }),
      why: why || t('workbench.inputsInvalid'),
    }),
  );
}
function WorkbenchConfirmation({ receipt, shotLabels, onClose }) {
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
  const cost = receiptCost(receipt),
    balance = useCredits(c.account)?.available,
    takes = (receipt.items || []).some((item) => item.index > 0),
    short = creditShortfall(cost.credits, balance),
    closed = !!receipt.closed_preflight,
    checking = !!receipt.checking;
  // A refused check on shots shorter than the video model makes: say so and offer the fix.
  const tooShort =
    closed && receipt.blocked === 'invalid_generation_input'
      ? shotsTooShort(
          (receipt.items || []).map((item) => item.shot_id),
          s.project.entities,
        )
      : [];
  const lengthen = async () => {
    setBusy(true);
    try {
      for (const shot of tooShort)
        if (!store.updateEntity(shot.id, { data: { seconds: SHORTEST_SHOT_SECONDS } }))
          throw Error('workbench.changed');
      if (store.getState().workspace.mode === 'cloud') await cloud.save();
      await cloud.workbenchAbandonClosed(receipt.id);
      onClose();
    } catch {
      setError(t('workbench.requestFailed'));
    } finally {
      setBusy(false);
    }
  };
  // A closed (refused) preflight never generates; leaving the dialog clears it so the
  // next try is not blocked by "the previous operation is not confirmed".
  const dismiss = async () => {
    if (!closed) return onClose();
    setBusy(true);
    try {
      await cloud.workbenchAbandonClosed(receipt.id);
      onClose();
    } catch {
      setError(t('workbench.requestFailed'));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t('workbench.confirm')}
      onClose={() => void dismiss()}
      busy={busy}
      wide
      className="sb-confirm"
    >
      {checking && !Number.isFinite(balance) ? (
        <i className="ui-wait sb-confirm-usage sb-confirm-wait" />
      ) : (
        <CreditUsageBar credits={cost.credits} balance={balance} />
      )}
      <p className="sb-confirm-note">{t('workbench.timing')}</p>
      {(receipt.items || []).map((item, i) => (
        <section key={item.plan_id || i}>
          <h3>
            {shotLabels?.[item.shot_id] ||
              s.project.entities.find((e) => e.id === item.shot_id)?.title}
            {Number.isFinite(item.plan?.output_spec?.actual_duration) &&
              ' · ' +
                t('workbench.seconds', {
                  seconds: Math.round(item.plan.output_spec.actual_duration),
                })}
            {takes && ' · ' + t('workbench.take', { number: item.index + 1 })}
          </h3>
          {checking ? (
            <i className="ui-wait sb-confirm-wait-recipe" />
          ) : (
            <TakeRecipe
              take={{ seed: item.seed, recipe: planRecipe(item.plan, c.capabilities) }}
              state={{
                capabilities: c.capabilities,
                materials: s.project.entities
                  .filter((e) => e.data.cloudAssetId)
                  .map((e) => ({ asset_id: e.data.cloudAssetId, asset: { file_name: e.title } })),
              }}
            />
          )}
        </section>
      ))}
      {!checking && !same && <p role="alert">{t('workbench.changed')}</p>}
      {/* The server refuses when no machine can take the work (none running and auto-start
          off or out of budget). Say so plainly instead of a silently greyed-out button. */}
      {!checking && same && !receipt.ready && (
        <p role="alert">
          {t(
            closed
              ? receipt.blocked === 'source_changed'
                ? 'workbench.changed'
                : tooShort.length
                  ? 'workbench.tooShort'
                  : 'workbench.blockedInput'
              : 'workbench.notReady',
            { count: tooShort.length, seconds: SHORTEST_SHOT_SECONDS },
          )}
        </p>
      )}
      {error && <p role="alert">{error}</p>}
      <DialogActions>
        <Button disabled={busy} onClick={() => void dismiss()}>
          {t(closed ? 'workbench.acknowledge' : 'workbench.cancel')}
        </Button>
        {tooShort.length > 0 && (
          <Button variant="primary" disabled={busy} onClick={() => void lengthen()}>
            {t('workbench.lengthen', { seconds: SHORTEST_SHOT_SECONDS })}
          </Button>
        )}
        {!closed && (
          <Button
            variant="primary"
            className="sb-confirm-start"
            disabled={checking || busy || !same || !receipt.ready || short}
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
            {t(checking ? 'workbench.checking' : 'workbench.submit')}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  );
}

function PreviewFilmButton({ primary }) {
  const { t } = useI18n();
  return (
    <span className="story-film-control">
      <Button variant={primary ? 'primary' : undefined}>
        <Clapperboard size={14} />
        {t('film.open')}
      </Button>
    </span>
  );
}

export default function StoryboardConnected({
  onLegacyGuide,
  onCanvas,
  onNewScene,
  preview = null,
}) {
  const { t: at, locale } = useI18n();
  const live = useSyncExternalStore(store.subscribe, store.getState, store.getState),
    c = useCloud(),
    s = preview
      ? {
          ...live,
          project: preview,
          selectedId: null,
          scopeId: null,
          workspace: { ...live.workspace, mode: 'cloud', account: c.account },
        }
      : live,
    [queue, setQueue] = useState(null),
    [busy, setBusy] = useState(false),
    [lastAdded, setLastAdded] = useState(null),
    serial = useRef(false);
  const checkRun = useRef(null);
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
    let api,
      stopped = false,
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
    // Operator code stays out of what ordinary accounts download (#1020).
    void import('./p4-operator-client.js')
      .then(({ createP4OperatorClient }) => {
        if (stopped) return;
        api = createP4OperatorClient();
        api.setAccount(c.account);
        void read();
      })
      .catch(() => {});
    return () => {
      stopped = true;
      clearTimeout(timer);
      api?.reset();
    };
  }, [c.account, c.operatorAccess]);
  const [creators, setCreators] = useState({});
  useEffect(() => {
    setCreators({});
    if (preview || !c.account || s.workspace.mode !== 'cloud' || s.workspace.account !== c.account)
      return;
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
  }, [
    preview,
    c.account,
    s.project.id,
    s.workspace.mode,
    s.workspace.account,
    s.workspace.serverVersion,
  ]);
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
        busy: c.busy && !preview,
        error: c.error,
      }),
      lastAdded,
    }),
    [s.project, s.scopeId, c.capabilities, c.jobs, queue, c.busy, c.error, lastAdded, preview],
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
    // A story that is still opening is only shown: nothing may change the story behind it.
    if (preview) return { ok: false };
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
      // An edit left unconfirmed by a failed answer replays with its own key first, so one
      // bad response does not block every later choice until the page reloads (#1032).
      if (['actions', 'preferences', 'copies'].includes(cloud.workbenchPending()?.kind))
        await dispatch({ type: 'workbenchRecover' });
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
      // The dialog opens on the press and "Start" waits for the check (#1030). A second
      // press while it checks does nothing; after Cancel it waits for the dropped check.
      const earlier = checkRun.current;
      if (earlier && !earlier.dropped) return { ok: true };
      const context = captureWorkspace(store),
        shotIds = [...new Set(action.ids)],
        run = { dropped: false };
      run.done = new Promise((resolve) => (run.settle = resolve));
      checkRun.current = run;
      setWorkbenchReceipt({
        checking: true,
        items: shotIds.flatMap((shot_id) =>
          Array.from({ length: action.copies }, (_, index) => ({ shot_id, index })),
        ),
      });
      try {
        await earlier?.done;
        if (run.dropped) return { ok: true };
        const result = await cloud.workbenchCommand('preflight', {
          shot_ids: shotIds,
          copies: action.copies,
          capabilities_version: c.capabilities?.capabilities_version,
        });
        assertWorkspace(store, context);
        if (!run.dropped) setWorkbenchReceipt(result);
        // Cancel came first: a closed check must still be cleared or the next try is blocked.
        else if (result.closed_preflight) await cloud.workbenchAbandonClosed(result.id);
        return { ok: true };
      } catch (problem) {
        if (run.dropped) return { ok: false };
        setWorkbenchReceipt(null);
        throw shotRefusal(problem, currentView(), at, locale);
      } finally {
        run.settle();
        if (checkRun.current === run) checkRun.current = null;
      }
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
        const reused = new Set();
        const ids = await uploadLibraryFiles({
          store,
          files: Array.from(action.files),
          upload: uploadFile,
          reused,
        });
        assertWorkspace(store, context);
        // A file the story already had is attached like a new one; a refused attach says why
        // (the file itself stays in the story's files either way).
        const refused = [];
        const name = () =>
          store.getState().project.entities.find((e) => e.id === action.target?.pid)?.title || '';
        for (const id of ids) {
          const refusal = uploadAttachRefusal(store.getState().project, action.target, id);
          if (refusal) {
            refused.push(at(refusal, { name: name() }));
            continue;
          }
          try {
            attachUpload(action.target, id);
          } catch {
            refused.push(at('upload.attachFailed', { name: name() }));
          }
        }
        if (refused.length) throw Error(refused[0]);
        return { ok: true, ids, reused: [...reused] };
      } finally {
        serial.current = false;
        setBusy(false);
      }
    }
    return mutate(action);
  };
  const attachUpload = (target, id) => {
    if (target?.kind === 'image')
      mutate({ type: 'fileImage', asset: id, pid: target.pid, vid: target.vid });
    else if (target?.kind === 'voice') mutate({ type: 'addVoice', asset: id, pid: target.pid });
    else if (target?.kind === 'ambient')
      mutate({ type: 'profile', id: target.pid, patch: { ambient: id } });
    else if (target?.kind === 'shot')
      mutate({
        type: 'drop',
        asset: id,
        shot: target.shot,
        slot: target.slot || 'smart',
      });
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
    readInkySuggestions: cloud.readInkySuggestions && (() => cloud.readInkySuggestions()),
    refreshInky: cloud.refreshInky,
    readPictureSettings: cloud.readPictureSettings,
    drawShotPicture: cloud.drawShotPicture,
    readVoices: cloud.readVoices,
    speakLine: cloud.speakLine,
    designVoice: cloud.designVoice,
    keepVoiceDesign: cloud.keepVoiceDesign,
    readShotReadiness: cloud.readShotReadiness && (() => cloud.readShotReadiness()),
    ignoreInkySuggestion: (id) => cloud.ignoreInkySuggestion?.(id),
    rewriteInkySuggestion: (id) => cloud.rewriteInkySuggestion(id),
    sendInkyMessage: (text, shotId) => cloud.sendInkyMessage(text, shotId),
    undoInkyMessage: (turnId) => cloud.undoInkyMessage(turnId),
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
      opening: !!preview,
      placeholder: !!preview?.placeholder,
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
          preview ? (
            <PreviewFilmButton />
          ) : (
            <StoryFilmControl
              cloudState={c}
              workspaceState={s}
              onSave={cloud.save}
              otherBusy={busy || !!adapter.state.workbenchPending}
              primary
            />
          )
        }
        onNewScene={onNewScene}
        adapter={adapter}
        onLegacyGuide={onLegacyGuide}
        onCanvas={onCanvas}
      />
      {workbenchReceipt && (
        <WorkbenchConfirmation
          receipt={workbenchReceipt}
          shotLabels={Object.fromEntries(
            view.shots.map((shot, i) => [
              shot.id,
              shot.title || at('workbench.shotN', { number: i + 1 }),
            ]),
          )}
          onClose={() => {
            if (checkRun.current) checkRun.current.dropped = true;
            setWorkbenchReceipt(null);
          }}
        />
      )}
    </>
  );
}
