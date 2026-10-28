// Customer credits (h3-studio#306) and Stripe credit packs (#307). 1 credit = US$0.01.
// The UI stays hidden until GET /v1/credits answers 200, so it ships before the backend.
// Responses are read tolerantly; the shapes the UI expects are posted on #306/#307.

export const CREDITS_CHANGED = 'inkseq:credits-changed';
export const INSUFFICIENT_CREDITS = 'inkseq:insufficient-credits';
export const TIERS = ['fast', 'standard', 'refine'];

const count = (value) => (Number.isSafeInteger(value) && value >= 0 ? value : null);

export function creditSummary(body) {
  if (!body || typeof body !== 'object') return null;
  const balance = count(body.balance);
  if (balance === null) return null;
  const held = count(body.held) ?? 0;
  return {
    balance,
    held,
    available: count(body.available) ?? Math.max(0, balance - held),
    todayUsed: count(body.today_used) ?? 0,
    dailyLimit: count(body.daily_limit) ?? 0,
  };
}

export function packList(body) {
  const rows = Array.isArray(body) ? body : Array.isArray(body?.packs) ? body.packs : [];
  return rows
    .map((row) => ({
      id: String(row.id || ''),
      credits: count(row.credits),
      cents: count(row.amount_cents) ?? (count(row.usd) === null ? null : row.usd * 100),
      currency: String(row.currency || 'usd').toUpperCase(),
      bonus: count(row.bonus_percent) ?? count(row.bonus) ?? 0,
      popular: row.popular === true,
    }))
    .filter((row) => row.id && row.credits && row.cents);
}

export function priceRates(body) {
  const rates = body?.rates && typeof body.rates === 'object' ? body.rates : {};
  return TIERS.filter((tier) => Number(rates[tier]) > 0).map((tier) => ({
    tier,
    rate: Number(rates[tier]),
    perClip: Math.ceil(Number(rates[tier]) * 5),
  }));
}

export function entryList(body) {
  const rows = Array.isArray(body?.entries) ? body.entries : Array.isArray(body) ? body : [];
  return {
    // Holds and releases move credits in and out of "held" (held_delta) with amount 0.
    // Rows that change nothing (zero-credit CPU steps) are left out.
    entries: rows
      .filter((row) => Number.isSafeInteger(row?.amount))
      .map((row) => {
        const held = Number.isSafeInteger(row.held_delta) ? row.held_delta : 0;
        return {
          ...row,
          shown: row.amount !== 0 ? row.amount : -held,
          onHold: row.amount === 0 && held !== 0,
        };
      })
      .filter((row) => row.shown !== 0),
    next: typeof body?.next_cursor === 'string' ? body.next_cursor : null,
  };
}

// 402 insufficient_credits {need, have}: nothing was queued and nothing was charged.
export function insufficientCredits(status, detail) {
  if (status !== 402 || detail?.code !== 'insufficient_credits') return null;
  const need = count(detail.need),
    have = count(detail.have);
  return need === null || have === null ? null : { need, have };
}

// API times arrive as Unix seconds (the platform default), milliseconds or ISO strings.
// Inkseq receipts for paid top-ups (h3-studio#698). Provider receipt pages show personal
// contact details, so their links are never shown to customers.
export function receiptList(body) {
  return (Array.isArray(body?.payments) ? body.payments : [])
    .map((row) => row?.receipt)
    .filter(
      (receipt) =>
        receipt &&
        typeof receipt.payment_id === 'string' &&
        Number.isSafeInteger(receipt.amount_cents) &&
        Number.isSafeInteger(receipt.credits),
    )
    .map((receipt) => ({
      ...receipt,
      currency: String(receipt.currency || 'usd').toUpperCase(),
      refunded_cents: Number.isSafeInteger(receipt.refunded_cents) ? receipt.refunded_cents : 0,
      // Present only when Stripe added tax to a tax-exclusive pack (h3-studio#727).
      base_cents: Number.isSafeInteger(receipt.base_amount_cents)
        ? receipt.base_amount_cents
        : null,
      tax_cents: Number.isSafeInteger(receipt.tax_amount_cents) ? receipt.tax_amount_cents : 0,
    }));
}

export function apiDate(value) {
  if (typeof value === 'number' && Number.isFinite(value))
    return new Date(value < 1e11 ? value * 1000 : value);
  if (typeof value === 'string' && value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  return null;
}

export function formatCredits(value, locale = 'en') {
  return Number.isSafeInteger(value)
    ? new Intl.NumberFormat(locale === 'zh' ? 'zh-CN' : 'en-US').format(value)
    : '—';
}

export function formatPrice(cents, currency = 'USD') {
  const whole = cents % 100 === 0;
  return (currency === 'USD' ? 'US$' : currency + ' ') + (cents / 100).toFixed(whole ? 0 : 2);
}

// Polls after a Stripe redirect until the purchase entry lands; Stripe fulfils by webhook only.
export async function waitForPurchase(read, before, { tries = 15, delay = 2000, sleep } = {}) {
  const pause = sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  for (let attempt = 0; attempt < tries; attempt++) {
    const now = await read().catch(() => null);
    if (now && (before === null || now.balance > before)) return now;
    await pause(delay);
  }
  return null;
}
