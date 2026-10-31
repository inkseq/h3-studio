import test from 'node:test';
import assert from 'node:assert/strict';
import { createQuickChatController } from './quick-chat-controller.js';
import { createQuickChatClient } from './quick-chat-client.js';
import { checkedCardPage } from './quick-chat-history.js';

function fixture(count = 66) {
  const calls = [],
    stored = new Map(),
    cards = Array.from({ length: count }, (_, i) => ({
      id: 'card-' + String(i + 1).padStart(3, '0'),
      created_at: 1000 + i,
      version: 1,
      current_revision_id: 'revision-' + String(i + 1).padStart(3, '0'),
      submission_ids: [],
    }));
  let account = 'owner',
    forward = [],
    failRevision = null,
    failCards = null,
    holdCard = null,
    releaseCard,
    version = 1;
  const api = {
    setAccount: (value) => (account = value),
    reset() {},
    schema: async () => ({ models: [] }),
    capabilities: async () => ({ recipes: [] }),
    generationAvailability: async () => null,
    me: async () => ({ username: account }),
    sessions: async () => ({ sessions: [] }),
    session: async (id) => ({ session: { id, version, next_settings: {} } }),
    materials: async () => ({ bindings: [] }),
    timeline: async (id, options = {}) => {
      calls.push(['timeline', id, { ...options }]);
      return options.after_cursor
        ? { events: structuredClone(forward), after_cursor: 'forward' }
        : {
            events: cards.slice(-30).map((card, i) => ({
              id: 'event-' + card.id,
              seq: 100 + i,
              type: 'preflight.completed',
              record: { id: 'preflight-' + card.id, revision_id: card.current_revision_id },
            })),
            next_cursor: 'event-older',
            after_cursor: 'forward',
          };
    },
    cards: async (id, { before_cursor, limit = 10 } = {}) => {
      calls.push(['cards', id, { before_cursor, limit }]);
      if (failCards) throw failCards;
      const filtered = cards.filter((card) => !before_cursor || card.id < before_cursor.slice(7)),
        page = filtered.slice(-limit);
      return {
        session_id: id,
        cards: structuredClone(page),
        limit,
        has_more: filtered.length > page.length,
        next_cursor: filtered.length > page.length ? 'before:' + page[0].id : null,
        start_reached: filtered.length === page.length,
      };
    },
    card: async (id, cardId) => {
      calls.push(['card', id, cardId]);
      if (cardId === holdCard) await new Promise((resolve) => (releaseCard = resolve));
      const card = cards.find((card) => card.id === cardId);
      if (!card) throw Error('Missing card');
      const { submission_ids, ...value } = card;
      return { ...value, session_id: id };
    },
    revision: async (id, revisionId) => {
      calls.push(['revision', id, revisionId]);
      if (revisionId === failRevision)
        throw Object.assign(Error('private upstream failure'), { status: 503 });
      return {
        id: revisionId,
        card_id: revisionId.replace('revision-', 'card-'),
        version: 1,
        prompt: 'Original prompt',
        recipe_id: 'h3-fl',
        controls: {},
        inputs: {},
        copies: 1,
      };
    },
    submission: async (id, sid) => {
      calls.push(['submission', id, sid]);
      return {
        id: sid,
        revision_id: sid.replace('submission-', 'revision-'),
        status: 'succeeded',
        items: [],
      };
    },
    getTurn: async (id, tid) => ({
      id: tid,
      text: 'Original turn',
      created_at: 999,
      card_id: 'card-060',
      status: 'completed',
    }),
    patchSession: async (id, body) => {
      version++;
      return { session: { id, version, ...body } };
    },
  };
  const storage = {
    getItem: (key) => stored.get(key) || null,
    setItem: (key, value) => stored.set(key, value),
    removeItem: (key) => stored.delete(key),
  };
  const controller = createQuickChatController({ client: api, storage });
  return {
    api,
    controller,
    cards,
    calls,
    stored,
    failRevision: (value) => (failRevision = value),
    failCards: (value) => (failCards = value),
    holdCard: (value) => (holdCard = value),
    releaseCard: () => releaseCard(),
    forward: (value) => (forward = value),
    async start() {
      await controller.setAccount('owner');
      await controller.open('session');
      return this;
    },
  };
}

test('card client uses the owner-fenced card endpoint and never substitutes a timeline cursor', async () => {
  const calls = [],
    client = createQuickChatClient({
      fetcher: async (path, options) => {
        calls.push({ path, options });
        return new Response('{}');
      },
    });
  client.setAccount('owner');
  await client.cards('session', { before_cursor: 'opaque+card/cursor', limit: 10 });
  assert.equal(
    calls[0].path,
    '/v1/quick-chat/sessions/session/cards?limit=10&before_cursor=opaque%2Bcard%2Fcursor',
  );
  assert.equal(calls[0].options.headers['X-Expected-Account'], 'owner');
  assert.equal(calls[0].options.credentials, 'same-origin');
});

test('failed card/revision hydration commits neither partial cards nor the next cursor, then retry adds the full page', async () => {
  const h = await fixture().start(),
    before = h.controller.getState(),
    cardReads = h.calls.filter(([kind]) => kind === 'cards').length;
  h.failRevision('revision-050');
  await assert.rejects(h.controller.history());
  const failed = h.controller.getState();
  assert.deepEqual(failed.cards, before.cards);
  assert.deepEqual(failed.revisions, before.revisions);
  assert.equal(failed.cardHistoryCursor, before.cardHistoryCursor);
  assert.equal(failed.cardHistoryHasMore, true);
  assert.equal(failed.cardHistoryLoading, false);
  assert.equal(failed.cardHistoryError.key, 'quickHistory.failed');
  assert.equal(failed.cardHistoryError.status, '503');
  h.failRevision(null);
  const result = await h.controller.history();
  assert.equal(result.added, 10);
  assert.equal(Object.keys(h.controller.getState().cards).length, 20);
  assert.equal(h.controller.getState().cardHistoryError, null);
  assert.equal(h.calls.filter(([kind]) => kind === 'cards').length, cardReads + 2);
  h.controller.destroy();
});

test('overlap from a deep link is skipped so the successful earlier-card click still adds ten cards', async () => {
  const h = fixture();
  await h.controller.setAccount('owner');
  await h.controller.open('session', { cardId: 'card-050' });
  assert.equal(Object.keys(h.controller.getState().cards).length, 11);
  const result = await h.controller.history();
  assert.equal(result.added, 10);
  assert.equal(Object.keys(h.controller.getState().cards).length, 21);
  assert.deepEqual(
    h.calls
      .filter(([kind]) => kind === 'cards')
      .slice(-2)
      .map((call) => call[2].limit),
    [10, 1],
  );
  h.controller.destroy();
});

test('initial card API failure stays unknown with a visible retry rather than claiming the beginning', async () => {
  const h = fixture();
  h.failCards(Object.assign(Error('not available'), { status: 503 }));
  await h.start();
  assert.equal(h.controller.getState().cardHistoryLoaded, false);
  assert.equal(h.controller.getState().cardHistoryHasMore, false);
  assert.equal(h.controller.getState().cardHistoryError.status, '503');
  h.failCards(null);
  await h.controller.history();
  assert.equal(h.controller.getState().cardHistoryLoaded, true);
  assert.equal(h.controller.getState().cardHistoryError, null);
  h.controller.destroy();
});

test('an event poll preserves the independent card cursor and cannot inject an old preflight card', async () => {
  const h = await fixture().start();
  await h.controller.history();
  const before = h.controller.getState(),
    cardReads = h.calls.filter(([kind]) => kind === 'card').length;
  h.forward([
    {
      id: 'new-pref-event',
      seq: 300,
      type: 'preflight.completed',
      record: { id: 'old-preflight', revision_id: 'revision-005' },
    },
  ]);
  await h.controller.poll();
  const after = h.controller.getState();
  assert.equal(after.cardHistoryCursor, before.cardHistoryCursor);
  assert.equal(after.historyCursor, before.historyCursor);
  assert.equal(after.forwardCursor, 'forward');
  assert.equal(after.cards['card-005'], undefined);
  assert.equal(h.calls.filter(([kind]) => kind === 'card').length, cardReads);
  assert.equal(Object.keys(after.cards).length, 20);
  h.controller.destroy();
});

test('session and account changes fence late history and its error from the next conversation', async () => {
  for (const change of ['session', 'account']) {
    const h = await fixture().start();
    h.holdCard('card-050');
    const pending = h.controller.history();
    while (!h.calls.some(([kind, , id]) => kind === 'card' && id === 'card-050'))
      await Promise.resolve();
    if (change === 'session') await h.controller.open('other-session');
    else await h.controller.setAccount('other-owner');
    const next = h.controller.getState();
    h.releaseCard();
    await assert.rejects(pending);
    assert.equal(h.controller.getState().session?.id, next.session?.id);
    assert.equal(h.controller.getState().account, next.account);
    assert.deepEqual(h.controller.getState().cards, next.cards);
    assert.equal(h.controller.getState().cardHistoryError, null);
    assert.equal(h.controller.getState().cardHistoryLoading, false);
    h.controller.destroy();
  }
});

test('an authored session update invalidates a pending history read without acknowledging its older cursor', async () => {
  const h = await fixture().start(),
    before = h.controller.getState();
  h.holdCard('card-050');
  const pending = h.controller.history();
  while (!h.calls.some(([kind, , id]) => kind === 'card' && id === 'card-050'))
    await Promise.resolve();
  await h.controller.patch({ title: 'New author title' });
  h.releaseCard();
  await assert.rejects(pending);
  assert.equal(h.controller.getState().session.title, 'New author title');
  assert.deepEqual(h.controller.getState().cards, before.cards);
  assert.equal(h.controller.getState().cardHistoryCursor, before.cardHistoryCursor);
  assert.equal(h.controller.getState().cardHistoryLoading, false);
  h.controller.destroy();
});

test('conversation-text history skips event-only pages without changing the card window or cursor', async () => {
  const h = await fixture().start(),
    before = h.controller.getState(),
    cursors = [];
  h.api.timeline = async (_id, { before_cursor }) => {
    cursors.push(before_cursor);
    return before_cursor === 'event-older'
      ? {
          events: [{ id: 'noise', seq: 2, type: 'preflight.completed', record: { id: 'pref' } }],
          next_cursor: 'older-text',
        }
      : {
          events: [
            {
              id: 'text',
              seq: 1,
              type: 'turn.created',
              created_at: 900,
              record: { id: 'older-turn', text: 'Original earlier chat', created_at: 900 },
            },
          ],
          next_cursor: null,
        };
  };
  await h.controller.textHistory();
  assert.deepEqual(cursors, ['event-older', 'older-text']);
  assert.deepEqual(h.controller.getState().cards, before.cards);
  assert.equal(h.controller.getState().cardHistoryCursor, before.cardHistoryCursor);
  assert.equal(h.controller.getState().historyCursor, null);
  assert.equal(
    h.controller
      .getState()
      .timeline.some((event) => event.record?.text === 'Original earlier chat'),
    true,
  );
  h.controller.destroy();
});

test('invalid empty/repeated/foreign card pages are errors, not false end-of-history', async () => {
  const base = {
    session_id: 's',
    cards: [],
    limit: 10,
    has_more: false,
    next_cursor: null,
    start_reached: true,
  };
  assert.equal(checkedCardPage(base, 's').start_reached, true);
  for (const value of [
    { ...base, session_id: 'foreign' },
    { ...base, has_more: true, next_cursor: 'next', start_reached: false },
    {
      ...base,
      cards: [
        { id: 'c', created_at: 1 },
        { id: 'c', created_at: 1 },
      ],
    },
    {
      ...base,
      cards: [
        { id: 'z', created_at: 2 },
        { id: 'a', created_at: 1 },
      ],
    },
  ])
    assert.throws(() => checkedCardPage(value, 's'));
  assert.throws(() =>
    checkedCardPage(
      {
        ...base,
        has_more: true,
        next_cursor: 'same',
        start_reached: false,
        cards: [{ id: 'c', created_at: 1 }],
      },
      's',
      'same',
    ),
  );
});

test('take focus rejects foreign card/revision pairing atomically and late owner/session/write responses never enter the next view', async () => {
  const wrong = await fixture().start(),
    before = wrong.controller.getState();
  await assert.rejects(
    wrong.controller.focusCard({ cardId: 'card-005', revisionId: 'revision-006' }),
  );
  assert.equal(wrong.controller.getState(), before);
  wrong.controller.destroy();
  for (const change of ['account', 'session', 'write']) {
    const h = await fixture().start();
    h.holdCard('card-005');
    const request = h.controller.focusCard({ cardId: 'card-005', revisionId: 'revision-005' });
    while (!h.calls.some(([kind, , id]) => kind === 'card' && id === 'card-005'))
      await Promise.resolve();
    if (change === 'account') await h.controller.setAccount('other-owner');
    else if (change === 'session') await h.controller.open('other-session');
    else await h.controller.patch({ title: 'Newly authored title' });
    h.releaseCard();
    await assert.rejects(request);
    assert.equal(h.controller.getState().cards['card-005'], undefined);
    h.controller.destroy();
  }
});
