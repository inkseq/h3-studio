import test from 'node:test';
import assert from 'node:assert/strict';
import {
  apiDate,
  creditSummary,
  entryList,
  formatPrice,
  insufficientCredits,
  packList,
  priceRates,
  waitForPurchase,
} from './credits.js';

test('credit summary needs a balance and derives available from held', () => {
  assert.equal(creditSummary(null), null);
  assert.equal(creditSummary({ balance: -1 }), null);
  assert.deepEqual(creditSummary({ balance: 500, held: 60 }), {
    balance: 500,
    held: 60,
    available: 440,
    todayUsed: 0,
    dailyLimit: 0,
  });
});

test('packs accept either shape and drop incomplete rows', () => {
  const packs = packList({
    packs: [
      { id: 'starter', credits: 500, amount_cents: 500 },
      { id: 'creator', credits: 2100, usd: 20, bonus_percent: 5, popular: true },
      { id: 'broken', credits: 0, amount_cents: 100 },
    ],
  });
  assert.deepEqual(
    packs.map((p) => [p.id, p.cents, p.bonus, p.popular]),
    [
      ['starter', 500, 0, false],
      ['creator', 2000, 5, true],
    ],
  );
  assert.equal(formatPrice(2000), 'US$20');
  assert.equal(formatPrice(499), 'US$4.99');
});

test('rates keep tier order and show a 5-second clip', () => {
  assert.deepEqual(priceRates({ rates: { refine: 12, fast: 2, standard: 4, other: 1 } }), [
    { tier: 'fast', rate: 2, perClip: 10 },
    { tier: 'standard', rate: 4, perClip: 20 },
    { tier: 'refine', rate: 12, perClip: 60 },
  ]);
});

test('only a well-formed 402 insufficient_credits opens the top-up prompt', () => {
  assert.deepEqual(insufficientCredits(402, { code: 'insufficient_credits', need: 80, have: 20 }), {
    need: 80,
    have: 20,
  });
  assert.equal(insufficientCredits(409, { code: 'insufficient_credits', need: 1, have: 0 }), null);
  assert.equal(insufficientCredits(402, { code: 'insufficient_credits' }), null);
});

test('entries show held moves for holds/releases and drop rows that change nothing', () => {
  const list = entryList({
    entries: [
      { kind: 'capture', amount: -21, held_delta: -21 },
      { kind: 'hold', amount: 0, held_delta: 21 },
      { kind: 'release', amount: 0, held_delta: -21 },
      { kind: 'hold', amount: 0, held_delta: 0 },
      { amount: 'x' },
    ],
    next_cursor: 'c2',
  });
  assert.deepEqual(
    list.entries.map((e) => [e.kind, e.shown, e.onHold]),
    [
      ['capture', -21, false],
      ['hold', -21, true],
      ['release', 21, true],
    ],
  );
  assert.equal(list.next, 'c2');
});

test('purchase polling stops when the balance grows, or gives up', async () => {
  const seen = [100, 100, 600];
  const read = async () => ({ balance: seen.shift() });
  assert.equal((await waitForPurchase(read, 100, { sleep: async () => {} })).balance, 600);
  assert.equal(
    await waitForPurchase(async () => ({ balance: 1 }), 1, { tries: 2, sleep: async () => {} }),
    null,
  );
});

test('API dates accept Unix seconds, milliseconds and ISO strings', () => {
  const iso = '2026-10-19T12:00:00.000Z';
  assert.equal(apiDate(1792411200).toISOString(), iso);
  assert.equal(apiDate(1792411200000).toISOString(), iso);
  assert.equal(apiDate(iso).toISOString(), iso);
  assert.equal(apiDate('nope'), null);
  assert.equal(apiDate(null), null);
});
