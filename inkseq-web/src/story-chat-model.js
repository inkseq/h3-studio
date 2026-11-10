// Chat-first Quick Create (#422): the chat builds a story's cards. This module is the pure state
// the view renders; it only folds server events (or the local preview's events) into that state.

export const CARD_GROUPS = [
  { kind: 'character', key: 'characters' },
  { kind: 'place', key: 'places' },
  { kind: 'prop', key: 'props' },
  { kind: 'shot', key: 'shots' },
];

export function emptyChat(title = '') {
  return {
    title,
    version: 0,
    messages: [],
    cards: [],
    quotes: {},
    busy: false,
    draft: '',
    step: '',
  };
}

// What the person just attached stays on their own bubble (inkseq-design#457): the
// server's view names only the photos, and these previews are already on screen.
const keepSent = (before, after) => {
  const sent = new Map(
    before
      .filter((message) => message.role === 'user' && message.attachments?.length)
      .map((message) => [message.id, message.attachments]),
  );
  return sent.size
    ? after.map((message) =>
        sent.has(message.id) ? { ...message, attachments: sent.get(message.id) } : message,
      )
    : after;
};

const upsert = (cards, card) => {
  const index = cards.findIndex((item) => item.id === card.id);
  if (index < 0) return [...cards, card];
  const next = cards.slice();
  next[index] = { ...cards[index], ...card };
  return next;
};

function addMessage(state, event) {
  if (state.messages.some((item) => item.id === event.id)) return state;
  const role = event.type === 'user.message' ? 'user' : 'assistant';
  return {
    ...state,
    messages: [
      ...state.messages,
      {
        id: event.id,
        role,
        text: event.text || '',
        attachments: event.attachments || [],
        chips: event.chips || [],
      },
    ],
  };
}

function projectChanged(state, event) {
  // A stale version means the user's own edit already superseded this change.
  if (event.version <= state.version) return state;
  let cards = state.cards;
  for (const change of event.changes || []) {
    if (change.op === 'upsert') cards = upsert(cards, change.card);
    if (change.op === 'remove') cards = cards.filter((card) => card.id !== change.id);
  }
  const next = { ...state, version: event.version, cards };
  if (event.title) next.title = event.title;
  if (!event.summary || state.messages.some((item) => item.id === event.id)) return next;
  return {
    ...next,
    messages: [...next.messages, { id: event.id, role: 'change', summary: event.summary }],
  };
}

const EVENTS = {
  // While a turn runs: the reply written so far and the server's step (#1083).
  'turn.started': (state) => ({ ...state, busy: true, draft: '', step: '' }),
  'turn.reply': (state, event) => (state.busy ? { ...state, draft: event.text || '' } : state),
  'turn.progress': (state, event) => (state.busy ? { ...state, step: event.step || '' } : state),
  'turn.finished': (state) => ({ ...state, busy: false, draft: '', step: '' }),
  'user.message': addMessage,
  'assistant.message': addMessage,
  'project.changed': projectChanged,
  'card.status': (state, event) => ({
    ...state,
    cards: state.cards.map((card) =>
      card.id === event.id ? { ...card, status: event.status } : card,
    ),
  }),
  // The live server's whole view replaces local state (#422).
  'chat.loaded': (state, event) => ({
    ...state,
    ...event.state,
    messages: keepSent(state.messages, event.state.messages),
    quotes: event.state.quotes || {},
    draft: '',
    step: '',
  }),
  'quote.proposed': (state, event) =>
    state.quotes[event.id]
      ? state
      : {
          ...state,
          quotes: { ...state.quotes, [event.id]: { ...event, status: 'open' } },
          messages: [...state.messages, { id: event.id, role: 'quote', quoteId: event.id }],
        },
  'quote.settled': (state, event) =>
    state.quotes[event.id]
      ? {
          ...state,
          quotes: {
            ...state.quotes,
            [event.id]: { ...state.quotes[event.id], status: event.status },
          },
        }
      : state,
};

// Every event is idempotent per id, so a replayed stream after reconnecting changes nothing.
export function applyEvent(state, event) {
  return Object.hasOwn(EVENTS, event.type) ? EVENTS[event.type](state, event) : state;
}

// The user's own card edit wins: it bumps the version so an older assistant change is ignored.
export function editCard(state, id, patch) {
  const card = state.cards.find((item) => item.id === id);
  if (!card) return state;
  return {
    ...state,
    version: state.version + 1,
    cards: upsert(state.cards, { ...card, ...patch }),
  };
}

export function cardsByKind(state) {
  const groups = Object.fromEntries(CARD_GROUPS.map((group) => [group.kind, []]));
  for (const card of state.cards) groups[card.kind]?.push(card);
  groups.shot.sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
  return groups;
}

// What the progress strip and the "Open in Works" offer read.
export function progress(state) {
  const groups = cardsByKind(state);
  const shots = groups.shot;
  const seconds = shots.reduce(
    (sum, shot) => sum + Math.max(0, (shot.end ?? 0) - (shot.start ?? 0)),
    0,
  );
  const generated = shots.filter((shot) => shot.status === 'ready').length;
  const pictured = [...groups.character, ...groups.place, ...groups.prop].filter(
    (card) => card.image,
  ).length;
  return {
    characters: groups.character.length,
    places: groups.place.length,
    props: groups.prop.length,
    shots: shots.length,
    seconds,
    generated,
    pictured,
    ready: shots.length > 0 && groups.character.length + groups.place.length > 0,
    done: shots.length > 0 && generated === shots.length,
  };
}

export const quoteAffordable = (quote, balance) =>
  !Number.isFinite(balance) || balance >= (quote?.credits || 0);
