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

test('several suggested pictures are one confirm that stays confirmed after a reload', async () => {
  const { fromServer } = await import('./story-chat-client.js');
  const ids = ['character-0123456789abcdef', 'location-0123456789abcde1', 'prop-0123456789abcde2'];
  const view = {
    version: 4,
    messages: [
      {
        id: 'turn-abcdef0123456789-a',
        role: 'assistant',
        text: 'Confirm the pictures.',
        changes: [],
        proposal: { kind: 'image', shot_ids: [], picture_card_ids: ids },
      },
    ],
    cards: {
      characters: [{ id: ids[0], name: 'Zib', text: '' }],
      places: [{ id: ids[1], name: 'Field', text: '' }],
      props: [{ id: ids[2], name: 'Pod', text: '' }],
      shots: [],
    },
  };
  const labels = { pictures: (names, count) => `${count}: ${names}`, imageCredits: 5 };
  const [quote] = Object.values(fromServer(view, labels).quotes);
  assert.equal(quote.status, 'open');
  assert.equal(quote.credits, 15);
  assert.equal(quote.label, '3: Zib、Field、Pod');
  assert.deepEqual(quote.action.cards, ids);
  assert.equal(new Set(quote.action.requests).size, 3);
  for (const request of quote.action.requests) assert.match(request, /^[A-Za-z0-9_-]{8,80}$/);
  const reloaded = fromServer({ ...view, image_requests: quote.action.requests }, labels);
  assert.equal(reloaded.quotes[quote.id].status, 'confirmed');
  const partly = fromServer({ ...view, image_requests: quote.action.requests.slice(0, 1) }, labels);
  assert.equal(partly.quotes[quote.id].status, 'open');
});

test('confirmed pictures are drawn one at a time, in order', async () => {
  const { createLiveChat, fromServer } = await import('./story-chat-client.js');
  const ids = ['character-0123456789abcdef', 'location-0123456789abcde1'];
  const view = {
    version: 4,
    project_id: 'p1',
    messages: [
      {
        id: 'turn-abcdef0123456789-a',
        role: 'assistant',
        text: 'Confirm.',
        changes: [],
        proposal: { kind: 'image', shot_ids: [], picture_card_ids: ids },
      },
    ],
    cards: {
      characters: [{ id: ids[0], name: 'Zib', text: '' }],
      places: [{ id: ids[1], name: 'Field', text: '' }],
      props: [],
      shots: [],
    },
  };
  let active = 0,
    most = 0;
  const calls = [];
  const api = {
    async request(path, options = {}) {
      if (!path.endsWith('/images')) return view;
      calls.push(options.body);
      active += 1;
      most = Math.max(most, active);
      await new Promise((resolve) => setTimeout(resolve, 5));
      active -= 1;
      return view;
    },
  };
  const chat = createLiveChat({ api, projectId: 'p1', labels: {} });
  const [quote] = Object.values(fromServer(view, {}).quotes);
  const single = {
    id: 'pic-manual-0123456789',
    action: { kind: 'image', cards: [ids[0]] },
  };
  await Promise.all([
    chat.settle(quote, true, [], () => {}),
    chat.settle(single, true, [], () => {}),
  ]);
  assert.equal(most, 1);
  assert.deepEqual(
    calls.map((body) => body.card_id),
    [ids[0], ids[1], ids[0]],
  );
  assert.deepEqual(
    calls.map((body) => body.request_id),
    [...quote.action.requests, single.id],
  );
});

test('a picture drawn from attached photos sends them with that card only', async () => {
  const { createLiveChat, fromServer } = await import('./story-chat-client.js');
  const ids = ['character-0123456789abcdef', 'location-0123456789abcde1'];
  const view = {
    version: 4,
    project_id: 'p1',
    messages: [
      {
        id: 'turn-abcdef0123456789-a',
        role: 'assistant',
        text: 'Confirm.',
        changes: [],
        proposal: {
          kind: 'image',
          shot_ids: [],
          picture_card_ids: ids,
          picture_sources: { [ids[0]]: ['asset-scarf'] },
        },
      },
    ],
    cards: {
      characters: [{ id: ids[0], name: 'Zib', text: '' }],
      places: [{ id: ids[1], name: 'Field', text: '' }],
      props: [],
      shots: [],
    },
  };
  const calls = [];
  const api = {
    async request(path, options = {}) {
      if (path.endsWith('/images')) calls.push(options.body);
      return view;
    },
  };
  const chat = createLiveChat({ api, projectId: 'p1', labels: {} });
  const [quote] = Object.values(fromServer(view, {}).quotes);
  await chat.settle(quote, true, [], () => {});
  assert.deepEqual(calls[0].photo_ids, ['asset-scarf']);
  assert.equal('photo_ids' in calls[1], false);
});

test('generation advice shows per shot in order, never stale or unknown advice (#628)', async () => {
  const { fromServer, riskWarnings } = await import('./story-chat-client.js');
  const shot = (id, order) => ({ id, order, text: 'x', seconds: 5, start: 0, end: 5 });
  const view = (review) => ({
    version: 1,
    messages: [
      {
        id: 't1-a',
        role: 'assistant',
        text: 'Ready.',
        proposal: { kind: 'generate', shot_ids: ['s1', 's2'], risk_review: review },
      },
    ],
    cards: { shots: [shot('s2', 2), shot('s1', 1)] },
  });
  const review = {
    version: 1,
    status: 'warnings',
    warnings: [
      { shot_id: 's2', code: 'plot_density', source: 'model', suggestion: ' Split it. ' },
      { shot_id: 's1', code: 'speech_too_dense', source: 'rule', suggestion: 'Fewer lines.' },
      { shot_id: 's1', code: 'speech_too_dense', source: 'model', suggestion: 'Again.' },
      { shot_id: 's1', code: 'made_up', source: 'model', suggestion: 'No.' },
      { shot_id: 'other', code: 'action_density', source: 'rule', suggestion: 'No.' },
    ],
  };
  const quote = Object.values(fromServer(view(review)).quotes)[0];
  assert.deepEqual(quote.risks, [
    { shotId: 's1', order: 1, code: 'speech_too_dense', suggestion: 'Fewer lines.' },
    { shotId: 's2', order: 2, code: 'plot_density', suggestion: 'Split it.' },
  ]);
  assert.deepEqual(
    Object.values(fromServer(view({ ...review, status: 'stale' })).quotes)[0].risks,
    [],
  );
  assert.deepEqual(Object.values(fromServer(view(undefined)).quotes)[0].risks, []);
  assert.deepEqual(
    riskWarnings({ version: 2, status: 'warnings', warnings: review.warnings }, []),
    [],
  );
});
