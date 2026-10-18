import test from 'node:test';
import assert from 'node:assert/strict';
import {
  productionRows,
  filterProductionRows,
  selectedProductionRows,
  batchSummary,
  reviewProgress,
  shotReviewState,
} from './production-model.js';
import { shotSnapshot, stableJSON } from './cloud-model.js';
const entity = (id, type, parentId, order = 0, data = {}) => ({
  id,
  type,
  parentId,
  order,
  title: id,
  description: '画面',
  version: 1,
  status: 'draft',
  data,
});
function fixture() {
  return {
    id: 'p',
    links: [],
    entities: [
      entity('c2', 'chapter', null, 1),
      entity('c1', 'chapter', null, 0),
      entity('s2', 'scene', 'c2'),
      entity('s1', 'scene', 'c1'),
      entity('b', 'shot', 's2', 0, { seconds: 6 }),
      entity('a', 'shot', 's1', 0, { seconds: 6 }),
      entity('img', 'image', null, 2, { fileId: 'imagefile' }),
    ],
  };
}
const ready = (p, id, estimate) => ({
  plan_id: 'plan-' + id,
  status: 'ready',
  expires_at: 2000,
  sourceSnapshot: stableJSON(shotSnapshot(p, id)),
  estimate,
});
test('chapter and state filters preserve story order and never expand the selected set', () => {
  const p = fixture(),
    c = { plans: { a: ready(p, 'a') }, jobs: [] },
    rows = productionRows(p, c, 1000);
  assert.deepEqual(
    rows.map((r) => r.id),
    ['a', 'b'],
  );
  assert.deepEqual(
    filterProductionRows(rows, { chapterId: 'c2' }).map((r) => r.id),
    ['b'],
  );
  assert.deepEqual(
    filterProductionRows(rows, { status: 'ready' }).map((r) => r.id),
    ['a'],
  );
  assert.deepEqual(
    filterProductionRows(rows, { query: ' s1 ' }).map((r) => r.id),
    ['a'],
  );
  assert.equal(filterProductionRows(rows, { chapterId: 'missing' }).length, 0);
  assert.deepEqual(
    selectedProductionRows(rows, ['b', 'gone', 'a', 'a']).map((r) => r.id),
    ['a', 'b'],
  );
});
test('unknown and active tasks cannot be selected; stale, expired, failed and blocked are not submit-ready', () => {
  const p = fixture(),
    job = (status) => ({
      id: 'j',
      status,
      plan_id: 'plan-a',
      created_at: 10,
      client_ref: { project_id: 'p', shot_id: 'a' },
    });
  for (const [status, phase] of [
    ['running', 'active'],
    ['planned', 'active'],
    ['submission_unknown', 'unknown'],
    ['recovery_hold', 'recovery_hold'],
  ]) {
    const row = productionRows(p, { jobs: [job(status)] })[0];
    assert.equal(row.phase, phase);
    assert.equal(row.canSelect, false);
  }
  for (const [state, phase] of [
    [{ plans: { a: ready(p, 'a') } }, 'expired'],
    [{ plans: { a: { ...ready(p, 'a'), sourceSnapshot: 'old' } } }, 'stale'],
    [{ plans: { a: { ...ready(p, 'a'), status: 'blocked', expires_at: null } } }, 'blocked'],
    [{ jobs: [job('failed')] }, 'failed'],
    [
      { plans: { a: { ...ready(p, 'a'), expires_at: null } }, jobs: [job('succeeded')] },
      'submitted',
    ],
  ]) {
    const row = productionRows(p, state, 3000000)[0];
    assert.equal(row.phase, phase);
    assert.equal(row.canSubmit, false);
  }
  const current = productionRows(
    p,
    { plans: { a: ready(p, 'a') }, preflightErrors: { a: '输入未上传' } },
    1000,
  )[0];
  assert.equal(current.phase, 'error');
  assert.equal(current.canSubmit, false);
});
test('batch summary separates quote kinds and unknowns and invalidates explicit confirmation on changes', () => {
  const p = fixture(),
    c = {
      plans: {
        a: ready(p, 'a', { kind: 'simulation', cost_microusd: 0, currency: 'USD' }),
        b: ready(p, 'b', { kind: 'budget_reservation', cost_microusd: 500000, currency: 'USD' }),
      },
    },
    rows = productionRows(p, c, 1000),
    summary = batchSummary(rows);
  assert.equal(summary.ready, 2);
  assert.equal(summary.estimates.length, 2);
  assert.equal(summary.unknown, 0);
  assert.notEqual(batchSummary(rows.slice(0, 1)).signature, summary.signature);
  c.plans.b.estimate = null;
  const unknown = batchSummary(productionRows(p, c, 1000));
  assert.equal(unknown.unknown, 1);
  assert.notEqual(unknown.signature, summary.signature);
  p.entities.find((e) => e.id === 'b').description = '改稿';
  const stale = batchSummary(productionRows(p, c, 1000));
  assert.equal(stale.blocked, 1);
  assert.equal(stale.ready, 1);
});
test('review progress skips confirmed shots, wraps pending ones and never changes candidates', () => {
  const p = fixture(),
    a = p.entities.find((e) => e.id === 'a'),
    b = p.entities.find((e) => e.id === 'b'),
    shots = [a, b];
  a.data.selectedAssetId = 'img';
  b.data.candidateIds = ['img'];
  const before = structuredClone(p);
  assert.equal(shotReviewState(p, a), 'accepted');
  assert.equal(shotReviewState(p, b), 'unselected');
  assert.deepEqual(reviewProgress(p, shots, 'a'), { done: 1, total: 2, remaining: 1, next: 'b' });
  assert.equal(reviewProgress(p, shots, 'b').next, null);
  assert.deepEqual(p, before);
  a.status = 'review';
  assert.equal(reviewProgress(p, shots, 'b').next, 'a');
  b.data.uxReview = { chosen: 'A' };
  b.data.candidateIds = [];
  assert.equal(shotReviewState(p, b), 'missing');
  assert.equal(shotReviewState(p, b, { cloud: false }), 'accepted');
});

test('capacity waiting is an active original job, not a fresh submit or a completed inference', () => {
  const p = fixture(),
    job = {
      id: 'original',
      status: 'waiting_capacity',
      client_ref: { project_id: p.id, shot_id: 'a' },
    },
    rows = productionRows(p, { jobs: [job] });
  const row = rows.find((r) => r.id === 'a');
  assert.equal(row.phase, 'waiting_capacity');
  assert.equal(row.canSelect, false);
  assert.equal(row.canSubmit, false);
  assert.deepEqual(
    filterProductionRows(rows, { status: 'active' }).map((r) => r.id),
    ['a'],
  );
  const plan = { ...ready(p, 'a'), execution: { admission_state: 'waiting_capacity' } };
  const allowed = productionRows(p, { plans: { a: plan } }, 1000),
    summary = batchSummary(allowed.filter((r) => r.id === 'a'));
  assert.equal(summary.waitingCapacity, 1);
  const old = summary.signature;
  plan.execution.admission_state = 'ready';
  assert.notEqual(
    batchSummary(productionRows(p, { plans: { a: plan } }, 1000).filter((r) => r.id === 'a'))
      .signature,
    old,
  );
});
