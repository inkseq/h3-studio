import test from 'node:test';
import assert from 'node:assert/strict';
import {
  dollars,
  ledgerSpend,
  revenueCells,
  revenueDetail,
  testOrders,
} from './admin-money-model.js';

test('ledger spend shows settled and open, or nothing for untracked providers', () => {
  assert.deepEqual(
    ledgerSpend({ ledger: { settled_microusd: 1_250_000, open_microusd: 500_000 } }),
    { settled: '$1.25', open: '$0.50' },
  );
  assert.deepEqual(ledgerSpend({ ledger: { settled_microusd: 0, open_microusd: 0 } }), {
    settled: '$0.00',
    open: null,
  });
  assert.equal(ledgerSpend({ ledger: null }), null);
  assert.equal(ledgerSpend({}), null);
});

test('revenue cells use live orders only and keep their shape before data', () => {
  const body = {
    live: {
      gross_cents: 12345,
      net_estimate_cents: -150,
      orders: 3,
      paying_users: 2,
      first_time_payers: 1,
    },
    test: { orders: 9, gross_cents: 99900 },
    credits_outstanding: 4200,
  };
  assert.deepEqual(revenueCells(body), {
    gross: '$123.45',
    net: '-$1.50',
    orders: 3,
    payers: 2,
    firstTime: 1,
    outstanding: 4200,
  });
  assert.deepEqual(Object.keys(revenueCells(undefined)), Object.keys(revenueCells(body)));
  assert.equal(revenueCells(undefined).gross, '—');
  assert.deepEqual(testOrders(body), { orders: 9, gross: '$999.00' });
  assert.equal(testOrders({ test: { orders: 0 } }), null);
  assert.equal(testOrders({ test: null }), null);
});

test('revenue detail and money formatting', () => {
  assert.deepEqual(
    revenueDetail({
      live: { refunded_cents: 500, disputed_count: 1, credits_sold: 1000 },
      credits_used: 300,
    }),
    { refunded: '$5.00', disputes: 1, sold: 1000, used: 300 },
  );
  assert.equal(revenueDetail({ live: null }), null);
  assert.equal(dollars(null), '—');
});
