import { createCloudClient } from './cloud-client.js';
import { storyboardCommandStore } from './storyboard-commands.js';

const copy = (value) => structuredClone(value);
const phases = new Set([
  'preflight',
  'generating',
  'admission_blocked',
  'authorization_required',
  'chapters',
  'exporting',
  'blocked',
  'cancel_requested',
  'cancelled',
  'completed',
]);
const id = (value) =>
  typeof value === 'string' &&
  value.length > 0 &&
  value.length <= 200 &&
  !/[\x00-\x1f]/.test(value);
const money = (value) => Number.isSafeInteger(value) && value >= 0;
export const filmTerminal = (run) => ['completed', 'cancelled'].includes(run?.phase);
const ownerReady = (context) =>
  !!context?.account && context.mode === 'cloud' && context.workspaceAccount === context.account;
export function filmReady(context) {
  return (
    ownerReady(context) && context.enabled === true && context.workflowVersion === 'inkseq-film-v1'
  );
}
export function filmLinkRun(href, projectId) {
  const query = new URL(href, 'http://localhost/').searchParams;
  return query.getAll('project').length === 1 &&
    query.get('project') === projectId &&
    query.getAll('film').length === 1 &&
    id(query.get('film'))
    ? query.get('film')
    : null;
}
export function filmReceipt(run, projectId, expected = null) {
  return (
    !!run &&
    run.project_id === projectId &&
    id(run.id) &&
    Number.isSafeInteger(run.project_version) &&
    run.project_version > 0 &&
    /^[a-f0-9]{64}$/.test(run.snapshot_hash) &&
    run.workflow_version === 'inkseq-film-v1' &&
    phases.has(run.phase) &&
    (!expected ||
      (run.id === expected.id &&
        run.snapshot_hash === expected.snapshot_hash &&
        run.project_version === expected.project_version)) &&
    Array.isArray(run.generation) &&
    Array.isArray(run.chapters) &&
    run.export &&
    run.counts &&
    [
      'shots',
      'reused',
      'generate',
      'generated_completed',
      'chapters_completed',
      'export_completed',
    ].every((key) => money(run.counts[key])) &&
    run.counts.reused + run.counts.generate === run.counts.shots &&
    run.generation.length === run.counts.generate &&
    new Set(run.generation.map((item) => item.plan_id)).size === run.generation.length &&
    run.generation.every((item) => id(item.plan_id) && id(item.shot_id)) &&
    run.counts.generated_completed <= run.counts.generate &&
    run.counts.chapters_completed <= run.chapters.length &&
    run.counts.export_completed <= 1 &&
    (run.phase !== 'completed' ||
      (id(run.export.artifact_id) &&
        run.counts.export_completed === 1 &&
        run.counts.generated_completed === run.counts.generate &&
        run.counts.chapters_completed === run.chapters.length))
  );
}
export function filmGrantMatches(approval, detail, account) {
  if (
    !approval ||
    approval.account !== account ||
    approval.run.estimate?.quote_known !== true ||
    !money(approval.run.estimate.cost_microusd) ||
    !money(detail?.estimate_microusd) ||
    detail.estimate_microusd > approval.run.estimate.cost_microusd
  )
    return false;
  const expected = approval.run.generation.map((item) => item.plan_id);
  const actual = detail.plan_ids || [detail.plan_id];
  return (
    Array.isArray(actual) &&
    actual.length > 0 &&
    actual.length === expected.length &&
    new Set(expected).size === expected.length &&
    new Set(actual).size === actual.length &&
    actual.includes(detail.plan_id) &&
    actual.every((plan) => id(plan) && expected.includes(plan))
  );
}
export function createFilmClient(options = {}) {
  let approval = null;
  const client = createCloudClient({
    ...options,
    confirmSpending: (detail, account) => filmGrantMatches(approval, detail, account),
  });
  const path = (project) => `/v1/projects/${encodeURIComponent(project)}/films`;
  return {
    setAccount: client.setAccount,
    reset: client.reset,
    preflight: (project, body, key) =>
      client.request(`${path(project)}/preflights`, { method: 'POST', body, key }),
    get: (project, run) => client.request(`${path(project)}/${encodeURIComponent(run)}`),
    async submit(project, body, key, confirmed) {
      if (approval) throw Error('film.busy');
      approval = confirmed;
      try {
        return await client.request(path(project), { method: 'POST', body, key });
      } finally {
        approval = null;
      }
    },
    cancel: (project, run) =>
      client.request(`${path(project)}/${encodeURIComponent(run)}/cancel`, { method: 'POST' }),
    resume: (project, run) =>
      client.request(`${path(project)}/${encodeURIComponent(run)}/resume`, { method: 'POST' }),
    download: (artifact) =>
      client.download(`/v1/artifacts/${encodeURIComponent(artifact)}/content?download=true`),
  };
}

// This stores only the original operation identity/run pointer. Domain jobs,
// financial receipts and immutable project snapshots remain server-owned.
export function createFilmController({ api, storage, getContext, keyFactory }) {
  let context = null,
    commands = null,
    pointer = null,
    serial = false,
    generation = 0;
  let state = { run: null, pending: null, busy: false, error: '' };
  const listeners = new Set();
  const publish = (patch) => {
    state = { ...state, ...patch };
    for (const listener of listeners) listener();
  };
  const same = (left, right) =>
    left &&
    right &&
    ['account', 'projectId', 'epoch', 'mode', 'workspaceAccount'].every(
      (key) => left[key] === right[key],
    );
  const guard = (scope) => {
    if (!same(scope, context) || !same(scope, getContext())) throw Error('film.contextChanged');
  };
  function keep(run, scope, expected = null) {
    guard(scope);
    if (!filmReceipt(run, scope.projectId, expected)) throw Error('film.receiptChanged');
    storage.setItem(
      pointer,
      JSON.stringify({
        id: run.id,
        project_id: run.project_id,
        project_version: run.project_version,
        snapshot_hash: run.snapshot_hash,
      }),
    );
    publish({ run: copy(run), error: '' });
    return run;
  }
  async function action(work, requireEnabled = true) {
    if (serial) throw Error('film.busy');
    const scope = context,
      readGeneration = ++generation;
    guard(scope);
    if (!(requireEnabled ? filmReady(getContext()) : ownerReady(getContext())))
      throw Error('film.unavailable');
    serial = true;
    publish({ busy: true, error: '' });
    try {
      return await work(scope);
    } catch (error) {
      if (same(scope, context) && same(scope, getContext()))
        publish({
          error: error?.detail?.code || error?.detail?.detail || error?.message || 'film.unknown',
          pending: commands.read(),
        });
      throw error;
    } finally {
      // A late completion from an old account/visit cannot unlock the new one.
      if (readGeneration === generation) {
        serial = false;
        publish({ busy: false });
      }
    }
  }
  async function execute(op, scope) {
    const expected = state.run;
    let run;
    if (op.kind === 'film-preflight') {
      run = await api.preflight(scope.projectId, op.body, op.key);
      if (run?.project_version !== op.body.expected_version) throw Error('film.receiptChanged');
      keep(run, scope);
    } else if (op.kind === 'film-submit') {
      if (!expected || expected.id !== op.body.preflight_id) throw Error('film.receiptChanged');
      run = await api.submit(scope.projectId, op.body, op.key, {
        account: scope.account,
        run: expected,
      });
      if (!run?.accepted_at || run.phase === 'preflight') throw Error('film.receiptChanged');
      keep(run, scope, expected);
    } else {
      if (
        !expected ||
        expected.id !== op.body.run_id ||
        !['film-cancel', 'film-resume'].includes(op.kind)
      )
        throw Error('film.receiptChanged');
      run = await api[op.kind === 'film-cancel' ? 'cancel' : 'resume'](
        scope.projectId,
        expected.id,
      );
      keep(run, scope, expected);
    }
    guard(scope);
    commands.clear(op);
    publish({ pending: null });
    return run;
  }
  return {
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getState: () => state,
    activate(next) {
      ++generation;
      serial = false;
      api.reset();
      context = copy(next);
      api.setAccount(next.account || null);
      commands = storyboardCommandStore({
        storage,
        account: next.account,
        projectId: next.projectId,
        namespace: 'film',
        keyFactory,
      });
      pointer = `inkseq-story-film-run-v1:${next.account}:${next.projectId}`;
      let saved = null,
        pending = null,
        error = '';
      try {
        saved = JSON.parse(storage.getItem(pointer) || 'null');
        pending = commands.read();
        if (
          saved &&
          (!id(saved.id) ||
            saved.project_id !== next.projectId ||
            !/^[a-f0-9]{64}$/.test(saved.snapshot_hash))
        )
          throw Error('film.receiptChanged');
        if (
          pending &&
          (pending.account !== next.account ||
            pending.projectId !== next.projectId ||
            !id(pending.key))
        )
          throw Error('film.receiptChanged');
      } catch {
        error = 'film.pendingCorrupt';
      }
      publish({ context: copy(next), run: null, pending, saved, busy: false, error });
    },
    deactivate() {
      ++generation;
      serial = false;
      api.reset();
      context = null;
      publish({ run: null, pending: null, busy: false, error: '' });
    },
    async refresh() {
      const scope = context,
        readGeneration = generation,
        expected = state.run || state.saved;
      if (!expected?.id || !ownerReady(getContext()) || state.pending || serial) return null;
      const run = await api.get(scope.projectId, expected.id);
      if (readGeneration !== generation) return null;
      return keep(run, scope, expected);
    },
    openRun(runId) {
      return action(async (scope) => {
        if (commands.read()) throw Error('film.pending');
        if (!id(runId)) throw Error('film.receiptChanged');
        const expected = state.run?.id === runId ? state.run : null;
        const run = await api.get(scope.projectId, runId);
        if (run?.id !== runId) throw Error('film.receiptChanged');
        return keep(run, scope, expected);
      }, false);
    },
    preflight(options) {
      return action(async (scope) => {
        const live = getContext();
        if (live.externalBusy) throw Error('film.busy');
        if (commands.read()) throw Error('film.pending');
        if (live.dirty || !Number.isInteger(live.serverVersion) || live.serverVersion < 1)
          throw Error('film.saveFirst');
        if (state.run && !filmTerminal(state.run) && state.run.phase !== 'preflight')
          throw Error('film.active');
        const op = commands.begin('film-preflight', {
          expected_version: live.serverVersion,
          resolution: options.resolution,
          aspect: options.aspect,
          burn_subtitles: options.burn_subtitles,
        });
        publish({ pending: op });
        return execute(op, scope);
      });
    },
    confirm(confirmed) {
      return action(async (scope) => {
        const live = getContext(),
          run = state.run;
        if (live.externalBusy) throw Error('film.busy');
        if (commands.read()) throw Error('film.pending');
        if (
          confirmed !== true ||
          run?.phase !== 'preflight' ||
          live.dirty ||
          live.serverVersion !== run.project_version ||
          run.estimate?.quote_known !== true ||
          !money(run.estimate?.cost_microusd)
        )
          throw Error('film.confirmChanged');
        const op = commands.begin('film-submit', { preflight_id: run.id, confirm: true });
        publish({ pending: op });
        return execute(op, scope);
      });
    },
    recover() {
      return action(async (scope) => {
        const op = commands.read();
        if (!op) throw Error('film.pending');
        if (op.kind !== 'film-preflight') {
          const run = await api.get(scope.projectId, op.body.preflight_id || op.body.run_id);
          keep(run, scope, state.run || state.saved);
          if (
            (op.kind === 'film-submit' && run.accepted_at && run.phase !== 'preflight') ||
            (op.kind === 'film-cancel' && (run.cancel_requested || run.phase === 'cancelled'))
          ) {
            commands.clear(op);
            publish({ pending: null });
            return run;
          }
        }
        if (op.kind !== 'film-cancel' && !filmReady(getContext())) throw Error('film.unavailable');
        return execute(op, scope);
      }, false);
    },
    change(kind) {
      return action(async (scope) => {
        if (commands.read()) throw Error('film.pending');
        const run = state.run;
        if (
          !run ||
          filmTerminal(run) ||
          run.phase === 'preflight' ||
          !['cancel', 'resume'].includes(kind)
        )
          throw Error('film.notResumable');
        const op = commands.begin('film-' + kind, { run_id: run.id });
        publish({ pending: op });
        return execute(op, scope);
      }, kind !== 'cancel');
    },
    download() {
      return action(async (scope) => {
        const run = state.run,
          artifact = run?.export?.artifact_id;
        if (run?.phase !== 'completed' || !id(artifact)) throw Error('film.notComplete');
        const blob = await api.download(artifact);
        guard(scope);
        return blob;
      }, false);
    },
  };
}
