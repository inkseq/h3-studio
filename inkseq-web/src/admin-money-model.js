// Presentation for the admin money views (h3-studio#966, #485). The server owns every number;
// this only formats them. Live orders are revenue; Stripe test-mode orders never are.
export const REVENUE_DAYS = [7, 30, 90];

const finite = (value) => typeof value === 'number' && Number.isFinite(value);

function money(value, scale) {
  if (!finite(value)) return '—';
  const text = '$' + (Math.abs(value) / scale).toFixed(2);
  return value < 0 ? '-' + text : text;
}

export const dollars = (cents) => money(cents, 100);
export const microDollars = (value) => money(value, 1000000);

// What our own ledger recorded for one provider row, or null when our records do not cover it.
export function ledgerSpend(row) {
  const ledger = row?.ledger;
  if (!ledger || !finite(ledger.settled_microusd) || !finite(ledger.open_microusd)) return null;
  return {
    settled: microDollars(ledger.settled_microusd),
    open: ledger.open_microusd > 0 ? microDollars(ledger.open_microusd) : null,
  };
}

// The five numbers on the revenue card, always all five so the card never changes shape.
export function revenueCells(body) {
  const live = body?.live;
  const count = (value) => (finite(value) ? value : null);
  return {
    gross: dollars(live?.gross_cents),
    net: dollars(live?.net_estimate_cents),
    orders: count(live?.orders),
    payers: count(live?.paying_users),
    firstTime: count(live?.first_time_payers),
    outstanding: count(body?.credits_outstanding),
  };
}

// The smaller line under the numbers: refunds, disputes and credits for live orders.
export function revenueDetail(body) {
  const live = body?.live;
  if (!live) return null;
  return {
    refunded: dollars(live.refunded_cents),
    disputes: finite(live.disputed_count) ? live.disputed_count : 0,
    sold: finite(live.credits_sold) ? live.credits_sold : 0,
    used: finite(body.credits_used) ? body.credits_used : 0,
  };
}

// Test-mode orders in the period, shown apart so staging never reads as revenue.
export function testOrders(body) {
  const test = body?.test;
  if (!test || !finite(test.orders) || test.orders <= 0) return null;
  return { orders: test.orders, gross: dollars(test.gross_cents) };
}
