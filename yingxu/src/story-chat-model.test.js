import test from 'node:test';
import assert from 'node:assert/strict';
import {
  applyEvent,
  cardsByKind,
  editCard,
  emptyChat,
  progress,
  quoteAffordable,
} from './story-chat-model.js';
import { createPreviewChat } from './story-chat-preview.js';

const changed = (version, changes, extra = {}) => ({
  type: 'project.changed',
  id: 'c' + version,
  version,
  changes,
  ...extra,
});
const shot = (n, extra = {}) => ({
  op: 'upsert',
  card: {
    id: 'shot-' + n,
    kind: 'shot',
    order: n,
    start: (n - 1) * 5,
    end: n * 5,
    status: 'draft',
    ...extra,
  },
});

test('a replayed stream changes nothing', () => {
  const events = [
    { type: 'user.message', id: 'u1', text: 'aliens' },
    changed(1, [{ op: 'upsert', card: { id: 'zib', kind: 'character', name: 'Zib' } }], {
      summary: { kind: 'character', count: 1 },
    }),
    { type: 'quote.proposed', id: 'q1', credits: 8, action: { kind: 'image', cards: ['zib'] } },
  ];
  const once = events.reduce(applyEvent, emptyChat());
  const twice = events.reduce(applyEvent, once);
  assert.deepEqual(twice, once);
  assert.equal(once.messages.length, 3);
});

test("the user's own card edit wins over an older assistant change", () => {
  let state = applyEvent(emptyChat(), changed(1, [shot(3, { text: 'old' })]));
  state = editCard(state, 'shot-3', { text: 'mine' });
  state = applyEvent(state, changed(2, [shot(3, { text: 'assistant' })]));
  assert.equal(state.cards[0].text, 'mine');
  state = applyEvent(
    state,
    changed(3, [{ op: 'upsert', card: { id: 'shot-3', status: 'ready' } }]),
  );
  assert.equal(state.cards[0].text, 'mine');
  assert.equal(state.cards[0].status, 'ready');
});

test('progress counts cast, length and made shots', () => {
  const state = applyEvent(
    emptyChat(),
    changed(1, [
      { op: 'upsert', card: { id: 'mia', kind: 'character', name: 'Mia', image: 'x' } },
      { op: 'upsert', card: { id: 'field', kind: 'place', name: 'Field' } },
      shot(2, { status: 'ready' }),
      shot(1),
    ]),
  );
  assert.deepEqual(
    cardsByKind(state).shot.map((card) => card.id),
    ['shot-1', 'shot-2'],
  );
  assert.deepEqual(progress(state), {
    characters: 1,
    places: 1,
    props: 0,
    shots: 2,
    seconds: 10,
    generated: 1,
    pictured: 1,
    ready: true,
    done: false,
  });
  assert.equal(quoteAffordable({ credits: 120 }, 100), false);
  assert.equal(quoteAffordable({ credits: 120 }, 300), true);
});

test('the preview never charges before a confirm and builds a whole story', async () => {
  const chat = createPreviewChat({ locale: 'en', pace: 0 });
  let state = emptyChat();
  const emit = (event) => (state = applyEvent(state, event));
  const settle = () => new Promise((resolve) => setTimeout(resolve, 20));
  chat.send({ text: 'aliens' }, emit);
  await settle();
  chat.send({ text: '30 seconds, tender' }, emit);
  await settle();
  assert.equal(progress(state).shots, 6);
  chat.send({ text: 'Generate all shots' }, emit);
  await settle();
  const quote = Object.values(state.quotes)[0];
  assert.equal(quote.credits, 120);
  assert.equal(progress(state).generated, 0);
  chat.settle(quote, true, state.cards, emit);
  await settle();
  assert.equal(progress(state).done, true);
  chat.close();
});
