import test from 'node:test';
import assert from 'node:assert/strict';
import {
  alerts,
  duration,
  jobRows,
  machineRows,
  reasonKey,
  spendSummary,
  usd,
} from './admin-overview-model.js';

const NOW = 1_800_000_000;
const node = (extra) => ({
  node_id: 'n1',
  owner_id: 'supervan',
  gpu_type: 'RTX5090',
  gpu_count: 1,
  mode: 'ref',
  provider: 'vast',
  state: 'running',
  lifecycle_phase: 'loading_model',
  preparation: { started_at: NOW - 60 },
  ...extra,
});

test('raw codes map to plain reasons', () => {
  assert.equal(reasonKey('global_capacity_exceeded'), 'capacityFull');
  assert.equal(reasonKey('something_new'), 'other');
  assert.equal(reasonKey(null), null);
});

test('machine rows skip stopped nodes and flag problems first', () => {
  const rows = machineRows(
    {
      queue: [{ waiting: 1 }],
      nodes: [
        node({ node_id: 'ok' }),
        node({ node_id: 'gone', state: 'stopped' }),
        node({ node_id: 'refused', dispatch_reason_code: 'global_capacity_exceeded' }),
        node({ node_id: 'slow', preparation: { started_at: NOW - 21 * 60 } }),
        node({ node_id: 'idle', ready: true, observation_fresh: true }),
      ],
    },
    NOW,
  );
  assert.deepEqual(
    rows.map((row) => row.id),
    ['slow', 'refused', 'idle', 'ok'],
  );
  const kinds = Object.fromEntries(rows.map((row) => [row.id, row.issue?.kind ?? null]));
  assert.deepEqual(kinds, {
    slow: 'slowStart',
    refused: 'refused',
    idle: 'idleWithQueue',
    ok: null,
  });
  assert.equal(rows.find((row) => row.id === 'refused').issue.reason, 'capacityFull');
});

test('a ready machine with an empty queue is not an alert', () => {
  const [row] = machineRows(
    { queue: [], nodes: [node({ ready: true, observation_fresh: true })] },
    NOW,
  );
  assert.equal(row.issue, null);
});

test('job rows mark stuck and long waits', () => {
  const rows = jobRows(
    [
      {
        id: 'a',
        status: 'cancel_requested',
        diagnostic: { error_code: 'upstream_status_unknown', age_seconds: 9000 },
      },
      { id: 'b', status: 'queued', created_at: NOW - 31 * 60 },
      { id: 'c', status: 'queued', created_at: NOW - 60, cancellable: true },
    ],
    NOW,
  );
  assert.deepEqual(
    rows.map((row) => [row.id, row.stuck, row.waitingLong, row.cancellable]),
    [
      ['a', true, false, false],
      ['b', false, true, false],
      ['c', false, false, true],
    ],
  );
  assert.equal(rows[0].reason, 'unreachable');
});

test('spend is near the cap only when spend is near it', () => {
  const day = (extra) => ({
    tenant_daily_supplier_spending: {
      limit_microusd: 100e6,
      settled_microusd: 0,
      committed_microusd: 0,
      unresolved_count: 2,
      ...extra,
    },
  });
  assert.equal(spendSummary(day()).nearCap, false);
  assert.equal(spendSummary(day()).unconfirmed, 2);
  assert.equal(
    spendSummary(day({ settled_microusd: 70e6, committed_microusd: 10e6 })).nearCap,
    true,
  );
  assert.equal(spendSummary(day({ admission_blocked: true })).blocked, true);
  assert.equal(spendSummary({}), null);
});

test('alerts put bad ones first', () => {
  const machines = machineRows({ nodes: [node({ preparation: { started_at: NOW - 3600 } })] }, NOW);
  const jobs = jobRows(
    [{ id: 'x', status: 'running', diagnostic: { error_code: 'upstream_status_unknown' } }],
    NOW,
  );
  const list = alerts({ machines, jobs, spend: null, workspace: {} });
  assert.deepEqual(
    list.map((item) => item.kind),
    ['stuckJobs', 'slowStart'],
  );
});

test('formatting', () => {
  assert.equal(duration(45), '45s');
  assert.equal(duration(600), '10m');
  assert.equal(duration(7200), '2.0h');
  assert.equal(duration(null), '—');
  assert.equal(usd(850000), '$0.85');
  assert.equal(usd(null), '—');
});

test('records to review stay out of the live machines and hourly total (#572)', async () => {
  const { machineRows, reviewRows } = await import('./admin-overview-model.js');
  const workspace = {
    nodes: [
      { node_id: 'live', state: 'running', hourly_cost_microusd: 650000, gpu_type: 'RTX5090' },
      {
        node_id: 'old',
        state: 'creation_unknown',
        record_group: 'pending_review',
        hourly_cost_microusd: 850000,
        gpu_type: 'RTX5090',
        owner_id: 'supervan',
        provider: 'vast',
      },
    ],
  };
  const live = machineRows(workspace, 0, 'en');
  assert.deepEqual(
    live.map((row) => row.id),
    ['live'],
  );
  assert.deepEqual(
    reviewRows(workspace).map((row) => [row.id, row.owner]),
    [['old', 'supervan']],
  );
});
