import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createFilmClient,
  createFilmController,
  filmGrantMatches,
  filmLinkRun,
  filmReceipt,
} from './story-film.js';

export const filmFixture = (patch = {}) => ({
  id: 'film-original',
  project_id: 'story',
  project_version: 7,
  snapshot_hash: 'a'.repeat(64),
  workflow_version: 'inkseq-film-v1',
  phase: 'preflight',
  accepted_at: null,
  cancel_requested: false,
  error_code: null,
  warnings: [],
  generation: [{ shot_id: 'shot', plan_id: 'plan-original', seed: 17, job_id: null }],
  chapters: [{ chapter_id: 'chapter', plan_id: null, job_id: null, artifact_id: null }],
  export: { plan_id: null, job_id: null, artifact_id: null },
  counts: {
    shots: 1,
    reused: 0,
    generate: 1,
    generated_completed: 0,
    chapters_completed: 0,
    export_completed: 0,
  },
  estimate: { quote_known: true, cost_microusd: 100000, final_bill: false },
  options: { resolution: '720P', burn_subtitles: false },
  ...patch,
});
function harness() {
  const values = new Map(),
    calls = [];
  let context = {
      account: 'one',
      projectId: 'story',
      epoch: 1,
      mode: 'cloud',
      workspaceAccount: 'one',
      enabled: true,
      workflowVersion: 'inkseq-film-v1',
      serverVersion: 7,
      dirty: false,
    },
    number = 0;
  const storage = {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  const api = {
    reset() {},
    setAccount() {},
    async preflight(project, body, key) {
      calls.push({ kind: 'preflight', project, body: structuredClone(body), key });
      return filmFixture();
    },
    async submit(project, body, key, approval) {
      calls.push({ kind: 'submit', project, body: structuredClone(body), key, approval });
      return filmFixture({ phase: 'generating', accepted_at: 100 });
    },
    async get(project, id) {
      calls.push({ kind: 'get', project, id });
      return filmFixture({ phase: 'generating', accepted_at: 100 });
    },
    async cancel(project, id) {
      calls.push({ kind: 'cancel', project, id });
      return filmFixture({ phase: 'cancel_requested', accepted_at: 100, cancel_requested: true });
    },
    async resume(project, id) {
      calls.push({ kind: 'resume', project, id });
      return filmFixture({ phase: 'generating', accepted_at: 100 });
    },
    async download(id) {
      calls.push({ kind: 'download', id });
      return new Blob(['video']);
    },
  };
  const controller = createFilmController({
    api,
    storage,
    getContext: () => context,
    keyFactory: () => `key-${++number}`,
  });
  controller.activate(context);
  return {
    controller,
    api,
    values,
    calls,
    storage,
    get context() {
      return context;
    },
    change(patch) {
      context = { ...context, ...patch };
    },
    activate() {
      controller.activate(context);
    },
  };
}
const options = { resolution: '720P', burn_subtitles: false };

test('typed preflight refusals allow correcting options; unknown outcomes retain the original request', async () => {
  for (const status of [409, 422]) {
    const h = harness();
    h.api.preflight = async () => {
      throw Object.assign(Error('caption confirmation'), {
        status,
        detail: { detail: 'film_captions_require_confirmation' },
      });
    };
    await assert.rejects(h.controller.preflight(options), /caption confirmation/);
    assert.equal(h.controller.getState().pending, null);
    assert.equal(h.controller.getState().error, 'film_captions_require_confirmation');
    h.api.preflight = async () => filmFixture();
    await h.controller.preflight({ ...options, burn_subtitles: true });
    assert.equal(h.controller.getState().run.id, 'film-original');
    assert.equal(
      h.calls.some((call) => call.kind === 'submit'),
      false,
    );
  }
  for (const status of [0, 408, 429, 503]) {
    const h = harness();
    h.api.preflight = async () => {
      throw Object.assign(Error('unknown outcome'), {
        status,
        detail: { code: 'film_captions_require_confirmation' },
      });
    };
    await assert.rejects(h.controller.preflight(options));
    assert.equal(h.controller.getState().pending.kind, 'film-preflight');
  }
  const stale = harness();
  stale.api.preflight = async () => {
    throw Object.assign(Error('stale project'), {
      status: 409,
      detail: { detail: 'document_version_conflict' },
    });
  };
  await assert.rejects(stale.controller.preflight(options));
  assert.equal(stale.controller.getState().pending, null);
  const h = harness();
  await h.controller.preflight(options);
  h.api.submit = async () => {
    throw Object.assign(Error('submit refused'), {
      status: 409,
      detail: { code: 'film_confirmation_required' },
    });
  };
  await assert.rejects(h.controller.confirm(true));
  assert.equal(h.controller.getState().pending.kind, 'film-submit');
});

test('preflight is not paid submission; dirty or stale revisions cannot be confirmed', async () => {
  const h = harness();
  h.change({ dirty: true });
  await assert.rejects(h.controller.preflight(options), /saveFirst/);
  assert.equal(h.calls.length, 0);
  h.change({ dirty: false });
  await h.controller.preflight(options);
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].body.expected_version, 7);
  h.change({ serverVersion: 8 });
  await assert.rejects(h.controller.confirm(true), /confirmChanged/);
  h.change({ serverVersion: 7 });
  await assert.rejects(h.controller.confirm(false), /confirmChanged/);
  assert.equal(h.calls.filter((c) => c.kind === 'submit').length, 0);
  await h.controller.confirm(true);
  assert.deepEqual(h.calls[1].body, { preflight_id: 'film-original', confirm: true });
  assert.equal(h.controller.getState().run.project_version, 7);
});

test('lost preflight response retains original body/key before IO; explicit recovery replays only that operation', async () => {
  const h = harness();
  let original;
  h.api.preflight = async (project, body, key) => {
    const pending = [...h.values.entries()].find(([k]) => k.includes('command-v1'));
    assert.ok(pending, 'operation must be durable before request');
    if (!original) {
      original = { project, body: structuredClone(body), key };
      throw Error('network lost');
    }
    assert.deepEqual({ project, body, key }, original);
    return filmFixture();
  };
  await assert.rejects(h.controller.preflight(options), /network lost/);
  assert.equal(h.controller.getState().pending.key, original.key);
  await assert.rejects(h.controller.preflight(options), /film.pending/);
  h.change({ serverVersion: 8, dirty: true });
  await h.controller.recover();
  assert.equal(h.controller.getState().pending, null);
  assert.equal(h.controller.getState().run.project_version, 7);
  await assert.rejects(h.controller.confirm(true), /confirmChanged/);
});

test('unknown submit checks accepted original run and never sends a duplicate paid submission', async () => {
  const h = harness();
  await h.controller.preflight(options);
  h.api.submit = async (project, body, key) => {
    h.calls.push({ kind: 'submit', project, body, key });
    throw Error('ack lost');
  };
  await assert.rejects(h.controller.confirm(true), /ack lost/);
  const pending = structuredClone(h.controller.getState().pending);
  await h.controller.recover();
  assert.equal(h.calls.filter((c) => c.kind === 'submit').length, 1);
  assert.equal(h.calls.at(-1).id, pending.body.preflight_id);
  assert.equal(h.controller.getState().pending, null);
  assert.equal(h.controller.getState().run.phase, 'generating');
});

test('when original submit is not accepted, recovery keeps exact original body/key and approval bound', async () => {
  const h = harness();
  await h.controller.preflight(options);
  let initial;
  h.api.submit = async (project, body, key, approval) => {
    const call = { project, body: structuredClone(body), key };
    if (!initial) {
      initial = call;
      throw Error('unknown');
    }
    assert.deepEqual(call, initial);
    assert.equal(approval.account, 'one');
    assert.equal(approval.run.snapshot_hash, 'a'.repeat(64));
    return filmFixture({ phase: 'generating', accepted_at: 100 });
  };
  h.api.get = async () => filmFixture();
  await assert.rejects(h.controller.confirm(true));
  await h.controller.recover();
  assert.equal(h.controller.getState().pending, null);
});

test('late account/visit completion cannot publish a run, clear old pending intent or unlock another visit', async () => {
  const h = harness();
  let finish;
  h.api.preflight = () =>
    new Promise((resolve) => {
      finish = resolve;
    });
  const request = h.controller.preflight(options);
  h.change({ account: 'two', workspaceAccount: 'two', epoch: 2 });
  h.activate();
  finish(filmFixture());
  await assert.rejects(request, /contextChanged/);
  assert.equal(h.controller.getState().run, null);
  assert.equal(h.controller.getState().busy, false);
  const old = [...h.values].find(([k]) => k.includes('command-v1:one:story'));
  assert.ok(old, 'old original operation must remain');
  assert.equal(
    [...h.values.keys()].some((k) => k.includes('run-v1:two:')),
    false,
  );
});

test('receipt snapshot changes refuse without discarding original pending request', async () => {
  const h = harness();
  await h.controller.preflight(options);
  h.api.submit = async () =>
    filmFixture({ phase: 'generating', accepted_at: 100, snapshot_hash: 'b'.repeat(64) });
  await assert.rejects(h.controller.confirm(true), /receiptChanged/);
  assert.equal(h.controller.getState().run.snapshot_hash, 'a'.repeat(64));
  assert.equal(h.controller.getState().pending.kind, 'film-submit');
  assert.equal(
    filmReceipt(
      filmFixture({ counts: { ...filmFixture().counts, generated_completed: 2 } }),
      'story',
    ),
    false,
  );
});

test('cancel recovery reads original result; resume uses same original run and no generation endpoint', async () => {
  const h = harness();
  await h.controller.preflight(options);
  await h.controller.confirm(true);
  h.api.cancel = async (project, id) => {
    h.calls.push({ kind: 'cancel', project, id });
    throw Error('unknown cancel');
  };
  await assert.rejects(h.controller.change('cancel'));
  h.api.get = async () =>
    filmFixture({ phase: 'cancel_requested', accepted_at: 100, cancel_requested: true });
  h.change({ enabled: false });
  await h.controller.recover();
  assert.equal(h.calls.filter((c) => c.kind === 'cancel').length, 1);
  assert.equal(h.controller.getState().pending, null);
  const second = harness();
  await second.controller.preflight(options);
  await second.controller.confirm(true);
  await second.controller.change('resume');
  assert.equal(second.calls.at(-1).id, 'film-original');
  assert.equal(second.calls.filter((c) => c.kind === 'submit').length, 1);
});

test('reload reads persisted original pointer, and download stays available with creation disabled', async () => {
  const h = harness();
  await h.controller.preflight(options);
  await h.controller.confirm(true);
  h.activate();
  assert.equal(h.controller.getState().run, null);
  h.api.get = async () =>
    filmFixture({
      phase: 'completed',
      accepted_at: 100,
      export: { artifact_id: 'export-original' },
      counts: {
        ...filmFixture().counts,
        generated_completed: 1,
        chapters_completed: 1,
        export_completed: 1,
      },
    });
  h.change({ enabled: false });
  await h.controller.refresh();
  const blob = await h.controller.download();
  assert.equal(await blob.text(), 'video');
  assert.equal(h.calls.at(-1).id, 'export-original');
});

test('shared film link reads only its explicit project and original run, without modifying the saved story', async () => {
  assert.equal(filmLinkRun('/?project=story&film=film-original', 'story'), 'film-original');
  for (const href of [
    '/?film=film-original',
    '/?project=other&film=film-original',
    '/?project=story&project=other&film=film-original',
    '/?project=story&film=film-original&film=other',
    '/?project=story&film=%00',
  ])
    assert.equal(filmLinkRun(href, 'story'), null);
  const h = harness();
  h.change({ enabled: false, dirty: true });
  await h.controller.openRun('film-original');
  assert.deepEqual(h.calls, [{ kind: 'get', project: 'story', id: 'film-original' }]);
  assert.equal(h.context.dirty, true);
  h.activate();
  await h.controller.refresh();
  assert.equal(h.controller.getState().run.id, 'film-original');
  h.api.get = async () => filmFixture({ id: 'other' });
  await assert.rejects(h.controller.openRun('film-original'), /receiptChanged/);
  assert.equal(h.controller.getState().run.id, 'film-original');
});

test('shared link cannot replace an unknown original operation or publish across an account change', async () => {
  const h = harness();
  h.api.preflight = async () => {
    throw Error('unknown');
  };
  await assert.rejects(h.controller.preflight(options));
  const original = structuredClone(h.controller.getState().pending);
  await assert.rejects(h.controller.openRun('other-run'), /film.pending/);
  assert.deepEqual(h.controller.getState().pending, original);
  assert.equal(h.calls.length, 0);
  const second = harness();
  let finish;
  second.api.get = () => new Promise((resolve) => (finish = resolve));
  const opened = second.controller.openRun('film-original');
  second.change({ account: 'two', workspaceAccount: 'two', epoch: 2 });
  second.activate();
  finish(filmFixture({ phase: 'generating', accepted_at: 100 }));
  await assert.rejects(opened, /contextChanged/);
  assert.equal(second.controller.getState().run, null);
  assert.equal(
    [...second.values.keys()].some((key) => key.includes('run-v1:two:')),
    false,
  );
});

test('spending grant is limited to exact frozen plan identities, account and confirmed upper estimate', () => {
  const approval = { account: 'one', run: filmFixture() };
  const detail = {
    plan_id: 'plan-original',
    plan_ids: ['plan-original'],
    estimate_microusd: 100000,
  };
  assert.equal(filmGrantMatches(approval, detail, 'one'), true);
  for (const [changed, account] of [
    [{ ...detail, estimate_microusd: 100001 }, 'one'],
    [{ ...detail, plan_ids: ['other'] }, 'one'],
    [{ ...detail, plan_ids: ['plan-original', 'other'] }, 'one'],
    [detail, 'two'],
  ])
    assert.equal(filmGrantMatches(approval, changed, account), false);
  assert.equal(
    filmGrantMatches(
      {
        account: 'one',
        run: filmFixture({ estimate: { quote_known: false, cost_microusd: null } }),
      },
      detail,
      'one',
    ),
    false,
  );
});

test('real cloud-client grant retry uses original film body/key/account, without a browser confirm', async () => {
  const calls = [];
  const api = createFilmClient({
    fetcher: async (path, options) => {
      calls.push({ path, ...options });
      if (calls.length === 1)
        return new Response(
          JSON.stringify({
            code: 'spending_confirmation_required',
            plan_id: 'plan-original',
            plan_ids: ['plan-original'],
            estimate_microusd: 100000,
          }),
          { status: 409 },
        );
      return new Response(JSON.stringify(filmFixture({ phase: 'generating', accepted_at: 100 })), {
        status: 200,
      });
    },
  });
  api.setAccount('one');
  const body = { preflight_id: 'film-original', confirm: true };
  await api.submit('story', body, 'original-submit-key', { account: 'one', run: filmFixture() });
  assert.equal(calls.length, 3);
  assert.equal(calls[1].path, '/v1/account/spending-confirmations');
  assert.equal(calls[0].body, calls[2].body);
  assert.equal(calls[2].headers['Idempotency-Key'], 'original-submit-key');
  assert.ok(calls.every((call) => call.headers['X-Expected-Account'] === 'one'));
});
