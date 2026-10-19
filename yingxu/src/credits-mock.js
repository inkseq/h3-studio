// Dev-only stand-in for the #306/#307 endpoints (?mockCredits). Never used in production builds.
const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const day = 86400000;

export async function mockCreditsFetch(url) {
  const path = String(url).split('?')[0];
  if (path === '/v1/credits')
    return json({ balance: 1340, held: 40, available: 1300, today_used: 260, daily_limit: 2000 });
  if (path === '/v1/billing/packs')
    return json({
      packs: [
        { id: 'starter', credits: 500, amount_cents: 500 },
        { id: 'creator', credits: 2100, amount_cents: 2000, bonus_percent: 5, popular: true },
        { id: 'studio', credits: 5500, amount_cents: 5000, bonus_percent: 10 },
        { id: 'pro', credits: 11500, amount_cents: 10000, bonus_percent: 15 },
      ],
    });
  if (path === '/v1/credits/prices')
    return json({ revision: 1, rates: { fast: 2, standard: 4, refine: 12 } });
  if (path === '/v1/credits/entries')
    return json({
      entries: [
        { id: 'e4', kind: 'capture', amount: -40, created_at: Date.now() - 3600000 },
        { id: 'e3', kind: 'release', amount: 20, created_at: Date.now() - day },
        {
          id: 'e2',
          kind: 'purchase',
          amount: 2100,
          created_at: Date.now() - 2 * day,
          receipt_url: 'https://example.com/receipt',
        },
        {
          id: 'e1',
          kind: 'grant',
          amount: 200,
          reason: 'Welcome credits',
          created_at: Date.now() - 3 * day,
        },
      ],
      next_cursor: null,
    });
  if (path === '/v1/billing/checkout') return json({ detail: 'Mock checkout' }, 503);
  return json({ detail: 'Not found' }, 404);
}
