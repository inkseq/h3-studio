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

test('confirmed pictures: the first alone, the rest a few at a time, shots last (h3-studio#1246)', async () => {
  const { createLiveChat } = await import('./story-chat-client.js');
  const ids = [
    'character-0123456789abcdef',
    'location-0123456789abcde1',
    'prop-0123456789abcdef012',
    'character-0123456789abcde2',
    'character-0123456789abcde3',
  ];
  const shot = 'shot-0123456789abcdef0123';
  const view = {
    version: 4,
    project_id: 'p1',
    messages: [
      {
        id: 'turn-abcdef0123456789-a',
        role: 'assistant',
        text: 'Confirm.',
        changes: [],
        proposal: null,
      },
    ],
    cards: {
      characters: [
        { id: ids[0], name: 'Zib', text: '' },
        { id: ids[3], name: 'Ko', text: '' },
        { id: ids[4], name: 'Lu', text: '' },
      ],
      places: [{ id: ids[1], name: 'Field', text: '' }],
      props: [{ id: ids[2], name: 'Pod', text: '' }],
      shots: [{ id: shot, order: 1, text: 'Zib lands.', seconds: 5, start: 0, end: 5 }],
    },
  };
  let active = 0;
  const calls = [];
  const api = {
    async request(path, options = {}) {
      if (!path.endsWith('/images')) return view;
      active += 1;
      calls.push({ card: options.body.card_id, request: options.body.request_id, active });
      await new Promise((resolve) => setTimeout(resolve, 5));
      active -= 1;
      return view;
    },
  };
  const chat = createLiveChat({ api, projectId: 'p1', labels: {} });
  await chat.open(() => {});
  // The batch as a picture confirm carries it: cards with their own request ids.
  const cards = [shot, ...ids];
  const quote = {
    id: 'pics-0123456789abcdef',
    action: {
      kind: 'image',
      cards,
      requests: cards.map((_card, i) => `pic-batch-${i}-0123456789`),
    },
  };
  const single = { id: 'pic-manual-0123456789', action: { kind: 'image', cards: [ids[0]] } };
  await Promise.all([
    chat.settle(quote, true, [], () => {}),
    chat.settle(single, true, [], () => {}),
  ]);
  const order = calls.map((call) => call.card);
  // No card has a picture yet: the first card is drawn alone and sets the style.
  assert.equal(order[0], ids[0]);
  assert.equal(calls[0].active, 1);
  // The other cards together, at most three at once; the shot after all of them.
  assert.deepEqual(new Set(order.slice(1, 5)), new Set(ids.slice(1)));
  assert.equal(Math.max(...calls.slice(1, 5).map((call) => call.active)), 3);
  assert.equal(order[5], shot);
  assert.equal(calls[5].active, 1);
  // A later confirm waits for the whole first one.
  assert.deepEqual(calls[6], { card: ids[0], request: single.id, active: 1 });
  const requests = Object.fromEntries(
    quote.action.cards.map((card, index) => [card, quote.action.requests[index]]),
  );
  for (const call of calls.slice(0, 6)) assert.equal(call.request, requests[call.card]);
});

test('a first picture that fails leaves the next one to set the style, alone', async () => {
  const { createLiveChat } = await import('./story-chat-client.js');
  const ids = ['character-a', 'character-b', 'character-c', 'character-d'];
  const view = {
    version: 4,
    project_id: 'p1',
    messages: [],
    cards: {
      characters: ids.map((id) => ({ id, name: id, text: '' })),
      places: [],
      props: [],
      shots: [],
    },
  };
  let active = 0;
  const calls = [];
  const api = {
    async request(path, options = {}) {
      if (!path.endsWith('/images')) return view;
      active += 1;
      calls.push({ card: options.body.card_id, active });
      await new Promise((resolve) => setTimeout(resolve, 5));
      active -= 1;
      if (options.body.card_id === ids[0]) throw new Error('provider failed');
      return view;
    },
  };
  const chat = createLiveChat({ api, projectId: 'p1', labels: {} });
  await chat.open(() => {});
  const quote = {
    id: 'pics-0123456789abcdef',
    action: { kind: 'image', cards: ids, requests: ids.map((id) => `pic-${id}-0123456789`) },
  };
  await chat.settle(quote, true, [], () => {});
  assert.deepEqual(
    calls.slice(0, 2).map((call) => [call.card, call.active]),
    [
      [ids[0], 1],
      [ids[1], 1],
    ],
  );
  assert.deepEqual(new Set(calls.slice(2).map((call) => call.card)), new Set(ids.slice(2)));
  assert.equal(Math.max(...calls.slice(2).map((call) => call.active)), 2);
});

test('once the story has a picture, every new card picture starts at once', async () => {
  const { createLiveChat, fromServer } = await import('./story-chat-client.js');
  const ids = ['location-0123456789abcde1', 'prop-0123456789abcdef012'];
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
      characters: [{ id: 'character-drawn', name: 'Zib', text: '', image_url: '/zib.png' }],
      places: [{ id: ids[0], name: 'Field', text: '' }],
      props: [{ id: ids[1], name: 'Pod', text: '' }],
      shots: [],
    },
  };
  let active = 0,
    most = 0;
  const api = {
    async request(path) {
      if (!path.endsWith('/images')) return view;
      active += 1;
      most = Math.max(most, active);
      await new Promise((resolve) => setTimeout(resolve, 5));
      active -= 1;
      return view;
    },
  };
  const chat = createLiveChat({ api, projectId: 'p1', labels: {} });
  await chat.open(() => {});
  const [quote] = Object.values(fromServer(view, {}).quotes);
  await chat.settle(quote, true, [], () => {});
  assert.equal(most, 2);
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

test('sent photos show on the bubble: at once, after the reply and after a reload (#457)', async () => {
  const { fromServer } = await import('./story-chat-client.js');
  const local = [
    { kind: 'image', name: 'scarf.png', url: 'blob:local' },
    { kind: 'video', name: 'walk.mp4', url: 'blob:video' },
  ];
  const sent = applyEvent(emptyChat(), {
    type: 'user.message',
    id: 't2-u',
    text: 'use this',
    attachments: local,
  });
  const view = {
    version: 2,
    messages: [
      { id: 't1-u', turn_id: 't1', role: 'user', text: 'aliens' },
      {
        id: 't2-u',
        turn_id: 't2',
        role: 'user',
        text: 'use this',
        photos: ['asset-a1'],
        photo_files: [
          { id: 'asset-a1', name: 'scarf.png', url: '/v1/assets/a1/content' },
          { id: 'asset-x', name: 'x', url: 'https://elsewhere.example/x.png' },
        ],
      },
      { id: 't2-a', turn_id: 't2', role: 'assistant', text: 'Nice scarf.' },
    ],
  };
  const server = fromServer(view);
  // After a reload the server's own asset path is the picture; nothing else is shown.
  assert.deepEqual(server.messages[1].attachments, [
    { kind: 'image', name: 'scarf.png', url: '/v1/assets/a1/content' },
  ]);
  assert.deepEqual(server.messages[0].attachments, []);
  // Right after sending, what is already on screen stays (the video too).
  const replied = applyEvent(sent, { type: 'chat.loaded', state: server });
  assert.deepEqual(replied.messages[1].attachments, local);
  assert.equal(replied.messages[2].text, 'Nice scarf.');
});

// h3-studio#1244: declining a picture used to strand a story (a voice sample needs a picture),
// and nothing led on from the pictures to the video. One offer now draws what the shots need.
const VIDEO_IDS = [
  'character-0123456789abcdef',
  'location-0123456789abcde1',
  'prop-0123456789abcde2',
];
const videoView = (proposal, extra = {}) => ({
  version: 5,
  project_id: 'p1',
  messages: [
    { id: 't1-u', turn_id: 't1', role: 'user', text: 'a lighthouse keeper' },
    {
      id: 'turn-abcdef0123456789-a',
      turn_id: 't1',
      role: 'assistant',
      text: 'Built the cards.',
      changes: [],
      proposal,
    },
  ],
  cards: {
    characters: [{ id: VIDEO_IDS[0], name: 'Thomas', text: '' }],
    places: [{ id: VIDEO_IDS[1], name: 'Shore', text: '' }],
    props: [{ id: VIDEO_IDS[2], name: 'Bottle', text: '', image_url: '/v1/assets/b/content' }],
    shots: [
      {
        id: 's1',
        order: 1,
        text: 'He walks',
        seconds: 5,
        cast: [VIDEO_IDS[0]],
        place: VIDEO_IDS[1],
        props: [VIDEO_IDS[2]],
      },
      {
        id: 's2',
        order: 2,
        text: 'He reads',
        seconds: 5,
        cast: [VIDEO_IDS[0]],
        place: VIDEO_IDS[1],
        props: [],
        takes: 1,
      },
    ],
  },
  ...extra,
});

test('suggested pictures and shots without a take become one video offer', async () => {
  const { fromServer } = await import('./story-chat-client.js');
  const labels = { generate: (count) => `Make ${count}`, imageCredits: 5 };
  const proposal = { kind: 'image', shot_ids: [], picture_card_ids: [VIDEO_IDS[0]] };
  const state = fromServer(videoView(proposal), labels);
  const quotes = Object.values(state.quotes);
  assert.equal(quotes.length, 1);
  const [quote] = quotes;
  assert.equal(quote.live, true);
  assert.equal(quote.label, 'Make 1');
  assert.deepEqual(quote.action.cards, ['s1']);
  // The suggested picture plus the place the shot uses that has none; the bottle has one.
  assert.deepEqual(quote.action.pictures, [VIDEO_IDS[0], VIDEO_IDS[1]]);
  assert.equal(quote.credits, 10);
  for (const request of quote.action.requests) assert.match(request, /^[A-Za-z0-9_-]{8,80}$/);
  // A reload after one picture started offers only what is left, with the same ids.
  const later = fromServer(
    videoView(proposal, { image_requests: quote.action.requests.slice(0, 1) }),
    labels,
  ).quotes[quote.id];
  assert.deepEqual(later.action.pictures, [VIDEO_IDS[1]]);
  assert.deepEqual(later.action.requests, quote.action.requests.slice(1));
  assert.equal(later.credits, 5);
  // With pictures switched off the offer draws nothing.
  const off = fromServer(videoView(proposal), { ...labels, picturesEnabled: false });
  assert.deepEqual(Object.values(off.quotes)[0].action.pictures, []);
});

test('the video offer draws its pictures, prices the shots and makes them in the chat', async () => {
  const { createLiveChat, fromServer } = await import('./story-chat-client.js');
  const { applyEvent } = await import('./story-chat-model.js');
  const proposal = { kind: 'generate', shot_ids: ['s1'] };
  let view = videoView(proposal);
  const calls = [];
  let preflight = 'credits',
    jobState = 'running',
    made = false,
    refuseBatch = false,
    refuse = '';
  const api = {
    async request(path, options = {}) {
      calls.push([options.method || 'GET', path.replace('/v1/', ''), options.key || '']);
      if (path.endsWith('/images')) {
        if (options.body.card_id === refuse) throw Object.assign(Error('no'), { detail: {} });
        const drawn = structuredClone(view);
        for (const group of ['characters', 'places'])
          for (const card of drawn.cards[group])
            if (card.id === options.body.card_id) card.image_url = '/v1/assets/new/content';
        view = drawn;
        return view;
      }
      if (path === '/v1/projects/p1') return { version: 9 };
      if (path.endsWith('/storyboard/preflights'))
        return preflight === 'credits'
          ? {
              id: 'pf1',
              project_version: 9,
              status: 'completed',
              items: [
                { shot_id: 's1', plan_id: 'plan-1', plan: { admission: 'credits', credits: 30 } },
              ],
            }
          : { id: 'pf2', status: 'completed', closed_preflight: true, items: [] };
      if (path === '/v1/batches') {
        made = !refuseBatch;
        return {
          batch_id: 'b1',
          items: [
            {
              plan_id: 'plan-1',
              job_id: 'j1',
              status: refuseBatch ? 'admission_blocked' : 'queued',
              error_code: refuseBatch ? 'insufficient_credits' : null,
            },
          ],
        };
      }
      if (path.startsWith('/v1/jobs?'))
        return { jobs: made ? [{ client_ref: { shot_id: 's1' }, status: jobState }] : [] };
      return view;
    },
  };
  const opened = [];
  const labels = { openWorks: (...args) => opened.push(args), failed: () => 'failed', pollMs: 1 };
  let state = { ...fromServer(view, labels) };
  const spent = [];
  const emit = (event) => {
    if (event.type === 'credits.spent') spent.push(event.credits);
    state = applyEvent(state, event);
  };
  const chat = createLiveChat({ api, projectId: 'p1', labels });
  const quote = Object.values(state.quotes)[0];
  await chat.settle(quote, true, [], emit);
  // Pictures first, in order, then one check at the story's version.
  assert.deepEqual(
    calls.filter(([, path]) => path.endsWith('/images')).length,
    quote.action.pictures.length,
  );
  const check = calls.find(([, path]) => path.endsWith('/storyboard/preflights'));
  assert.match(check[2], /-v9$/);
  assert.deepEqual(opened, []);
  const priced = state.quotes[quote.id];
  assert.equal(priced.status, 'priced');
  assert.equal(priced.credits, 30);
  assert.deepEqual(priced.plans, ['plan-1']);
  // Confirm: one batch with those plans, then progress until the take arrives.
  setTimeout(() => (jobState = 'succeeded'), 5);
  await chat.make(priced, emit);
  const batch = calls.find(([method, path]) => method === 'POST' && path === 'batches');
  assert.deepEqual(spent, [30]);
  assert.equal(batch[2], 'chat-batch-pf1');
  assert.equal(state.quotes[quote.id].status, 'made');
  assert.deepEqual(state.quotes[quote.id].made, { done: 1, total: 1, failed: 0 });

  // Refused inside a 202 (credits): nothing is watched, the card offers Works.
  view = videoView(proposal);
  for (const card of [...view.cards.characters, ...view.cards.places])
    card.image_url = '/v1/assets/x/content';
  made = false;
  refuseBatch = true;
  const refusedChat = createLiveChat({ api, projectId: 'p1', labels });
  state = { ...fromServer(view, labels) };
  const fresh = Object.values(state.quotes)[0];
  await refusedChat.settle(fresh, true, [], emit);
  assert.equal(state.quotes[fresh.id].status, 'priced');
  await refusedChat.make(state.quotes[fresh.id], emit);
  assert.equal(state.quotes[fresh.id].status, 'partly');
  // Nothing started, so nothing leaves the shown balance.
  assert.deepEqual(spent, [30]);
  assert.deepEqual(state.quotes[fresh.id].made, { done: 0, total: 1, failed: 1 });
  refuseBatch = false;

  // A check the chat cannot price opens Works, exactly as before.
  view = videoView(proposal);
  preflight = 'closed';
  const second = createLiveChat({ api, projectId: 'p1', labels });
  state = { ...fromServer(view, labels) };
  await second.settle(Object.values(state.quotes)[0], true, [], emit);
  assert.deepEqual(opened, [['p1', 's1', { generate: true }]]);

  // A picture that does not arrive keeps the offer open and checks nothing.
  view = videoView(proposal);
  refuse = VIDEO_IDS[1];
  calls.length = 0;
  opened.length = 0;
  const third = createLiveChat({ api, projectId: 'p1', labels });
  state = { ...fromServer(view, labels) };
  const open = Object.values(state.quotes)[0];
  await third.settle(open, true, [], emit);
  assert.deepEqual(opened, []);
  assert.equal(state.quotes[open.id].status, 'open');
  assert.equal(
    calls.some(([, path]) => path.endsWith('/storyboard/preflights')),
    false,
  );
});

test("shot progress reads each shot's newest job", async () => {
  const { shotProgress, planCredits } = await import('./story-chat-client.js');
  const jobs = [
    { client_ref: { shot_id: 'a' }, status: 'succeeded' },
    { client_ref: { shot_id: 'b' }, status: 'running' },
    { client_ref: { shot_id: 'c' }, status: 'failed' },
    { client_ref: { shot_id: 'c' }, status: 'succeeded' },
    { client_ref: { shot_id: 'z' }, status: 'running' },
  ];
  const progress = shotProgress(jobs, ['a', 'b', 'c', 'd']);
  assert.deepEqual(
    [progress.done, progress.failed, progress.making, progress.finished, progress.total],
    [1, 1, ['b'], false, 4],
  );
  assert.equal(shotProgress(jobs, ['a', 'c']).finished, true);
  assert.equal(shotProgress(null, ['a']).finished, false);
  assert.deepEqual(
    shotProgress([{ client_ref: { shot_id: 'a' }, status: 'planned' }], ['a']).making,
    ['a'],
  );
  assert.equal(
    planCredits({
      items: [
        { plan: { admission: 'credits', credits: 5 } },
        { plan: { admission: 'credits', credits: 7 } },
      ],
    }),
    12,
  );
  assert.equal(planCredits({ items: [{ plan: { admission: 'usd' } }] }), null);
  assert.equal(planCredits({ items: [] }), null);
});

test('a shot with a take or one still being made is not offered or made again', async () => {
  const { createLiveChat, fromServer } = await import('./story-chat-client.js');
  const { applyEvent } = await import('./story-chat-model.js');
  // s2 already has a take: the offer names only s1.
  const proposal = { kind: 'generate', shot_ids: ['s1', 's2'] };
  const view = videoView(proposal);
  for (const card of [...view.cards.characters, ...view.cards.places])
    card.image_url = '/v1/assets/x/content';
  const labels = { openWorks: () => {}, failed: () => 'failed', pollMs: 1 };
  let state = { ...fromServer(view, labels) };
  const [quote] = Object.values(state.quotes);
  assert.deepEqual(quote.action.cards, ['s1']);
  // s1 is being made already (a reload during making): no check, no batch, just watching.
  const calls = [];
  let status = 'running';
  const api = {
    async request(path, options = {}) {
      calls.push([options.method || 'GET', path]);
      if (path.startsWith('/v1/jobs?'))
        return { jobs: [{ client_ref: { shot_id: 's1' }, status }] };
      return view;
    },
  };
  setTimeout(() => (status = 'succeeded'), 5);
  await createLiveChat({ api, projectId: 'p1', labels }).settle(quote, true, [], (event) => {
    state = applyEvent(state, event);
  });
  assert.equal(
    calls.some(([method]) => method === 'POST'),
    false,
  );
  assert.equal(state.quotes[quote.id].status, 'made');
});

test('a price for an older story version is checked again, never submitted', async () => {
  const { createLiveChat, fromServer } = await import('./story-chat-client.js');
  const { applyEvent } = await import('./story-chat-model.js');
  const view = videoView({ kind: 'generate', shot_ids: ['s1'] });
  for (const card of [...view.cards.characters, ...view.cards.places])
    card.image_url = '/v1/assets/x/content';
  let version = 9;
  const calls = [];
  const api = {
    async request(path, options = {}) {
      calls.push([options.method || 'GET', path, options.key || '']);
      if (path === '/v1/projects/p1') return { version };
      if (path.startsWith('/v1/jobs?')) return { jobs: [] };
      if (path.endsWith('/storyboard/preflights'))
        return {
          id: 'pf-' + version,
          project_version: version,
          status: 'completed',
          items: [
            {
              shot_id: 's1',
              plan_id: 'plan-' + version,
              plan: { admission: 'credits', credits: 30 },
            },
          ],
        };
      return view;
    },
  };
  const labels = { openWorks: () => {}, failed: () => 'failed', pollMs: 1 };
  let state = { ...fromServer(view, labels) };
  const emit = (event) => (state = applyEvent(state, event));
  const chat = createLiveChat({ api, projectId: 'p1', labels });
  const [quote] = Object.values(state.quotes);
  await chat.settle(quote, true, [], emit);
  assert.equal(state.quotes[quote.id].version, 9);
  version = 10; // an edit elsewhere
  await chat.make(state.quotes[quote.id], emit);
  assert.equal(
    calls.some(([method, path]) => method === 'POST' && path === '/v1/batches'),
    false,
  );
  assert.equal(state.quotes[quote.id].status, 'priced');
  assert.equal(state.quotes[quote.id].version, 10);
  assert.deepEqual(state.quotes[quote.id].plans, ['plan-10']);
  chat.close();
});

test('a finished story says so instead of offering its pictures again', async () => {
  const { fromServer } = await import('./story-chat-client.js');
  // Every shot has a take: one "all ready" offer, no picture offer.
  const view = videoView({ kind: 'image', shot_ids: [], picture_card_ids: [VIDEO_IDS[0]] });
  view.cards.shots[0].takes = 1;
  const quotes = Object.values(fromServer(view, { imageCredits: 5 }).quotes);
  assert.equal(quotes.length, 1);
  assert.equal(quotes[0].status, 'made');
  assert.equal(quotes[0].action.kind, 'generate');
  // A shot still without a take: the video offer, never a claim that all are ready.
  const open = videoView({ kind: 'image', shot_ids: [], picture_card_ids: [VIDEO_IDS[0]] });
  const [offer] = Object.values(fromServer(open, { imageCredits: 5 }).quotes);
  assert.equal(offer.status, 'open');
  assert.deepEqual(offer.action.cards, ['s1']);
  // Named shots that all have takes stay as a finished offer.
  const named = videoView({ kind: 'generate', shot_ids: ['s1', 's2'] });
  named.cards.shots[0].takes = 1;
  const [done] = Object.values(fromServer(named, {}).quotes);
  assert.equal(done.status, 'made');
  assert.deepEqual(done.made, { done: 2, total: 2, failed: 0 });
});
