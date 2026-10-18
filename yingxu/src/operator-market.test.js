import test from 'node:test';
import assert from 'node:assert/strict';
import { createOperatorClient } from './operator-client.js';
import { createOperatorController } from './operator-controller.js';
import {
  operatorModels,
  operatorCandidateSelection,
  operatorCandidateCurrent,
  operatorCandidatePreviewMatches,
  initialOperatorMarket,
  operatorMarketReducer,
  operatorSlotsLabel,
  operatorMarketSummary,
  operatorMarketProvider,
  roundedSpec,
} from './operator-model.js';

const catalog = {
  profiles: [
    { id: 'pruned-5090', model_id: 'MiniMax-H3-Pruned-Rank8-INT8', gpu_models: ['RTX 5090'] },
    {
      id: 'pruned-pro',
      model_id: 'MiniMax-H3-Pruned-Rank8-INT8',
      gpu_models: ['RTX PRO 6000 Blackwell'],
    },
    { id: 'base-bf16', model_id: 'MiniMax-H3-Base-BF16' },
    { id: 'base-int8', model_id: 'MiniMax-H3-Base-INT8' },
  ],
};
const query = { model_id: 'MiniMax-H3-Pruned-Rank8-INT8', mode: 'ref', ttl_seconds: 10800 };
const row = {
  provider: 'targon',
  offer_id: 'rtx6000b-small',
  gpu_type: 'RTX PRO 6000 Blackwell',
  gpu_count: 1,
  available_count: 2,
  observed_at: 990,
  selection: {
    runtime_profile_id: 'pruned-pro',
    mode: 'ref',
    provider: 'targon',
    gpu_type: 'RTX PRO 6000 Blackwell',
    gpu_count: 1,
    node_count: 1,
    ttl_seconds: 10800,
    filters: {},
    offer_id: 'rtx6000b-small',
  },
};
const result = {
  ...query,
  observed_at: 995,
  fresh_seconds: 120,
  providers: [{ provider: 'targon', status: 'ok', observed_at: 990 }],
  candidates: [row],
};

test('model-first catalog groups hardware profiles without combining precision variants', () => {
  const models = operatorModels(catalog);
  assert.equal(models.length, 3);
  assert.deepEqual(
    models[0].profiles.map((profile) => profile.id),
    ['pruned-5090', 'pruned-pro'],
  );
  assert.match(models[0].label, /Pruned Rank8 INT8/);
  assert.doesNotMatch(models[0].label, /5090|PRO 6000/);
  assert.notEqual(models[1].id, models[2].id);
});

test('candidate selection copies the exact server allocation and rejects model, mode, TTL or offer substitution', () => {
  const chosen = operatorCandidateSelection(catalog, query, row);
  assert.deepEqual(chosen, row.selection);
  assert.notEqual(chosen, row.selection);
  assert.notEqual(chosen.filters, row.selection.filters);
  for (const patch of [
    { runtime_profile_id: 'base-bf16' },
    { runtime_profile_id: 'missing' },
    { mode: 'fl' },
    { ttl_seconds: 120 },
    { offer_id: 'other-host' },
    { node_count: 2 },
    { gpu_count: 2 },
    { provider: 'lium' },
  ]) {
    assert.equal(
      operatorCandidateSelection(catalog, query, {
        ...row,
        selection: { ...row.selection, ...patch },
      }),
      null,
    );
  }
});

test('model, mode, window changes and refresh invalidate selected allocation, consent and preview; late responses stay discarded', () => {
  const initial = initialOperatorMarket({ max_ttl_seconds: 10800 });
  assert.deepEqual(initial.query, { model_id: '', mode: 'fl', ttl_seconds: 10800 });
  let state = operatorMarketReducer(initial, { type: 'change', patch: query });
  state = operatorMarketReducer(state, { type: 'scan' });
  const readRevision = state.revision;
  state = operatorMarketReducer(state, { type: 'result', revision: readRevision, result });
  state = operatorMarketReducer(state, { type: 'select', row, selection: row.selection });
  state = operatorMarketReducer(state, {
    type: 'preview',
    revision: state.revision,
    preview: { preview_id: 'old', can_start: true },
  });
  state = operatorMarketReducer(state, { type: 'confirm', value: true });
  for (const patch of [
    { model_id: 'MiniMax-H3-Base-BF16' },
    { mode: 'fl' },
    { ttl_seconds: 7200 },
  ]) {
    const next = operatorMarketReducer(state, { type: 'change', patch });
    for (const key of ['result', 'selection', 'row', 'preview']) assert.equal(next[key], null);
    assert.equal(next.confirmed, false);
    assert.equal(
      operatorMarketReducer(next, { type: 'result', revision: readRevision, result }),
      next,
    );
    assert.equal(
      operatorMarketReducer(next, {
        type: 'preview',
        revision: state.revision,
        preview: { preview_id: 'late' },
      }),
      next,
    );
  }
  const refreshed = operatorMarketReducer(state, { type: 'scan' });
  assert.equal(refreshed.preview, null);
  assert.equal(refreshed.selection, null);
  assert.equal(refreshed.confirmed, false);
  const returned = operatorMarketReducer(state, { type: 'back' });
  assert.equal(returned.result, result);
  assert.deepEqual(returned.query, query);
  assert.equal(returned.selection, null);
  assert.equal(returned.preview, null);
  assert.equal(returned.confirmed, false);
  assert.equal(
    operatorMarketReducer(returned, {
      type: 'preview',
      revision: state.revision,
      preview: { preview_id: 'late' },
    }),
    returned,
  );
});

test('candidate freshness needs matching query and current provider and row observations', () => {
  assert.equal(operatorCandidateCurrent(result, query, row, 1000000), true);
  for (const changed of [
    { ...row, observed_at: 879 },
    { ...row, observed_at: 1001 },
    { ...row, observed_at: null },
  ])
    assert.equal(operatorCandidateCurrent(result, query, changed, 1000000), false);
  assert.equal(
    operatorCandidateCurrent(
      { ...result, providers: [{ provider: 'targon', status: 'error', observed_at: 990 }] },
      query,
      row,
      1000000,
    ),
    false,
  );
  assert.equal(operatorCandidateCurrent(result, { ...query, mode: 'fl' }, row, 1000000), false);
  assert.equal(
    operatorCandidateCurrent(result, { ...query, ttl_seconds: 7200 }, row, 1000000),
    false,
  );
  assert.equal(operatorCandidateCurrent(result, query, row, 1111000), false);
  assert.match(operatorMarketSummary({ ...result, providers: [] }, 1000000), /不能判断/);
});

test('preview must retain exact offer and deployment identity; server filters can become authoritative', () => {
  const preview = {
    selection: { ...row.selection, filters: { min_ram_gib: 96 } },
    selected_offer: row,
  };
  assert.equal(operatorCandidatePreviewMatches(row.selection, preview), true);
  for (const patch of [
    { offer_id: 'other-host' },
    { runtime_profile_id: 'pruned-5090' },
    { gpu_count: 2 },
    { provider: 'lium' },
  ])
    assert.equal(
      operatorCandidatePreviewMatches(row.selection, {
        ...preview,
        selection: { ...preview.selection, ...patch },
      }),
      false,
    );
  assert.equal(
    operatorCandidatePreviewMatches(row.selection, {
      ...preview,
      selected_offer: { ...row, offer_id: 'other-host' },
    }),
    false,
  );
  const { offer_id, ...legacySelection } = row.selection;
  assert.equal(
    operatorCandidatePreviewMatches(row.selection, { selection: legacySelection }),
    false,
  );
});

test('cards only promise per-GPU slots for an exactly qualified topology and round resource telemetry', () => {
  assert.equal(
    operatorSlotsLabel({ gpu_count: 2, execution_slots: 2, deployment_qualified: true }),
    '2 个执行槽 · 每卡 1 槽',
  );
  for (const candidate of [
    { gpu_count: 2, execution_slots: 1, deployment_qualified: true },
    { gpu_count: 2, execution_slots: 2, deployment_qualified: false },
    { gpu_count: 2, execution_slots: null },
  ])
    assert.equal(operatorSlotsLabel(candidate), '执行槽数量待验收');
  assert.equal(roundedSpec(96.123456, 'GiB'), '96.1 GiB');
  assert.equal(roundedSpec(null, 'Mbps'), '尚未上报');
});

test('candidate GET uses only model/mode/window, then explicit preview sends exact selected row without a start', async () => {
  const calls = [],
    client = createOperatorClient({
      fetcher: async (path, options) => {
        calls.push({ path, options });
        return new Response(JSON.stringify(result), {
          headers: { 'Content-Type': 'application/json', 'X-Authenticated-Account': 'owner' },
        });
      },
    });
  client.setAccount('owner');
  await client.candidates({ ...query, provider: 'lium', gpu_count: 8 });
  const search = new URL(calls[0].path, 'https://local.invalid').searchParams;
  assert.deepEqual(Object.fromEntries(search), {
    model_id: query.model_id,
    mode: 'ref',
    ttl_seconds: '10800',
  });
  assert.equal(calls[0].options.method, 'GET');
  await client.preview(operatorCandidateSelection(catalog, query, row));
  assert.deepEqual(JSON.parse(calls[1].options.body), row.selection);
  assert.equal(
    calls.some((call) => call.path.endsWith('/starts')),
    false,
  );
});

test('old-account candidate responses are rejected and never create a pending rental', async () => {
  let finish, account;
  const client = {
    setAccount: (value) => {
      account = value;
    },
    reset() {},
    state: async () => ({ operator: { account }, nodes: [] }),
    catalog: async () => catalog,
    candidates: () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  };
  const controller = createOperatorController({ client, storage: { getItem: () => null } });
  await controller.setAccount('a');
  const read = controller.candidates(query);
  await controller.setAccount('b');
  finish(result);
  await assert.rejects(read, /账户已改变/);
  assert.equal(controller.getState().pending, null);
  assert.equal(controller.getState().account, 'b');
});

const refreshReceipt = {
  request_id: 'refresh-1',
  requested_at: 995,
  providers: ['lium', 'targon'],
  coalesced: false,
};
const refreshed = (lium, targon, patch = {}) => ({
  ...result,
  ...patch,
  providers: [
    ['lium', lium],
    ['targon', targon],
  ].map(([provider, refresh_status]) => ({
    provider,
    status: 'ok',
    observed_at: refresh_status === 'pending' ? 800 : 999,
    refresh_request_id: 'refresh-1',
    refresh_requested_at: 995,
    refresh_status,
  })),
});
const refreshClient = (overrides) => {
  let account;
  return {
    setAccount(value) {
      account = value;
    },
    reset() {},
    state: async () => ({ operator: { account }, nodes: [] }),
    catalog: async () => catalog,
    ...overrides,
  };
};

test('one cookie-authenticated refresh targets the shared inventory endpoint without rental consent or journal', async () => {
  const calls = [],
    client = createOperatorClient({
      fetcher: async (path, options) => {
        calls.push({ path, options });
        return new Response(JSON.stringify(refreshReceipt), {
          status: 202,
          headers: { 'Content-Type': 'application/json', 'X-Authenticated-Account': 'owner' },
        });
      },
    });
  client.setAccount('owner');
  await client.marketRefresh();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/v1/operator/capacity/market-refreshes');
  assert.equal(calls[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].options.body), {});
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.equal(calls[0].options.headers['X-Expected-Account'], 'owner');
});

test('one refresh exposes independent supplier progress and preserves a successful supplier when the other fails', async () => {
  const responses = [
      refreshed('pending', 'pending'),
      refreshed('complete', 'pending'),
      refreshed('complete', 'failed'),
    ],
    updates = [],
    waits = [];
  let writes = 0,
    reads = 0;
  const controller = createOperatorController({
    client: refreshClient({
      marketRefresh: async () => {
        writes++;
        return refreshReceipt;
      },
      candidates: async (selection) => {
        assert.deepEqual(selection, query);
        return responses[reads++];
      },
    }),
    storage: {
      getItem: () => null,
      setItem() {
        throw Error('must not journal rental');
      },
    },
    wait: async (ms) => waits.push(ms),
  });
  await controller.setAccount('owner');
  const final = await controller.refreshCandidates(query, (result) => updates.push(result));
  assert.equal(writes, 1);
  assert.equal(reads, 3);
  assert.deepEqual(waits, [2000, 2000]);
  assert.equal(updates.length, 3);
  assert.equal(final, responses[2]);
  assert.equal(operatorMarketProvider(final, 'lium', 1000000).fresh, true);
  assert.equal(operatorMarketProvider(final, 'targon', 1000000).fresh, false);
  assert.equal(controller.getState().pending, null);
  assert.equal(controller.getState().busy, false);
});

test('refresh status never makes prior or stale stock fresh and does not erase a newer query revision', () => {
  for (const state of ['failed', 'timeout'])
    assert.equal(
      operatorMarketProvider(refreshed(state, 'complete'), 'lium', 1000000).fresh,
      false,
    );
  const pending = refreshed('pending', 'complete');
  pending.providers[0].observed_at = 800;
  assert.equal(operatorMarketProvider(pending, 'lium', 1000000).fresh, false);
  const stale = refreshed('complete', 'complete');
  stale.providers[0].observed_at = 800;
  assert.equal(operatorMarketProvider(stale, 'lium', 1000000).fresh, false);
  const scanning = operatorMarketReducer(initialOperatorMarket({ max_ttl_seconds: 10800 }), {
    type: 'scan',
  });
  const progress = operatorMarketReducer(scanning, {
    type: 'refresh-progress',
    revision: scanning.revision,
    result: refreshed('complete', 'pending'),
  });
  assert.equal(progress.loading, 'candidates');
  assert.match(operatorMarketSummary(progress.result, 1000000), /Lium.*Targon/);
  const changed = operatorMarketReducer(progress, { type: 'change', patch: { mode: 'ref' } });
  assert.equal(
    operatorMarketReducer(changed, {
      type: 'refresh-progress',
      revision: scanning.revision,
      result,
    }),
    changed,
  );
});

test('valid cached candidates remain usable during background refresh but stale, failed and unknown reads do not', () => {
  const original = {
    ...result,
    providers: [
      {
        provider: 'targon',
        status: 'ok',
        observed_at: 990,
        refresh_status: 'pending',
        refresh_requested_at: 999,
      },
    ],
  };
  assert.equal(operatorCandidateCurrent(original, query, row, 1000000), true);
  assert.match(operatorMarketProvider(original, 'targon', 1000000).label, /上次库存仍有效/);
  assert.equal(operatorCandidateCurrent(original, query, row, 1111000), false);
  assert.equal(
    operatorCandidateCurrent(
      { ...original, providers: [{ ...original.providers[0], status: 'unconfirmed' }] },
      query,
      row,
      1000000,
    ),
    false,
  );
  for (const refresh_status of ['failed', 'timeout'])
    assert.equal(
      operatorCandidateCurrent(
        { ...original, providers: [{ ...original.providers[0], refresh_status }] },
        query,
        row,
        1000000,
      ),
      false,
    );
  const state = {
    ...initialOperatorMarket({ max_ttl_seconds: 10800 }),
    query,
    result: original,
    selection: row.selection,
    preview: { preview_id: 'old' },
  };
  const next = operatorMarketReducer(state, { type: 'scan' });
  assert.equal(next.result, original);
  assert.equal(next.selection, null);
  assert.equal(next.preview, null);
  assert.equal(next.loading, 'candidates');
});

test('a fresh candidate enters exact preview while the other supplier is still pending', async () => {
  for (const readyProvider of ['lium', 'targon']) {
    let release,
      waiting = new Promise((resolve) => {
        release = resolve;
      }),
      reads = 0,
      previewCalls = 0;
    const candidate = {
      ...row,
      provider: readyProvider,
      selection: { ...row.selection, provider: readyProvider },
    };
    const first = {
      ...refreshed(
        readyProvider === 'lium' ? 'complete' : 'pending',
        readyProvider === 'targon' ? 'complete' : 'pending',
      ),
      candidates: [candidate],
    };
    const final = { ...refreshed('complete', 'complete'), candidates: [candidate] };
    const controller = createOperatorController({
      client: refreshClient({
        marketRefresh: async () => refreshReceipt,
        candidates: async () => (reads++ ? final : first),
        preview: async (selection) => {
          previewCalls++;
          assert.deepEqual(selection, candidate.selection);
          return { preview_id: 'preview-exact', selection };
        },
      }),
      storage: { getItem: () => null },
      wait: () => waiting,
      now: () => 1000000,
    });
    await controller.setAccount('owner');
    let partial;
    const scan = controller.refreshCandidates(query, (value) => {
      partial = value;
    });
    while (!partial) await Promise.resolve();
    assert.equal(controller.getState().busy, false);
    assert.equal(controller.getState().refreshingCandidates, true);
    assert.equal(operatorCandidateCurrent(partial, query, candidate, 1000000), true);
    const preview = await controller.preview(candidate.selection);
    assert.equal(preview.preview_id, 'preview-exact');
    assert.equal(previewCalls, 1);
    assert.equal(controller.getState().pending, null);
    release();
    await scan;
    assert.equal(controller.getState().refreshingCandidates, false);
  }
});

test('another operator can refresh advisory stock without changing this model selection or enabling older refresh data', async () => {
  const newer = refreshed('complete', 'complete');
  newer.providers = newer.providers.map((item) => ({
    ...item,
    refresh_request_id: 'refresh-2',
    refresh_requested_at: 996,
  }));
  let response = newer;
  const controller = createOperatorController({
    client: refreshClient({
      marketRefresh: async () => refreshReceipt,
      candidates: async () => response,
    }),
    storage: { getItem: () => null },
  });
  await controller.setAccount('owner');
  assert.equal(await controller.refreshCandidates(query), newer);
  response = refreshed('complete', 'complete');
  response.providers[0].refresh_requested_at = 994;
  await assert.rejects(controller.refreshCandidates(query), /本轮记录/);
  assert.equal(controller.getState().pending, null);
});

test('bounded browser polling stops while keeping a pending supplier explicit; old account cannot receive refresh progress', async () => {
  let reads = 0,
    waits = 0;
  const controller = createOperatorController({
    client: refreshClient({
      marketRefresh: async () => refreshReceipt,
      candidates: async () => {
        reads++;
        return refreshed('complete', 'pending');
      },
    }),
    storage: { getItem: () => null },
    wait: async () => {
      waits++;
    },
  });
  await controller.setAccount('owner');
  const final = await controller.refreshCandidates(query);
  assert.equal(reads, 31);
  assert.equal(waits, 30);
  assert.equal(final.providers[1].refresh_status, 'pending');
  assert.equal(operatorMarketProvider(final, 'targon', 1000000).fresh, false);
  let finish;
  const updates = [],
    switched = createOperatorController({
      client: refreshClient({
        marketRefresh: async () => refreshReceipt,
        candidates: () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      }),
      storage: { getItem: () => null },
    });
  await switched.setAccount('a');
  const read = switched.refreshCandidates(query, (value) => updates.push(value));
  await Promise.resolve();
  await switched.setAccount('b');
  finish(refreshed('complete', 'complete'));
  await assert.rejects(read, /账户已改变/);
  assert.equal(updates.length, 0);
  assert.equal(switched.getState().account, 'b');
  assert.equal(switched.getState().pending, null);
});

test('slow reads respect the wall-clock refresh deadline and closing the drawer stops later reads', async () => {
  let clock = 0,
    reads = 0;
  const controller = createOperatorController({
    client: refreshClient({
      marketRefresh: async () => refreshReceipt,
      candidates: async () => {
        reads++;
        clock += 40000;
        return refreshed('complete', 'pending');
      },
    }),
    storage: { getItem: () => null },
    now: () => clock,
    wait: async (ms) => {
      clock += ms;
    },
  });
  await controller.setAccount('owner');
  const final = await controller.refreshCandidates(query);
  assert.equal(reads, 2);
  assert.equal(final.providers[1].refresh_status, 'pending');
  assert.equal(controller.getState().busy, false);
  const signal = new AbortController();
  let closedReads = 0;
  const closed = createOperatorController({
    client: refreshClient({
      marketRefresh: async () => refreshReceipt,
      candidates: async () => {
        closedReads++;
        return refreshed('complete', 'pending');
      },
    }),
    storage: { getItem: () => null },
    wait: async () => signal.abort(),
  });
  await closed.setAccount('owner');
  await closed.refreshCandidates(query, () => {}, signal.signal);
  assert.equal(closedReads, 1);
  assert.equal(closed.getState().busy, false);
  assert.equal(closed.getState().pending, null);
});

test('a saved pending refresh expires visibly after one minute even when browser polling has ended', () => {
  const waiting = refreshed('complete', 'pending');
  assert.equal(operatorMarketProvider(waiting, 'targon', 1054000).refreshing, true);
  const expired = operatorMarketProvider(waiting, 'targon', 1055000);
  assert.equal(expired.refreshing, false);
  assert.equal(expired.refresh_status, 'timeout');
  assert.equal(expired.refresh_reason_code, 'inventory_refresh_timeout');
  assert.equal(expired.fresh, false);
  assert.equal(waiting.providers[1].refresh_status, 'pending');
  assert.doesNotMatch(operatorMarketSummary(waiting, 1055000), /正在查询/);
  assert.equal(operatorMarketProvider(waiting, 'lium', 1055000).fresh, true);
});
