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

test('the live server view becomes cards, change notes and an advice-only proposal', async () => {
  const { fromServer } = await import('./story-chat-client.js');
  const view = {
    title: 'First Contact',
    version: 3,
    messages: [
      { id: 't1-u', turn_id: 't1', role: 'user', text: 'aliens' },
      {
        id: 't1-a',
        turn_id: 't1',
        role: 'assistant',
        text: 'Made the cast.',
        chips: ['30 seconds'],
        changes: [
          { kind: 'character', op: 'create', id: 'zib' },
          { kind: 'location', op: 'create', id: 'field' },
          { kind: 'shot', op: 'create', id: 's1' },
          { kind: 'shot', op: 'update', id: 's0' },
        ],
        proposal: { kind: 'generate', shot_ids: ['s1'] },
      },
    ],
    cards: {
      characters: [{ id: 'zib', name: 'Zib', text: 'alien', version: 1 }],
      places: [{ id: 'field', name: 'Field', text: 'night', version: 1 }],
      props: [],
      shots: [
        {
          id: 's1',
          order: 1,
          text: 'Light falls',
          seconds: 5,
          start: 0,
          end: 5,
          cast: ['zib'],
          place: 'field',
          props: [],
          selected_artifact_id: null,
          version: 1,
        },
      ],
    },
  };
  const state = fromServer(view, { generate: (count) => `Make ${count}` });
  assert.equal(state.version, 3);
  assert.deepEqual(
    state.cards.map((card) => [card.id, card.kind]),
    [
      ['zib', 'character'],
      ['field', 'place'],
      ['s1', 'shot'],
    ],
  );
  assert.deepEqual(state.cards[2].refs, ['zib', 'field']);
  assert.deepEqual(
    state.messages.map((message) => message.role + ':' + (message.summary?.kind || '')),
    [
      'user:',
      'change:character',
      'change:place',
      'change:shot',
      'change:edit',
      'assistant:',
      'quote:',
    ],
  );
  const quote = Object.values(state.quotes)[0];
  assert.equal(quote.live, true);
  assert.equal(quote.label, 'Make 1');
  assert.equal(quote.credits, undefined);
  assert.equal(progress({ ...emptyChat(), ...state }).shots, 1);
});

test('a suggested card picture becomes one stable confirm card', async () => {
  const { fromServer } = await import('./story-chat-client.js');
  const view = {
    version: 4,
    messages: [
      { id: 't1-u', role: 'user', text: 'redraw zib' },
      {
        id: 'turn-abcdef0123456789-a',
        role: 'assistant',
        text: 'You can confirm a new picture.',
        changes: [],
        proposal: { kind: 'image', shot_ids: [], picture_card_ids: ['character-0123456789abcdef'] },
      },
    ],
    cards: {
      characters: [{ id: 'character-0123456789abcdef', name: 'Zib', text: '' }],
      places: [],
      props: [],
      shots: [],
    },
  };
  const labels = { picture: (name) => `Draw ${name}`, imageCredits: 5 };
  const first = fromServer(view, labels);
  const quotes = Object.values(first.quotes);
  assert.equal(quotes.length, 1);
  assert.deepEqual(quotes[0].action, { kind: 'image', cards: ['character-0123456789abcdef'] });
  assert.equal(quotes[0].credits, 5);
  assert.equal(quotes[0].label, 'Draw Zib');
  assert.match(quotes[0].id, /^[A-Za-z0-9_-]{8,80}$/);
  assert.equal(Object.keys(fromServer(view, labels).quotes)[0], quotes[0].id);
});

test('the server view marks the latest turn undoable only when it says so', async () => {
  const { fromServer } = await import('./story-chat-client.js');
  const view = {
    title: 'T',
    version: 3,
    cards: { characters: [], places: [], props: [], shots: [] },
    messages: [
      { id: 't1-u', turn_id: 't1', role: 'user', text: 'hi' },
      { id: 't1-a', turn_id: 't1', role: 'assistant', text: 'ok', chips: [], changes: [] },
    ],
    undo_turn_id: 't1',
  };
  const state = fromServer(view, {});
  assert.equal(state.undoTurnId, 't1');
  assert.equal(state.messages.find((m) => m.role === 'assistant').turnId, 't1');
  assert.equal(fromServer({ ...view, undo_turn_id: null }, {}).undoTurnId, null);
});
