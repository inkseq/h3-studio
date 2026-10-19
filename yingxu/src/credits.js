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
    entries: rows.filter((row) => Number.isSafeInteger(row?.amount)),
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
