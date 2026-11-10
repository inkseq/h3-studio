// A story chat turn as server-sent events (h3-studio#1083): the reply shows while it is being
// written, and the turn resolves with the same body as the plain POST. The line handling follows
// the WHATWG event-stream rules, as eventsource-parser and fetch-event-source do; no dependency.

// A model turn can take 10–30 s; never give up before the server does.
export const TURN_TIMEOUT_MS = 120000;
const POLL_MS = 3000;
// The route answered without a stream (an older server): the turn never ran.
export const NO_STREAM = Symbol('story-chat-no-stream');
// The stream stopped before `done` or `error`: the turn may still be running on the server.
export const DROPPED = Symbol('story-chat-stream-dropped');

// Feed it text or bytes in any chunks; it calls onEvent({ event, data }) per complete event.
export function sseParser(onEvent) {
  const decoder = new TextDecoder();
  let buffer = '',
    event = '',
    data = [];
  const line = (text) => {
    if (text === '') {
      if (data.length) onEvent({ event: event || 'message', data: data.join('\n') });
      event = '';
      data = [];
      return;
    }
    if (text.startsWith(':')) return; // keep-alive or comment
    const colon = text.indexOf(':');
    const field = colon < 0 ? text : text.slice(0, colon);
    const value = colon < 0 ? '' : text.slice(colon + 1).replace(/^ /, '');
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  };
  return {
    push(chunk) {
      buffer += typeof chunk === 'string' ? chunk : decoder.decode(chunk, { stream: true });
      // A trailing CR may be the first half of CRLF: wait for the next chunk.
      const cut = buffer.endsWith('\r') ? buffer.length - 1 : buffer.length;
      const lines = buffer.slice(0, cut).split(/\r\n|\r|\n/);
      buffer = lines.pop() + buffer.slice(cut);
      for (const text of lines) line(text);
    },
  };
}

const parsed = (data) => {
  try {
    return JSON.parse(data);
  } catch {
    return null;
  }
};

// One event of a turn: report progress, or settle the outcome ({ value } or { error }).
function turnEvent({ event, data }, { refuse, onReply, onProgress }) {
  const value = parsed(data);
  if (!value || typeof value !== 'object') return null;
  if (event === 'reply' && typeof value.text === 'string') onReply?.(value.text);
  if (event === 'progress' && typeof value.step === 'string') onProgress?.(value.step);
  if (event === 'done') return { value };
  if (event === 'error') {
    const status = Number.isInteger(value.status) ? value.status : 500;
    // The plain POST's error body is { detail }; keep the same shape for the same handling.
    return { error: refuse(status, { detail: value.detail }) };
  }
  return null;
}

// Read one turn's stream: the `done` body, the server's refusal thrown as the plain POST's
// error, NO_STREAM or DROPPED.
export async function readTurn(response, { signal, refuse }, handlers = {}) {
  const type = response.headers.get('Content-Type') || '';
  if (!response.body || !type.startsWith('text/event-stream')) return NO_STREAM;
  let outcome = null;
  const parser = sseParser((event) => {
    outcome ||= turnEvent(event, { refuse, ...handlers });
  });
  const reader = response.body.getReader();
  try {
    while (!outcome) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.push(value);
    }
  } catch (error) {
    // A deadline or a cancelled page is the request's own error; a dropped stream falls back.
    if (signal?.aborted) throw error;
    return DROPPED;
  } finally {
    reader.cancel().catch(() => {});
  }
  if (outcome?.error) throw outcome.error;
  return outcome ? outcome.value : DROPPED;
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// After a dropped stream, wait for the server to store the turn (its worker carries on without
// us) rather than asking again at once, which would run the model a second time.
async function storedTurn(api, chatPath, turnId, deadline, { wait = pause, now = Date.now }) {
  while (now() < deadline) {
    await wait(POLL_MS);
    try {
      const view = await api.request(chatPath);
      if (view?.messages?.some((item) => item.turn_id === turnId)) return view;
    } catch (failure) {
      if (failure?.quiet) throw failure;
    }
  }
  return DROPPED;
}

// Send one turn through the stream. The plain POST with the SAME turn id is the fallback: at
// once when the stream route is missing, or after a dropped stream once the wait is over. The
// server replays a stored turn by id, so the fallback never applies a turn twice.
export async function sendTurn(api, chatPath, body, options = {}) {
  const turns = chatPath + '/turns';
  const post = { method: 'POST', body, timeoutMs: TURN_TIMEOUT_MS };
  const { now = Date.now } = options;
  const deadline = now() + TURN_TIMEOUT_MS;
  let value = NO_STREAM;
  try {
    value = await api.request(turns + '/stream', {
      ...post,
      read: (response, context) => readTurn(response, context, options),
    });
  } catch (failure) {
    if (failure?.status !== 404 && failure?.status !== 405) throw failure;
  }
  if (value === DROPPED) value = await storedTurn(api, chatPath, body.turn_id, deadline, options);
  return value === NO_STREAM || value === DROPPED ? api.request(turns, post) : value;
}
