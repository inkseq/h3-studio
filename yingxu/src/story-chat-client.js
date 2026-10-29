// Live story chat (#422): the same interface as story-chat-preview.js, backed by
// /v1/story-chats. The server owns the transcript and cards; every reply replaces the local
// state with the server's view. Paid generation is never started here: a proposal hands the
// story to Works, where the existing preflight, quote and confirmation run.
import { track } from './analytics.js';
import { attachActions, attachmentNote, uploadReady } from './story-chat-attach.js';

const KIND = { characters: 'character', places: 'place', props: 'prop' };

function cardsFromServer(view) {
  const cards = [];
  for (const [group, kind] of Object.entries(KIND))
    for (const card of view.cards?.[group] || [])
      cards.push({
        id: card.id,
        kind,
        name: card.name,
        text: card.text,
        image: card.image_url || null,
        version: card.version,
      });
  for (const shot of view.cards?.shots || [])
    cards.push({
      id: shot.id,
      kind: 'shot',
      order: shot.order,
      text: shot.text,
      start: shot.start,
      end: shot.end,
      seconds: shot.seconds,
      cast: shot.cast || [],
      place: shot.place || null,
      props: shot.props || [],
      refs: [...(shot.cast || []), shot.place, ...(shot.props || [])].filter(Boolean),
      image: shot.thumbnail_url || null,
      // Finished takes but none chosen yet (e.g. moved from old Quick Create).
      status: shot.selected_artifact_id ? 'ready' : shot.takes > 0 ? 'pick' : 'draft',
      version: shot.version,
    });
  return cards;
}

// One summary row per kind of created card, and one for edits.
function changeRows(message, messages) {
  const counts = {};
  for (const change of message.changes || []) {
    if (change.op !== 'create') continue;
    const kind = change.kind === 'location' || change.kind === 'prop' ? 'place' : change.kind;
    counts[kind] = (counts[kind] || 0) + 1;
  }
  const edits = (message.changes || []).filter(
    (change) => change.op === 'update' && change.kind !== 'title',
  ).length;
  for (const [kind, count] of Object.entries(counts))
    if (['character', 'place', 'shot'].includes(kind))
      messages.push({ id: `${message.id}-${kind}`, role: 'change', summary: { kind, count } });
  if (edits)
    messages.push({
      id: `${message.id}-edit`,
      role: 'change',
      summary: { kind: 'edit', count: edits },
    });
}

// The newest reply's proposals become confirm cards: pictures, a new story, generation.
function proposalQuotes(message, { cards, labels, messages, quotes }) {
  // Suggested card pictures: a confirm card each; the id is stable, so a replay never
  // draws or charges twice.
  if (labels.picturesEnabled !== false)
    for (const card of message.proposal?.picture_card_ids || []) {
      const id = `pic-${message.id.slice(-14)}-${card.slice(-14)}`.replace(/[^A-Za-z0-9_-]/g, '');
      const name = cards.find((item) => item.id === card)?.name || '';
      quotes[id] = {
        id,
        status: 'open',
        action: { kind: 'image', cards: [card] },
        credits: labels.imageCredits ?? null,
        label: labels.picture?.(name) || name,
      };
      messages.push({ id, role: 'quote', quoteId: id });
    }
  if (message.proposal?.new_story) {
    const id = `new-${message.id.slice(-20)}`.replace(/[^A-Za-z0-9_-]/g, '');
    quotes[id] = {
      id,
      status: 'open',
      action: { kind: 'new_story', idea: message.proposal.new_story },
      label: labels.newStory?.(message.proposal.new_story) || message.proposal.new_story,
    };
    messages.push({ id, role: 'quote', quoteId: id });
  }
  if (message.proposal?.shot_ids?.length) {
    const id = `${message.id}-q`;
    quotes[id] = {
      id,
      live: true,
      status: 'open',
      action: { kind: 'generate', cards: message.proposal.shot_ids },
      label: labels.generate?.(message.proposal.shot_ids.length) || '',
    };
    messages.push({ id, role: 'quote', quoteId: id });
  }
}

export function fromServer(view, labels = {}) {
  const cards = cardsFromServer(view);
  const messages = [],
    quotes = {};
  // Only the newest reply's proposal is actionable; older ones are superseded by later edits.
  const latest = [...(view.messages || [])].reverse().find((item) => item.role === 'assistant');
  for (const message of view.messages || []) {
    if (message.role === 'user') {
      messages.push({
        id: message.id,
        role: 'user',
        text: message.text,
        attachments: [],
        chips: [],
      });
      continue;
    }
    changeRows(message, messages);
    messages.push({
      id: message.id,
      role: 'assistant',
      text: message.text,
      attachments: [],
      chips: message.chips || [],
      turnId: message.turn_id || null,
    });
    if (message === latest) proposalQuotes(message, { cards, labels, messages, quotes });
  }
  return {
    title: view.title || '',
    version: view.version,
    cards,
    messages,
    quotes,
    busy: false,
    // The server says the latest turn can still be undone (nothing changed since).
    undoTurnId: view.undo_turn_id || null,
  };
}

const turnId = () =>
  'turn-' +
  (globalThis.crypto?.randomUUID?.() ||
    Math.random().toString(36).slice(2) + Date.now().toString(36));

export function createLiveChat({
  api,
  language = 'zh',
  projectId = null,
  labels,
  onProject,
  imageCredits = null,
  picturesEnabled = true,
}) {
  let project = projectId,
    view = null;
  const settled = {};
  const load = (value, emit, fresh = []) => {
    view = value;
    const state = fromServer(value, { ...labels, imageCredits, picturesEnabled });
    for (const [id, status] of Object.entries(settled))
      if (state.quotes[id]) state.quotes[id] = { ...state.quotes[id], status };
    emit({ type: 'chat.loaded', state, fresh });
  };
  // After a timeout or lost response, re-read the story; true if it shows the outcome.
  async function recovered(done, emit) {
    if (!project) return false;
    try {
      const value = await api.request(`/v1/story-chats/${encodeURIComponent(project)}`);
      if (!done(value)) return false;
      load(value, emit);
      return true;
    } catch {
      return false;
    }
  }
  async function ensure() {
    if (project) return project;
    const created = await api.request('/v1/story-chats', { method: 'POST', body: { language } });
    project = created.project_id;
    view = created;
    track('story_chat_story_created');
    onProject?.(project);
    return project;
  }
  // Upload every file, then add them to the story (and photos to their chosen cards) in one
  // ordinary project write. Returns what went where, for the message note.
  async function attach(ident, attachments) {
    const uploaded = [];
    for (const item of attachments)
      uploaded.push({ ...item, assetId: await uploadReady(api, item.file, ident) });
    const path = `/v1/projects/${encodeURIComponent(ident)}`;
    const current = await api.request(path);
    const actions = uploaded.flatMap((item) =>
      attachActions(current.project, {
        assetId: item.assetId,
        title: item.name,
        cardId: item.cardId,
      }),
    );
    await api.request(path + '/actions', {
      method: 'POST',
      body: { expected_version: current.version, actions },
    });
    const names = Object.fromEntries(
      fromServer(view || {}, labels).cards.map((card) => [card.id, card.name]),
    );
    return uploaded.map((item) => ({ ...item, cardName: names[item.cardId] || '' }));
  }
  return {
    live: true,
    balance: Number.NaN,
    get projectId() {
      return project;
    },
    // The admin-set picture price, read from /v1/story-chats/config when the server has it.
    set imageCredits(value) {
      imageCredits = value;
    },
    // Picture suggestions are hidden while an admin has pictures switched off.
    set picturesEnabled(value) {
      picturesEnabled = value;
    },
    async open(emit) {
      if (!project) return;
      const path = `/v1/story-chats/${encodeURIComponent(project)}`;
      try {
        load(await api.request(path), emit);
      } catch (failure) {
        // A story made in Works: attach a chat to it so it can be edited by chatting.
        if (failure.detail?.detail?.code !== 'story_chat_not_found') throw failure;
        load(await api.request(path + '/open', { method: 'POST', body: { language } }), emit);
      }
    },
    async send({ text, attachments = [] }, emit) {
      const id = turnId();
      emit({ type: 'user.message', id: id + '-u', text, attachments });
      emit({ type: 'turn.started' });
      try {
        const ident = await ensure();
        const added = attachments.length ? await attach(ident, attachments) : [];
        const note = attachmentNote(added, labels.t);
        const value = await api.request(`/v1/story-chats/${encodeURIComponent(ident)}/turns`, {
          method: 'POST',
          body: { turn_id: id, text: [text, note].filter(Boolean).join('\n\n') },
          // A model turn can take 10–30 s; never give up before the server does.
          timeoutMs: 120000,
        });
        load(value, emit, (value.turn?.changes || []).map((change) => change.id).filter(Boolean));
      } catch (failure) {
        // The server may still have finished (lost response): show what it holds.
        if (await recovered((value) => value.messages?.some((item) => item.turn_id === id), emit))
          return;
        emit({
          type: 'assistant.message',
          id: id + '-error',
          text: labels.failed?.(failure) || failure.message,
          chips: [],
        });
        emit({ type: 'turn.finished' });
      }
    },
    // The live proposal opens the story in Works, where generation is confirmed and charged.
    async settle(quote, accepted, _cards, emit) {
      settled[quote.id] = accepted ? 'confirmed' : 'declined';
      emit({ type: 'quote.settled', id: quote.id, status: settled[quote.id] });
      if (!accepted) return;
      if (quote.action.kind === 'new_story') {
        labels.startStory?.(quote.action.idea);
        return;
      }
      if (quote.action.kind !== 'image') {
        labels.openWorks?.(project, quote.action.cards?.[0], { generate: true });
        return;
      }
      // A confirmed picture: the server holds the credits, draws, and charges only when
      // the picture is on the card (#461).
      const card = quote.action.cards[0];
      emit({ type: 'card.status', id: card, status: 'generating' });
      try {
        const value = await api.request(`/v1/story-chats/${encodeURIComponent(project)}/images`, {
          method: 'POST',
          body: { request_id: quote.id, card_id: card },
          // Drawing plus storing a picture takes 15–40 s.
          timeoutMs: 180000,
        });
        load(value, emit, [card]);
      } catch (failure) {
        const before = fromServer(view || {}, labels).cards.find((item) => item.id === card)?.image;
        if (
          await recovered((value) => {
            const now = fromServer(value, labels).cards.find((item) => item.id === card)?.image;
            return now && now !== before;
          }, emit)
        )
          return;
        emit({ type: 'card.status', id: card, status: 'draft' });
        emit({
          type: 'assistant.message',
          id: quote.id + '-error',
          text: labels.failed?.(failure) || failure.message,
          chips: [],
        });
      }
    },
    propose(action, card, emit) {
      if (action.kind === 'generate') {
        labels.openWorks?.(project, card.id);
        return;
      }
      const id = 'pic-' + turnId().slice(5);
      emit({
        type: 'quote.proposed',
        id,
        action: { kind: 'image', cards: [card.id] },
        credits: imageCredits,
        label: labels.picture?.(card.name) || card.name,
      });
    },
    // A photo chosen in a card's drawer goes straight onto that card.
    async attachPhoto(cardId, file, emit) {
      if (!project) return;
      emit({ type: 'card.status', id: cardId, status: 'uploading' });
      try {
        await attach(project, [{ file, kind: 'image', name: file.name, cardId }]);
      } catch (failure) {
        emit({
          type: 'assistant.message',
          id: 'photo-' + turnId().slice(5) + '-error',
          text: labels.failed?.(failure) || failure.message,
          chips: [],
        });
      }
      load(await api.request(`/v1/story-chats/${encodeURIComponent(project)}`), emit, [cardId]);
    },
    // A card edit from the drawer goes through the ordinary project actions API.
    // Rename the story itself through the ordinary project actions.
    // Undo the latest turn; the server refuses if anything changed the story since.
    async undo(id, emit) {
      if (!project || !id) return false;
      try {
        load(
          await api.request(`/v1/story-chats/${encodeURIComponent(project)}/undo`, {
            method: 'POST',
            body: { turn_id: id },
          }),
          emit,
        );
        return true;
      } catch {
        load(await api.request(`/v1/story-chats/${encodeURIComponent(project)}`), emit);
        return false;
      }
    },
    async rename(title, emit) {
      const value = title.trim();
      if (!project || !value || value === view?.title) return;
      try {
        await api.request(`/v1/projects/${encodeURIComponent(project)}/actions`, {
          method: 'POST',
          body: {
            expected_version: view.version,
            actions: [{ op: 'project.update', patch: { title: value.slice(0, 160) } }],
          },
        });
      } finally {
        load(await api.request(`/v1/story-chats/${encodeURIComponent(project)}`), emit);
      }
    },
    async edit(id, patch, emit) {
      const card = fromServer(view, labels).cards.find((item) => item.id === id);
      if (!card || !project) return;
      const path = `/v1/projects/${encodeURIComponent(project)}`;
      const change = {};
      let expected = view.version;
      if (card.kind === 'shot') {
        const data = {};
        if ('text' in patch) {
          data.prompt = patch.text;
          change.description = patch.text;
        }
        if ('refs' in patch) {
          // Merge into the stored storyboard so explicit generation choices survive.
          const current = await api.request(path);
          expected = current.version;
          const entity = current.project.entities.find((item) => item.id === id);
          const kinds = Object.fromEntries(
            current.project.entities.map((item) => [item.id, item.type]),
          );
          const places = patch.refs.filter((ref) => kinds[ref] === 'location');
          // Keep an existing explicit look; new characters use the default look.
          const looks = Object.fromEntries(
            (entity?.data?.cast || []).map((c) =>
              typeof c === 'string' ? [c, ''] : [c.characterId, c.lookId || ''],
            ),
          );
          data.cast = patch.refs
            .filter((ref) => kinds[ref] === 'character')
            .map((ref) => ({ characterId: ref, lookId: looks[ref] || '' }));
          data.storyboard = {
            version: 1,
            mode: 'ref',
            ...(entity?.data?.storyboard || {}),
            place: places.length ? { locationId: places[places.length - 1] } : null,
            propIds: patch.refs.filter((ref) => kinds[ref] === 'prop'),
          };
        }
        if (Object.keys(data).length) change.data = data;
      } else {
        if ('name' in patch) change.title = patch.name;
        if ('text' in patch) change.description = patch.text;
      }
      if (!Object.keys(change).length) return;
      try {
        await api.request(path + '/actions', {
          method: 'POST',
          body: {
            expected_version: expected,
            actions: [{ op: 'entity.update', entity_id: id, patch: change }],
          },
        });
      } finally {
        // Success or a version conflict: either way show what the server now holds.
        load(await api.request(`/v1/story-chats/${encodeURIComponent(project)}`), emit, [id]);
      }
    },
    edited() {},
    close() {},
  };
}
