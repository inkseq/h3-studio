import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup as renderMarkup } from 'react-dom/server';
import { readFile } from 'node:fs/promises';
import { build } from 'esbuild';
import {
  uniqueOperatorReasons,
  historicalNode,
  operatorNodeGroups,
  operatorNodeStatus,
  operatorBootstrap,
  operatorCapacitySummary,
  filterOperatorHistory,
  operatorExtensionCurrent,
  operatorExtensionReadback,
} from './operator-presentation.js';

const now = 1000000;
const active = {
  id: 'node-live',
  provider: 'lium',
  provider_instance_id: 'instance-live',
  version: 'v1',
  state: 'starting',
  runtime_state: 'preparing',
  observed_at: 999,
  hard_deadline: 1300,
  gpu_model: 'RTX 5090',
  gpu_count: 1,
  mode: 'fl',
  slots: [],
  actions: { drain: { allowed: true }, stop: { allowed: true }, extend: { allowed: true } },
  bootstrap: {
    observed_at: 998,
    slots: [{ index: 0, state: 'preparing', phase: 'model_download' }],
  },
};
const ended = {
  ...active,
  id: 'old-node',
  state: 'destroyed',
  runtime_state: 'failed',
  observed_at: 100,
  hard_deadline: 120,
  reason_code: 'runtime_process_exited',
  bootstrap: {
    observed_at: 95,
    slots: [{ index: 0, phase: 'runtime_start', error_code: 'runtime_process_exited' }],
  },
  slots: [{ id: 'old-slot', state: 'ready', stale: false, current_job_id: 'old-job' }],
  actions: {
    drain: { allowed: false, blockers: ['operator_active_obligations'] },
    stop: {
      allowed: false,
      blockers: [{ code: 'operator_active_obligations' }, 'operator_node_manually_reviewed'],
    },
  },
};

// Bundle the pure view alone so SSR exercises actual JSX without importing browser state.
const { Module } = await import('node:module');
const viewModule = new Module(
  new URL('./operator-machine-view.test-build.cjs', import.meta.url).pathname,
);
// ESM import from a data URL cannot resolve external React packages; esbuild CJS stays in this directory.
const viewPath = new URL('./operator-machine-view.jsx', import.meta.url).pathname.replace(
  /^\/([A-Za-z]:)/,
  '$1',
);
const cjs = await build({
  stdin: {
    contents:
      (await readFile(viewPath, 'utf8')) +
      "\nexport {I18nProvider as TestI18nProvider} from './i18n/I18nProvider.jsx';\n",
    resolveDir: viewPath.replace(/[/\\][^/\\]+$/, ''),
    loader: 'jsx',
  },
  bundle: true,
  write: false,
  platform: 'node',
  format: 'cjs',
  packages: 'external',
  loader: { '.css': 'empty' },
});
viewModule.filename = new URL(
  './operator-machine-view.test-build.cjs',
  import.meta.url,
).pathname.replace(/^\/([A-Za-z]:)/, '$1');
viewModule.paths = Module._nodeModulePaths(viewModule.filename.replace(/[/\\][^/\\]+$/, ''));
viewModule._compile(cjs.outputFiles[0].text, viewModule.filename);
const { NodeCard, MachineRow, CandidateCard, TestI18nProvider } = viewModule.exports;
const renderToStaticMarkup = (element) => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: { getItem: () => 'zh' },
  });
  try {
    return renderMarkup(React.createElement(TestI18nProvider, null, element));
  } finally {
    if (previous) Object.defineProperty(globalThis, 'localStorage', previous);
    else delete globalThis.localStorage;
  }
};
const render = (node) =>
  renderToStaticMarkup(
    React.createElement(NodeCard, { node, profiles: [], disabled: false, onAction: () => {}, now }),
  );

test('ended and manually reviewed records stay in searchable non-destructive history; unresolved review stays visible', () => {
  const reviewed = {
      ...active,
      id: 'reviewed',
      state: 'destroying',
      removal_confirmation: { state: 'manually_reviewed' },
    },
    pending = { ...active, id: 'pending', removal_confirmation: { state: 'overdue' } },
    explicit = { ...ended, id: 'needs-review', record_group: 'pending_review' };
  const source = [ended, reviewed, pending, active, explicit],
    saved = structuredClone(source),
    groups = operatorNodeGroups(source);
  assert.deepEqual(
    groups.current.map((n) => n.id),
    ['node-live'],
  );
  assert.deepEqual(
    groups.pending_review.map((n) => n.id),
    ['pending', 'needs-review'],
  );
  assert.deepEqual(
    groups.history.map((n) => n.id),
    ['reviewed', 'old-node'],
  );
  assert.equal(historicalNode(reviewed), true);
  assert.deepEqual(filterOperatorHistory(groups.history, { search: 'OLD-JOB' }), []);
  assert.deepEqual(
    filterOperatorHistory(groups.history, { search: 'INSTANCE-LIVE', provider: 'lium' }).map(
      (n) => n.id,
    ),
    ['reviewed', 'old-node'],
  );
  assert.deepEqual(filterOperatorHistory(groups.history, { provider: 'targon' }), []);
  assert.deepEqual(source, saved);
  assert.deepEqual(
    filterOperatorHistory([{ ...ended, provider: 'targon' }], { search: 'TARGON' }).map(
      (n) => n.id,
    ),
    ['old-node'],
  );
});
test('destroyed rendering makes deadlines, failures and slots historical without claiming settled bills', () => {
  const html = render(ended);
  assert.match(html, /历史记录 · 已结束/);
  assert.match(html, /历史停止上限/);
  assert.doesNotMatch(html, /期限已到，等待状态核对/);
  assert.match(html, /历史原因/);
  assert.match(html, /历史上报/);
  assert.match(html, /当时任务/);
  assert.match(html, /历史启动观测/);
  assert.match(html, /费用是否结清仍以账本为准/);
  assert.equal((html.match(/operator_active_obligations/g) || []).length, 1);
  assert.match(html, /operator_node_manually_reviewed/);
});
test('normal model download is visible with observed timestamp and active controls retain server decisions', () => {
  const html = render(active);
  assert.match(html, /正在下载模型/);
  assert.match(html, /启动准备进度/);
  assert.match(html, /model_download/);
  assert.match(html, /2 秒前/);
  assert.doesNotMatch(html, /百分之|预计.*分钟|自动重试/);
  assert.doesNotMatch(html, /<button disabled=/);
  assert.match(html, /延长运行/);
  const blocked = render({
    ...active,
    actions: { ...active.actions, stop: { allowed: false, blockers: ['active_jobs'] } },
  });
  assert.match(blocked, /<button disabled=""[^>]*>/);
  assert.match(blocked, /active_jobs/);
});
test('stale and absent bootstrap observations remain unknown rather than invented current progress', () => {
  assert.equal(
    operatorBootstrap({ ...active, bootstrap: { ...active.bootstrap, observed_at: 800 } }, now)
      .freshness,
    'stale',
  );
  const stale = render({ ...active, bootstrap: { ...active.bootstrap, observed_at: 800 } });
  assert.match(stale, /观测已过期/);
  assert.match(stale, /当前进度待核对/);
  assert.equal(operatorNodeStatus({ ...active, stale: true }, now).label, '状态待核对');
  assert.equal(operatorNodeStatus({ ...active, observed_at: 1001 }, now).label, '状态待核对');
  const absent = render({ ...active, bootstrap: null });
  assert.match(absent, /观测时间未知/);
  assert.match(absent, /尚未收到启动阶段上报/);
  assert.equal(
    operatorBootstrap({ ...active, bootstrap: { observed_at: 1001, slots: [] } }, now).freshness,
    'unknown',
  );
});
test('capacity summary excludes historical ready slots and counts only observed live readiness', () => {
  const ready = {
      ...active,
      id: 'ready',
      state: 'ready',
      desired_state: 'running',
      runtime_state: 'ready',
      hard_deadline: 1400,
      provider_safe_deadline: 1500,
      provider_lifetime_state: 'verified',
      provider_lifetime_observed_at: 999,
      slots: [
        { id: 's', state: 'ready', stale: false },
        { id: 'expired', state: 'ready', stale: true },
      ],
    },
    unknown = { ...ready, id: 'unknown', observed_at: 800 };
  const snapshot = {
    observed_at: 999,
    controller: { state: 'running', last_heartbeat_at: 999, stale: false },
    nodes: [active, ended, ready, unknown],
  };
  const summary = operatorCapacitySummary(snapshot, now);
  assert.deepEqual(summary, {
    stale: false,
    ready_slots: 1,
    starting_nodes: 1,
    unknown_nodes: 1,
    pending_review: 0,
  });
  assert.equal(operatorNodeStatus({ ...ready, slots: [] }, now).label, '等待执行槽就绪');
  for (const patch of [
    { desired_state: 'draining' },
    { desired_state: 'drained' },
    { desired_state: 'stopped' },
    { state: 'destroying' },
    { state: 'destroy_unknown' },
    { runtime_state: 'blocked' },
    { runtime_state: 'failed' },
    { record_group: 'pending_review' },
    { hard_deadline: 1000 },
    { hard_deadline: 1300 },
    { provider_safe_deadline: 1000 },
    { provider_safe_deadline: null },
    { provider_safe_deadline: 1399 },
    { provider_lifetime_state: 'unverified' },
    { provider_lifetime_observed_at: 969 },
    { provider_lifetime_observed_at: 1001 },
    { reason_code: 'runtime_process_exited' },
  ]) {
    assert.equal(
      operatorCapacitySummary({ ...snapshot, nodes: [{ ...ready, ...patch }] }, now).ready_slots,
      0,
      JSON.stringify(patch),
    );
  }
  for (const controller of [
    { state: 'offline', last_heartbeat_at: 999 },
    { state: 'running', last_heartbeat_at: 800 },
    { state: 'running', last_heartbeat_at: 969 },
    { state: 'running', last_heartbeat_at: 1001 },
    { state: 'running', last_heartbeat_at: 999, stale: true },
    null,
  ]) {
    assert.equal(
      operatorCapacitySummary({ ...snapshot, controller, nodes: [ready] }, now).ready_slots,
      0,
    );
  }
  assert.equal(
    operatorCapacitySummary({ ...snapshot, observed_at: 800, nodes: [ready] }, now).ready_slots,
    0,
  );
  assert.equal(
    operatorCapacitySummary({ ...snapshot, nodes: [{ ...ready, runtime_state: 'busy' }] }, now)
      .ready_slots,
    1,
  );
  assert.equal(
    operatorCapacitySummary(
      { ...snapshot, nodes: [{ ...ready, state: 'busy', runtime_state: 'busy' }] },
      now,
    ).ready_slots,
    1,
  );
  assert.equal(
    operatorCapacitySummary({ ...snapshot, nodes: [{ ...ready, state: 'starting' }] }, now)
      .ready_slots,
    1,
  );
  const notAdmitted = {
    ...ready,
    slots: [
      {
        id: 's',
        state: 'ready',
        stale: false,
        admission_allowed: false,
        admission_reason_code: 'job_exceeds_worker_window',
      },
    ],
  };
  assert.equal(operatorCapacitySummary({ ...snapshot, nodes: [notAdmitted] }, now).ready_slots, 0);
  assert.equal(operatorNodeStatus(notAdmitted, now).label, '暂不接单');
  assert.equal(operatorNodeStatus({ ...ready, hard_deadline: 1300 }, now).label, '暂不接单');
  assert.equal(operatorNodeStatus({ ...ready, hard_deadline: 1301 }, now).label, '可接单');
  const rejectedMarkup = render(notAdmitted);
  assert.match(rejectedMarkup, /暂不接单/);
  assert.match(rejectedMarkup, /job_exceeds_worker_window/);
  assert.doesNotMatch(rejectedMarkup, /<span class="op-badge good"><i><\/i>可接单/);
});
test('compact rows expose a detail action instead of full slot history', () => {
  const html = renderToStaticMarkup(
    React.createElement(MachineRow, {
      node: ended,
      profiles: [],
      now,
      onOpen: () => {},
      onExtend: () => {},
    }),
  );
  assert.match(html, /查看 Lium RTX 5090 old-node 详情/);
  assert.match(html, /原期限/);
  assert.doesNotMatch(html, /old-job|排空后关机|延长运行/);
  assert.equal(uniqueOperatorReasons(['a', { code: 'a' }, 'b', { code: 'b' }, null]).length, 2);
});
test('manual mark is available directly for server-eligible records without a prior platform stop', () => {
  for (const provider of ['lium', 'targon']) {
    const node = {
      ...active,
      provider,
      provider_instance_id: null,
      state: 'creation_unknown',
      runtime_state: 'creation_unknown',
      actions: { manual_review: { allowed: true } },
    };
    const row = (props) =>
      renderToStaticMarkup(
        React.createElement(MachineRow, {
          node,
          profiles: [],
          now,
          onOpen: () => {},
          onReview: () => {},
          ...props,
        }),
      );
    assert.match(row(), />标记已关机<\/button>/);
    assert.doesNotMatch(row(), /<button[^>]*disabled=/);
    assert.match(row({ disabled: true }), /<button[^>]*disabled=""[^>]*>.*标记已关机/);
    assert.match(render(node), />标记已关机<\/button>/);
    const blocked = {
      ...node,
      actions: {
        manual_review: { allowed: false, blockers: ['operator_manual_review_worker_active'] },
      },
    };
    assert.doesNotMatch(render(blocked), />标记已关机<\/button>/);
    assert.match(render(blocked), /operator_manual_review_worker_active/);
    assert.doesNotMatch(row({ node: blocked }), />标记已关机<\/button>/);
  }
});
test('candidate rendering enables only usable fresh stock and preserves reasons on stale, sold-out or blocked offers', () => {
  const candidate = {
    provider: 'lium',
    offer_id: 'host-one',
    gpu_type: 'RTX 5090',
    gpu_count: 1,
    available_count: 1,
    deployment_qualified: true,
    execution_slots: 1,
    blockers: [],
    hourly_cost_microusd: 700000,
  };
  const renderCandidate = (row, current = true, busy = false) =>
    renderToStaticMarkup(
      React.createElement(CandidateCard, { row, index: 0, current, busy, onStart: () => {} }),
    );
  const fresh = renderCandidate(candidate);
  assert.doesNotMatch(fresh, /<button[^>]* disabled=/);
  assert.match(fresh, /点击后核对费用并开机/);
  const stale = renderCandidate(candidate, false);
  assert.match(stale, /库存待刷新/);
  assert.match(stale, /<button[^>]*disabled=""/);
  const soldOut = renderCandidate({ ...candidate, available_count: 0 });
  assert.match(soldOut, /<button[^>]*disabled=""/);
  assert.match(soldOut, /暂无库存/);
  assert.match(
    renderCandidate({ ...candidate, deployment_qualified: false }),
    /<button[^>]*disabled=""/,
  );
  const blocked = renderCandidate({ ...candidate, blockers: ['operator_pool_paused'] });
  assert.match(blocked, /operator_pool_paused/);
  assert.match(blocked, /<button[^>]*disabled=""/);
});
test('extension confirmation needs exact version/deadline/duration, fresh consent and authoritative eligibility', () => {
  const preview = {
    node_id: active.id,
    node_version: active.version,
    can_extend: true,
    current_deadline: 1300,
    new_deadline: 1900,
    additional_seconds: 600,
    expires_at: 1005,
  };
  assert.equal(operatorExtensionCurrent(active, preview, 600, now), true);
  for (const patch of [
    { node_version: 'changed' },
    { node_id: 'other' },
    { current_deadline: 1299 },
    { new_deadline: 2000 },
    { additional_seconds: 1200 },
    { expires_at: 999 },
    { can_extend: false },
  ])
    assert.equal(operatorExtensionCurrent(active, { ...preview, ...patch }, 600, now), false);
  for (const patch of [
    { stale: true },
    { state: 'destroyed' },
    { actions: { extend: { allowed: false } } },
  ])
    assert.equal(operatorExtensionCurrent({ ...active, ...patch }, preview, 600, now), false);
});
test('extension success requires completed durable metadata and refreshed exact node deadline', () => {
  const preview = {
      node_id: active.id,
      current_deadline: 1300,
      new_deadline: 1900,
      additional_seconds: 600,
    },
    receipt = {
      operation: {
        state: 'completed',
        extension: { current_deadline: 1300, new_deadline: 1900, additional_seconds: 600 },
      },
    };
  assert.equal(
    operatorExtensionReadback({ ...active, hard_deadline: 1900 }, preview, receipt),
    true,
  );
  assert.equal(operatorExtensionReadback(active, preview, receipt), false);
  assert.equal(
    operatorExtensionReadback({ ...active, id: 'other', hard_deadline: 1900 }, preview, receipt),
    false,
  );
  assert.equal(
    operatorExtensionReadback({ ...active, hard_deadline: 1900 }, preview, {
      operation: { ...receipt.operation, state: 'accepted' },
    }),
    false,
  );
  for (const patch of [
    { current_deadline: 1200 },
    { new_deadline: 2000 },
    { additional_seconds: 1200 },
  ])
    assert.equal(
      operatorExtensionReadback({ ...active, hard_deadline: 1900 }, preview, {
        operation: { state: 'completed', extension: { ...receipt.operation.extension, ...patch } },
      }),
      false,
    );
});
