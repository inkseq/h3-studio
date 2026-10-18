import test from 'node:test';
import assert from 'node:assert/strict';
import {
  spendingBreakdown,
  spendingChargeBreakdown,
  admissionExposure,
  spendingReconciliation,
} from './spending-breakdown.js';

const bucket = (overrides) => ({
  settled_today_microusd: 0,
  reserved_microusd: 0,
  awaiting_invoice_estimate_microusd: 0,
  awaiting_invoice_count: 0,
  prior_day_pending_estimate_microusd: 0,
  legacy_settled_unknown_date_microusd: 0,
  exposure_microusd: 0,
  unconfirmed_exposure_microusd: 0,
  unconfirmed_reference_count: 0,
  ...overrides,
});
const usage = (scope, overrides = {}) => ({
  day: '2026-10-19',
  timezone: 'Australia/Sydney',
  projected_microusd: 195_000_000,
  breakdown: {
    schema_version: 1,
    attribution_scope: scope,
    ...bucket(),
    by_kind: { job: bucket(), instance: bucket() },
    unattributed_exposure_microusd: 50_000_000,
    unattributed_actor_exposure_microusd: 40_000_000,
    ...overrides,
  },
});

test('two connection-origin fixtures retain their own server totals without borrowing admission or account unknown amounts', () => {
  const first = spendingBreakdown(
    usage('connection', {
      settled_today_microusd: 100_000,
      reserved_microusd: 200_000,
      awaiting_invoice_estimate_microusd: 300_000,
      exposure_microusd: 600_000,
    }),
    'connection',
  );
  const second = spendingBreakdown(
    usage('connection', { reserved_microusd: 700_000, exposure_microusd: 700_000 }),
    'connection',
  );
  assert.equal(first.totals.exposure_microusd, 600_000);
  assert.equal(second.totals.exposure_microusd, 700_000);
  assert.equal(first.unattributedShared, undefined);
  assert.equal(first.unattributedActor, undefined);
  assert.equal(first.totals.projected_microusd, undefined);
  const account = spendingBreakdown(
    usage('account', {
      exposure_microusd: 1_300_000,
      unattributed_actor_exposure_microusd: 400_000,
    }),
    'account',
  );
  assert.equal(account.totals.exposure_microusd, 1_300_000);
  assert.equal(account.unattributedActor, 400_000);
});

test('prior-day invoice estimates and unknown settlement dates remain separate from the server Sydney-day confirmed amount', () => {
  const old = usage('account', {
    settled_today_microusd: 2_340_000,
    awaiting_invoice_estimate_microusd: 82_090_000,
    awaiting_invoice_count: 97,
    prior_day_pending_estimate_microusd: 82_090_000,
    legacy_settled_unknown_date_microusd: 500_000,
  });
  const current = spendingBreakdown(old, 'account');
  assert.equal(current.day, '2026-10-19');
  assert.equal(current.totals.settled_today_microusd, 2_340_000);
  assert.equal(current.totals.prior_day_pending_estimate_microusd, 82_090_000);
  old.day = '2026-10-20';
  old.breakdown.settled_today_microusd = 0;
  const next = spendingBreakdown(old, 'account');
  assert.equal(next.totals.settled_today_microusd, 0);
  assert.equal(next.totals.awaiting_invoice_estimate_microusd, 82_090_000);
  assert.equal(next.totals.legacy_settled_unknown_date_microusd, 500_000);
});

test('legacy aggregate-only, wrong-owner scope and incomplete contract are unavailable rather than fabricated zero or projected spend', () => {
  assert.equal(spendingBreakdown({ projected_microusd: 198_620_000 }, 'account'), null);
  assert.equal(spendingBreakdown(usage('account'), 'connection'), null);
  const missing = usage('connection');
  delete missing.breakdown.by_kind.job.awaiting_invoice_count;
  assert.equal(spendingBreakdown(missing, 'connection'), null);
  for (const invalid of [-1, NaN, Infinity, true, '12', Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(
      spendingBreakdown(usage('account', { settled_today_microusd: invalid }), 'account'),
      null,
    );
  }
});

test('unconfirmed exposure is neither folded into confirmed amounts nor inferred by adding the three visible buckets', () => {
  const view = spendingBreakdown(
    usage('account', {
      settled_today_microusd: 1_000_000,
      exposure_microusd: 6_000_000,
      unconfirmed_exposure_microusd: 5_000_000,
      unconfirmed_reference_count: 2,
    }),
    'account',
  );
  assert.equal(view.totals.settled_today_microusd, 1_000_000);
  assert.equal(view.totals.exposure_microusd, 6_000_000);
  assert.equal(view.totals.unconfirmed_reference_count, 2);
  assert.equal(view.totals.reserved_microusd, 0);
});

test('zero is available only from a complete contract and cap exposure remains a separate conservative server metric', () => {
  const raw = usage('connection');
  assert.equal(spendingBreakdown(raw, 'connection').totals.exposure_microusd, 0);
  assert.deepEqual(admissionExposure(raw, 20_000_000), {
    amount: 195_000_000,
    limit: 20_000_000,
    percent: 100,
  });
  assert.deepEqual(admissionExposure(raw, 0), { amount: 195_000_000, limit: 0, percent: null });
  assert.equal(admissionExposure({ projected_microusd: null }, 20_000_000), null);
  assert.equal(admissionExposure(raw, -1), null);
});

test('net user charges are independent from retained costs and admission, and never borrow another assistant attribution', () => {
  const raw = usage('connection', {
    settled_today_microusd: 4_000_000,
    exposure_microusd: 8_000_000,
  });
  raw.charge_breakdown = {
    ...structuredClone(raw.breakdown),
    ...bucket(),
    waived_reference_count: 97,
    waived_exposure_microusd: 8_000_000,
  };
  assert.equal(spendingChargeBreakdown(raw, 'connection').totals.exposure_microusd, 0);
  assert.equal(spendingBreakdown(raw, 'connection').totals.exposure_microusd, 8_000_000);
  assert.equal(admissionExposure(raw, 20_000_000).amount, 195_000_000);
  assert.equal(spendingChargeBreakdown(raw, 'account'), null);
  delete raw.charge_breakdown.by_kind.instance.reserved_microusd;
  assert.equal(spendingChargeBreakdown(raw, 'connection'), null);
  delete raw.charge_breakdown;
  assert.equal(spendingChargeBreakdown(raw, 'connection'), null);
});

test('provider observation is optional, explicit and never turned into final or settled money', () => {
  assert.equal(spendingReconciliation(usage('account')), null);
  const raw = {
    reconciliation: {
      job: { state: 'allocation_required', reason_code: 'rental_cost_not_per_job_invoice' },
      instance: {
        state: 'current',
        pending_count: 2,
        checked_count: 1,
        observed_at: 1792306800.25,
        reported_cost_microusd: 600_000,
        final_cost_confirmed: false,
        reason_code: 'provider_estimate_only',
      },
    },
  };
  assert.deepEqual(spendingReconciliation(raw), {
    state: 'current',
    pending: 2,
    checked: 1,
    observedAt: 1792306800.25,
    reportedCost: 600_000,
  });
  raw.reconciliation.instance.final_cost_confirmed = true;
  assert.equal(spendingReconciliation(raw), null);
});

test('unavailable checks retain nullable observation amounts; absent or malformed observations cannot imply a successful check', () => {
  const raw = {
    reconciliation: {
      instance: {
        state: 'unavailable',
        pending_count: 2,
        checked_count: 0,
        observed_at: null,
        reported_cost_microusd: null,
        final_cost_confirmed: false,
      },
    },
  };
  assert.deepEqual(spendingReconciliation(raw), {
    state: 'unavailable',
    pending: 2,
    checked: 0,
    observedAt: null,
    reportedCost: null,
  });
  delete raw.reconciliation.instance.observed_at;
  assert.equal(spendingReconciliation(raw), null);
});
