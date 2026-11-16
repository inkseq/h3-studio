// Live story chat (#422): the same interface as story-chat-preview.js, backed by
// /v1/story-chats. The server owns the transcript and cards; every reply replaces the local
// state with the server's view. Paid generation is never started here: a proposal hands the
// story to Works, where the existing preflight, quote and confirmation run.
import { track } from './analytics.js';
import { attachActions, attachmentNote, turnPhotos, uploadReady } from './story-chat-attach.js';
import { sendTurn } from './story-chat-stream.js';

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

// Advice on the proposed shots (h3-studio#628): one row per warning, in shot order, with the
// shot's number. Advice made for an older draft (stale) or with unknown codes is not shown;
// the server text is shown as text only, never acted on.
const RISK_CODES = ['simultaneous_speakers', 'speech_too_dense', 'action_density', 'plot_density'];
export function riskWarnings(review, cards) {
  if (!review || review.version !== 1 || review.status === 'stale') return [];
  const order = new Map(cards.filter((c) => c.kind === 'shot').map((c) => [c.id, c.order]));
  const seen = new Set();
  return (Array.isArray(review.warnings) ? review.warnings : [])
    .filter((w) => RISK_CODES.includes(w?.code) && order.has(w.shot_id))
    .filter((w) => {
      const key = w.shot_id + ' ' + w.code;
      return !seen.has(key) && seen.add(key);
    })
    .map((w) => ({
      shotId: w.shot_id,
      order: order.get(w.shot_id),
      code: w.code,
      suggestion: typeof w.suggestion === 'string' ? w.suggestion.trim().slice(0, 500) : '',
    }))
    .sort((a, b) => a.order - b.order || RISK_CODES.indexOf(a.code) - RISK_CODES.indexOf(b.code));
}

function pictureAction(proposal, cards, requests) {
  return {
    kind: 'image',
    cards,
    ...(cards.length === 1 ? {} : { requests }),
    // Attached photos stay bound to the picture proposed from them (#726).
    ...(proposal?.picture_sources ? { sources: proposal.picture_sources } : {}),
  };
}

// The newest reply's proposals become confirm cards: pictures, a new story, generation.
const pictureRequest = (message, card) =>
  `pic-${message.id.slice(-14)}-${card.slice(-14)}`.replace(/[^A-Za-z0-9_-]/g, '');

// Shots the reply offers to make: the ones it names, or, when it only suggests pictures,
// every shot that has no take yet, so the pictures lead straight on to the video.
// A shot that already has a take is never offered again, so a reload cannot pay for it twice.
function offeredShots(proposal, cards) {
  const draft = cards
    .filter((card) => card.kind === 'shot' && card.status === 'draft')
    .map((card) => card.id);
  if (proposal.shot_ids?.length) return proposal.shot_ids.filter((id) => draft.includes(id));
  return proposal.picture_card_ids?.length ? draft : [];
}

// The video offer, with the pictures it still has to draw: the suggested ones and any card the
// shots use that has none. A shot whose character has a voice sample but no picture cannot be
// made (h3-studio#1244), so the offer draws them first, with the same stable request ids.
function videoQuote(message, offered, { cards, labels, started, draw }) {
  const proposal = message.proposal || {};
  const used = new Set(
    cards.filter((card) => offered.includes(card.id)).flatMap((card) => card.refs || []),
  );
  const missing = cards
    .filter((card) => card.kind !== 'shot' && used.has(card.id) && !card.image)
    .map((card) => card.id);
  const wanted = draw ? [...new Set([...(proposal.picture_card_ids || []), ...missing])] : [];
  const requests = wanted.map((card) => pictureRequest(message, card));
  const pictures = wanted.filter((_card, index) => !started.has(requests[index]));
  return {
    id: `${message.id}-q`,
    live: true,
    status: 'open',
    action: {
      kind: 'generate',
      cards: offered,
      pictures,
      requests: requests.filter((request) => !started.has(request)),
      ...(proposal.picture_sources ? { sources: proposal.picture_sources } : {}),
    },
    credits:
      pictures.length && Number.isFinite(labels.imageCredits)
        ? labels.imageCredits * pictures.length
        : undefined,
    label: labels.generate?.(offered.length) || '',
    risks: riskWarnings(proposal.risk_review, cards),
  };
}

// Every shot the reply offered already has a take: the offer stays, saying so, instead of
// vanishing the moment the last take arrives.
function doneQuote(message, proposal, cards, labels) {
  const shots = cards.filter((card) => card.kind === 'shot');
  const named = proposal.shot_ids?.length
    ? proposal.shot_ids
    : proposal.picture_card_ids?.length &&
        shots.length &&
        shots.every((card) => card.status !== 'draft')
      ? shots.map((card) => card.id)
      : [];
  if (!named.length) return null;
  return {
    id: `${message.id}-q`,
    live: true,
    status: 'made',
    made: { done: named.length, total: named.length, failed: 0 },
    action: { kind: 'generate', cards: named, pictures: [], requests: [] },
    label: labels.generate?.(named.length) || '',
  };
}

function proposalQuotes(message, { cards, labels, messages, quotes, started }) {
  const proposal = message.proposal || {};
  const draw = labels.picturesEnabled !== false;
  const offered = offeredShots(proposal, cards);
  const madeQuote = !offered.length && doneQuote(message, proposal, cards, labels);
  if (madeQuote) {
    quotes[madeQuote.id] = madeQuote;
    messages.push({ id: madeQuote.id, role: 'quote', quoteId: madeQuote.id });
  }
  // Suggested card pictures: one confirm card for all of them, drawn one at a time so each
  // follows the first picture's style (#1111). Each card keeps its own stable request id,
  // so a replay never draws or charges twice, and a reload shows it as already confirmed.
  // Pictures alone are offered only for a story with no shots to make with them.
  const pictures = draw && !offered.length && !madeQuote ? proposal.picture_card_ids || [] : [];
  if (pictures.length) {
    const requests = pictures.map((card) => pictureRequest(message, card));
    const id = pictures.length === 1 ? requests[0] : `pics-${message.id.slice(-20)}`;
    const names = pictures.map((card) => cards.find((item) => item.id === card)?.name || '');
    quotes[id] = {
      id,
      status: requests.every((request) => started.has(request)) ? 'confirmed' : 'open',
      action: pictureAction(message.proposal, pictures, requests),
      credits: Number.isFinite(labels.imageCredits)
        ? labels.imageCredits * pictures.length
        : (labels.imageCredits ?? null),
      label:
        pictures.length === 1
          ? labels.picture?.(names[0]) || names[0]
          : labels.pictures?.(names.join('、'), pictures.length) || names.join('、'),
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
  if (offered.length) {
    const quote = videoQuote(message, offered, { cards, labels, started, draw });
    quotes[quote.id] = quote;
    messages.push({ id: quote.id, role: 'quote', quoteId: quote.id });
  }
}

// The photos sent with a message, shown on its bubble after a reload too
// (inkseq-design#457). Only the story's own asset paths are shown as pictures.
const sentPhotos = (message) =>
  (Array.isArray(message.photo_files) ? message.photo_files : [])
    .filter((file) => typeof file?.url === 'string' && file.url.startsWith('/v1/assets/'))
    .map((file) => ({ kind: 'image', name: String(file.name || ''), url: file.url }));

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
        attachments: sentPhotos(message),
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
    if (message === latest)
      proposalQuotes(message, {
        cards,
        labels,
        messages,
        quotes,
        started: new Set(view.image_requests || []),
      });
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

// The credits a checked plan set costs, or null when any plan is not priced in credits (the
// dollar path); the chat then hands the shots to Works, which shows any price.
export function planCredits(receipt) {
  const plans = (receipt?.items || []).map((item) => item.plan || {});
  if (!plans.length || plans.some((plan) => plan.admission !== 'credits')) return null;
  if (!plans.every((plan) => Number.isSafeInteger(plan.credits))) return null;
  return plans.reduce((sum, plan) => sum + plan.credits, 0);
}

// What a batch reply says about the priced shots: refused items (credits, limits, a changed
// shot; a 202 can still refuse some), the shots actually started, and the credits they cost.
export function batchOutcome(batch, quote) {
  const items = Array.isArray(batch?.items) ? batch.items : [];
  const index = Object.fromEntries((quote.plans || []).map((plan, at) => [plan, at]));
  const refused = items.filter(
    (item) => item.error_code || ['rejected', 'admission_blocked'].includes(item.status),
  );
  const accepted = items.filter((item) => !refused.includes(item) && item.plan_id in index);
  return {
    items,
    refused,
    started: accepted.map((item) => (quote.priced || [])[index[item.plan_id]]).filter(Boolean),
    spent: accepted.reduce((sum, item) => sum + ((quote.costs || [])[index[item.plan_id]] || 0), 0),
  };
}

// Each shot's newest job decides its state: made, failed, or still being made (also while the
// job is planned and not yet admitted). Jobs come newest first from /v1/jobs.
const MAKING = new Set([
  'planned',
  'waiting_capacity',
  'queued',
  'claimed',
  'submitting',
  'running',
  'collecting',
  'checking',
  'cancel_requested',
]);
// A shot that is being made or was made already is never priced or charged again; a job
// that only got as far as "planned" (refused at admission) does not count.
export function shotsTaken(jobs, shotIds) {
  const state = shotProgress(jobs, shotIds);
  return [...state.making.filter((shot) => state.statuses[shot] !== 'planned'), ...state.made];
}

export function shotProgress(jobs, shotIds) {
  const newest = new Map();
  for (const job of Array.isArray(jobs) ? jobs : []) {
    const shot = job?.client_ref?.shot_id;
    if (shot && shotIds.includes(shot) && !newest.has(shot)) newest.set(shot, job.status);
  }
  const of = (test) => shotIds.filter((shot) => newest.has(shot) && test(newest.get(shot)));
  const done = of((status) => status === 'succeeded');
  const made = done;
  const failed = of((status) => ['failed', 'cancelled', 'expired', 'blocked'].includes(status));
  const making = of((status) => MAKING.has(status));
  return {
    total: shotIds.length,
    done: done.length,
    failed: failed.length,
    making,
    made,
    statuses: Object.fromEntries(newest),
    finished: shotIds.length > 0 && done.length + failed.length === shotIds.length,
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
  // What this page learned about an offer (its status, checked price, progress) outlives the
  // server's view of the story, which is re-read after every change.
  const settled = {},
    extra = {};
  let closed = false,
    turning = false;
  const load = (value, emit, fresh = []) => {
    view = value;
    const state = fromServer(value, { ...labels, imageCredits, picturesEnabled });
    for (const [id, status] of Object.entries(settled))
      if (state.quotes[id]) state.quotes[id] = { ...state.quotes[id], ...extra[id], status };
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
  // One picture at a time: concurrent draws race on the story version and each would miss
  // the style of the picture before it.
  let drawing = Promise.resolve();
  const queued = (task) => {
    const run = drawing.then(task, task);
    drawing = run.catch(() => {});
    return run;
  };
  async function drawPicture(requestId, card, emit, photos = []) {
    emit({ type: 'card.status', id: card, status: 'generating' });
    try {
      const value = await api.request(`/v1/story-chats/${encodeURIComponent(project)}/images`, {
        method: 'POST',
        body: {
          request_id: requestId,
          card_id: card,
          ...(photos.length ? { photo_ids: photos } : {}),
        },
        // Drawing plus storing a picture takes 15–40 s.
        timeoutMs: 180000,
      });
      load(value, emit, [card]);
      return true;
    } catch (failure) {
      const before = fromServer(view || {}, labels).cards.find((item) => item.id === card)?.image;
      if (
        await recovered((value) => {
          const now = fromServer(value, labels).cards.find((item) => item.id === card)?.image;
          return now && now !== before;
        }, emit)
      )
        return true;
      emit({ type: 'card.status', id: card, status: 'draft' });
      emit({
        type: 'assistant.message',
        id: requestId + '-error',
        text: labels.failed?.(failure) || failure.message,
        chips: [],
      });
      return false;
    }
  }
  const toWorks = (quote, emit) => {
    if (closed) return;
    settled[quote.id] = 'confirmed';
    emit({ type: 'quote.settled', id: quote.id, status: 'confirmed' });
    labels.openWorks?.(project, quote.action.cards?.[0], { generate: true });
  };
  const projectJobs = async () => {
    const value = await api.request(
      `/v1/jobs?client_project_id=${encodeURIComponent(project)}&limit=100&offset=0`,
    );
    return Array.isArray(value) ? value : value?.jobs || [];
  };
  async function priceShots(quote, emit) {
    settled[quote.id] = 'pricing';
    emit({ type: 'quote.settled', id: quote.id, status: 'pricing' });
    let receipt, shots;
    try {
      // A shot already being made (say, after a reload) is never made a second time.
      const taken = shotsTaken(await projectJobs(), quote.action.cards);
      shots = quote.action.cards.filter((shot) => !taken.includes(shot));
      if (!shots.length) {
        settled[quote.id] = 'making';
        emit({ type: 'quote.settled', id: quote.id, status: 'making' });
        await watch(quote, emit);
        return;
      }
      const path = `/v1/projects/${encodeURIComponent(project)}`;
      const current = await api.request(path);
      receipt = await api.request(path + '/storyboard/preflights', {
        method: 'POST',
        body: { expected_version: current.version, shot_ids: shots, copies: 1 },
        // One check per offer and story version: a retry finds the same plans.
        key: `chat-${quote.id}-v${current.version}`.replace(/[^A-Za-z0-9_-]/g, '').slice(-120),
        timeoutMs: 180000,
      });
    } catch {
      toWorks(quote, emit);
      return;
    }
    const credits = planCredits(receipt);
    if (
      receipt?.closed_preflight ||
      receipt?.status !== 'completed' ||
      credits === null ||
      receipt.items.some((item) => !item.plan_id)
    ) {
      toWorks(quote, emit);
      return;
    }
    settled[quote.id] = 'priced';
    extra[quote.id] = {
      credits,
      receipt: receipt.id,
      plans: receipt.items.map((item) => item.plan_id),
      priced: receipt.items.map((item) => item.shot_id),
      costs: receipt.items.map((item) => item.plan.credits),
      version: receipt.project_version,
    };
    emit({
      type: 'quote.priced',
      id: quote.id,
      credits,
      receipt: receipt.id,
      plans: receipt.items.map((item) => item.plan_id),
      priced: receipt.items.map((item) => item.shot_id),
      costs: receipt.items.map((item) => item.plan.credits),
      version: receipt.project_version,
    });
  }
  // Watch the offer's shots: "Making…" until each take arrives, then the story is re-read so
  // the finished takes appear. Stops when every shot is finished.
  async function watch(quote, emit, shots = quote.action.cards) {
    let seen = -1;
    while (!closed) {
      let state = null;
      try {
        state = shotProgress(await projectJobs(), shots);
      } catch {
        /* The next look tries again. */
      }
      if (state && !closed) {
        extra[quote.id] = {
          ...extra[quote.id],
          made: { done: state.done, total: state.total, failed: state.failed },
        };
        emit({
          type: 'quote.progress',
          id: quote.id,
          done: state.done,
          total: state.total,
          failed: state.failed,
        });
        if (state.done + state.failed !== seen && !turning) {
          seen = state.done + state.failed;
          try {
            const value = await api.request(`/v1/story-chats/${encodeURIComponent(project)}`);
            // The story may have changed under this request, or a turn may have started.
            if (closed) return;
            if (!turning) load(value, emit);
            else seen = -1;
          } catch {
            /* The next look tries again. */
          }
        }
        for (const shot of state.making)
          emit({ type: 'card.status', id: shot, status: 'generating' });
        if (state.finished) {
          settled[quote.id] = state.failed ? 'partly' : 'made';
          emit({ type: 'quote.settled', id: quote.id, status: settled[quote.id] });
          return;
        }
      }
      await new Promise((resolve) => setTimeout(resolve, labels.pollMs ?? 15000));
    }
  }
  return {
    live: true,
    balance: Number.NaN,
    // The person confirmed the checked price: submit the plans as one batch, then watch it.
    async make(quote, emit) {
      if (!quote.plans?.length) return;
      settled[quote.id] = 'submitting';
      extra[quote.id] = { ...extra[quote.id], label: quote.label };
      emit({ type: 'quote.settled', id: quote.id, status: 'submitting' });
      // The same gate as Works: the price must be for the story as it is now, and no shot may
      // have been made or started meanwhile (another tab, Works). Otherwise check again.
      try {
        const current = await api.request(`/v1/projects/${encodeURIComponent(project)}`);
        const taken = shotsTaken(await projectJobs(), quote.priced || []);
        if (current.version !== quote.version || taken.length) {
          await priceShots(quote, emit);
          return;
        }
      } catch {
        settled[quote.id] = 'priced';
        emit({ type: 'quote.settled', id: quote.id, status: 'priced' });
        return;
      }
      let batch;
      try {
        batch = await api.request('/v1/batches', {
          method: 'POST',
          body: { client_project_id: project, plan_ids: quote.plans },
          key: `chat-batch-${quote.receipt}`.replace(/[^A-Za-z0-9_-]/g, '').slice(-120),
          timeoutMs: 180000,
        });
      } catch (failure) {
        const code = failure.detail?.code || failure.detail?.detail?.code;
        // Credits or a spending confirmation are handled in Works, with the same plans.
        if (code === 'spending_confirmation_required' || failure.status === 402) {
          toWorks(quote, emit);
          return;
        }
        settled[quote.id] = 'priced';
        emit({ type: 'quote.settled', id: quote.id, status: 'priced' });
        emit({
          type: 'assistant.message',
          id: quote.id + '-make-error-' + Date.now().toString(36),
          text: labels.failed?.(failure) || failure.message,
          chips: [],
        });
        return;
      }
      // A 202 can still refuse items (credits, limits, a changed shot): those shots are not
      // being made, so they are not watched, and Works is offered for them.
      const { items, refused, started, spent } = batchOutcome(batch, quote);
      if (refused.length)
        emit({
          type: 'assistant.message',
          id: quote.id + '-refused-' + Date.now().toString(36),
          text: labels.refused?.(refused.length, refused[0].error_code) || refused[0].error_code,
          chips: [],
        });
      if (!started.length) {
        settled[quote.id] = 'partly';
        extra[quote.id] = {
          ...extra[quote.id],
          made: { done: 0, total: items.length, failed: refused.length },
        };
        emit({
          type: 'quote.progress',
          id: quote.id,
          done: 0,
          total: items.length,
          failed: refused.length,
        });
        emit({ type: 'quote.settled', id: quote.id, status: 'partly' });
        return;
      }
      emit({ type: 'credits.spent', credits: spent });
      settled[quote.id] = 'making';
      emit({ type: 'quote.settled', id: quote.id, status: 'making' });
      track('story_chat_shots_made');
      await watch(quote, emit, started);
      if (refused.length && ['made', 'partly'].includes(settled[quote.id])) {
        const made = extra[quote.id]?.made || { done: 0, total: 0, failed: 0 };
        const whole = {
          done: made.done,
          total: made.total + refused.length,
          failed: made.failed + refused.length,
        };
        extra[quote.id] = { ...extra[quote.id], made: whole };
        emit({ type: 'quote.progress', id: quote.id, ...whole });
        settled[quote.id] = 'partly';
        emit({ type: 'quote.settled', id: quote.id, status: 'partly' });
      }
    },
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
    // While a turn runs, a watch does not re-read the story under it (h3-studio#1244).
    async send(input, emit) {
      turning = true;
      try {
        return await this.sendTurn(input, emit);
      } finally {
        turning = false;
      }
    },
    close() {
      closed = true;
    },
    // Works is the way on when the chat cannot finish (a refused or failed shot).
    openWorks(quote) {
      labels.openWorks?.(project, quote.action.cards?.[0], { generate: true });
    },
    async sendTurn({ text, attachments = [], script = false }, emit) {
      const id = turnId();
      emit({ type: 'user.message', id: id + '-u', text, attachments });
      emit({ type: 'turn.started' });
      try {
        const ident = await ensure();
        const added = attachments.length ? await attach(ident, attachments) : [];
        const note = attachmentNote(added, labels.t);
        // The assistant sees the attached photos themselves (h3-studio#726).
        const photos = turnPhotos(added);
        const body = {
          turn_id: id,
          text: [text, note].filter(Boolean).join('\n\n'),
          ...(script ? { script: true } : {}),
          ...(photos.length ? { photos } : {}),
        };
        // The reply shows while it is being written (#1083); the result is the plain POST's.
        const value = await sendTurn(api, `/v1/story-chats/${encodeURIComponent(ident)}`, body, {
          onReply: (reply) => emit({ type: 'turn.reply', text: reply }),
          onProgress: (step) => emit({ type: 'turn.progress', step }),
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
    // The video offer draws the pictures it needs, then checks the shots for their price
    // (the same check Works runs). The person confirms that price here; anything the chat
    // cannot price opens Works, which handles every case as before.
    async settle(quote, accepted, _cards, emit) {
      const drawFirst =
        accepted && quote.action.kind === 'generate' && quote.action.pictures?.length;
      settled[quote.id] = accepted ? (drawFirst ? 'working' : 'confirmed') : 'declined';
      emit({ type: 'quote.settled', id: quote.id, status: settled[quote.id] });
      if (!accepted) return;
      if (quote.action.kind === 'new_story') {
        labels.startStory?.(quote.action.idea);
        return;
      }
      if (quote.action.kind !== 'image') {
        if (drawFirst) {
          const drawn = await Promise.all(
            quote.action.pictures.map((card, index) =>
              queued(() =>
                drawPicture(quote.action.requests[index], card, emit, quote.action.sources?.[card]),
              ),
            ),
          );
          // A picture that did not arrive leaves the offer open; its error is already shown.
          settled[quote.id] = drawn.every(Boolean) ? 'confirmed' : 'open';
          emit({ type: 'quote.settled', id: quote.id, status: settled[quote.id] });
          if (settled[quote.id] !== 'confirmed') return;
        }
        await priceShots(quote, emit);
        return;
      }
      // Confirmed pictures: the server holds the credits, draws, and charges only when the
      // picture is on the card (#461). They queue behind any picture still drawing (#1112).
      const requests = quote.action.requests || [quote.id];
      // Queue the whole batch at once so it stays together, in order.
      await Promise.all(
        quote.action.cards.map((card, index) =>
          queued(() =>
            drawPicture(requests[index] || quote.id, card, emit, quote.action.sources?.[card]),
          ),
        ),
      );
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
  };
}
