import test from 'node:test';
import assert from 'node:assert/strict';
import { createCloudClient } from './cloud-client.js';
import { applyEvent, emptyChat } from './story-chat-model.js';
import { DROPPED, readTurn, sendTurn, sseParser } from './story-chat-stream.js';

const encoder = new TextEncoder();
const sse = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

// A text/event-stream response that sends these byte chunks, then ends (or fails).
function streamResponse(chunks, { fail = false, type = 'text/event-stream' } = {}) {
  const body = new ReadableStream({
    start(controller) {
      for (const chunk of chunks)
        controller.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : chunk);
      if (fail) controller.error(new TypeError('network error'));
      else controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { 'Content-Type': type } });
}

// Split bytes at every given offset, so lines and characters break across chunks.
const splitAt = (bytes, offsets) =>
  [0, ...offsets].map((start, index, all) => bytes.slice(start, all[index + 1] ?? bytes.length));
const byteLength = (text) => encoder.encode(text).length;

test('the event parser joins lines and characters split across chunks', () => {
  const events = [];
  const parser = sseParser((event) => events.push(event));
  const text = ': keep-alive\n\n' + sse('reply', { text: '你好，世界 🎬' }) + ': keep-alive\n\n';
  const bytes = encoder.encode(text + sse('done', { ok: true }));
  // Cut inside "event:", inside the JSON, inside a multi-byte character and between \n\n.
  const cut = [3, 20, byteLength(text.slice(0, text.indexOf('世'))) + 1, byteLength(text) - 1];
  for (const chunk of splitAt(bytes, cut)) parser.push(chunk);
  assert.deepEqual(events, [
    { event: 'reply', data: '{"text":"你好，世界 🎬"}' },
    { event: 'done', data: '{"ok":true}' },
  ]);
});

test('the event parser handles CRLF split between chunks and multi-line data', () => {
  const events = [];
  const parser = sseParser((event) => events.push(event));
  for (const chunk of [
    'event: progress\r',
    '\ndata: {"step":\r\n',
    'data: "thinking"}\r',
    '\n\r\n',
  ])
    parser.push(chunk);
  assert.deepEqual(events, [{ event: 'progress', data: '{"step":\n"thinking"}' }]);
});

test('a streamed turn reports the reply and progress, then resolves with the done body', async () => {
  const replies = [],
    steps = [];
  const view = { version: 3, turn: { turn_id: 't1', changes: [] } };
  const response = streamResponse([
    sse('progress', { step: 'thinking' }),
    ': keep-alive\n\n',
    sse('reply', { text: '好' }),
    sse('reply', { text: '好的，' }),
    sse('progress', { step: 'applying' }),
    sse('done', view),
  ]);
  const value = await readTurn(
    response,
    { refuse: () => assert.fail('no refusal') },
    { onReply: (text) => replies.push(text), onProgress: (step) => steps.push(step) },
  );
  assert.deepEqual(value, view);
  assert.deepEqual(replies, ['好', '好的，']);
  assert.deepEqual(steps, ['thinking', 'applying']);
});

test('an incomplete or non-stream answer has an unknown outcome', async () => {
  const context = { signal: new AbortController().signal, refuse: () => assert.fail() };
  assert.equal(await readTurn(streamResponse([sse('reply', { text: 'a' })]), context), DROPPED);
  assert.equal(await readTurn(streamResponse(['event: done\ndata: {"a"'], {}), context), DROPPED);
  assert.equal(await readTurn(streamResponse([], { fail: true }), context), DROPPED);
  assert.equal(await readTurn(streamResponse(['{}'], { type: 'text/html' }), context), DROPPED);
  assert.equal(await readTurn(new Response(null, { status: 200 }), context), DROPPED);
});

// The plain POST's refusal and the stream's `error` event become the same error.
test('an error event is the same error as the refused plain POST', async () => {
  const detail = { code: 'story_chat_busy' };
  const plain = createCloudClient({
    fetcher: async () =>
      new Response(JSON.stringify({ detail }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
  });
  const streamed = createCloudClient({
    fetcher: async (path) =>
      path.endsWith('/stream')
        ? streamResponse([
            sse('progress', { step: 'thinking' }),
            sse('error', { status: 409, detail }),
          ])
        : assert.fail('no fallback after a refusal'),
  });
  const body = { turn_id: 'turn-1', text: 'hi' };
  const expected = await plain.request('/v1/story-chats/p1/turns', { method: 'POST', body }).then(
    () => assert.fail(),
    (error) => error,
  );
  const actual = await sendTurn(streamed, '/v1/story-chats/p1', body).then(
    () => assert.fail(),
    (error) => error,
  );
  assert.equal(actual.name, 'CloudError');
  assert.equal(actual.status, expected.status);
  assert.equal(actual.message, expected.message);
  assert.deepEqual(actual.detail, expected.detail);
  assert.equal(actual.detail.detail.code, 'story_chat_busy');
});

// A fake api: the stream route's answer, and a record of every other call.
function fakeApi(stream, { stored = () => false } = {}) {
  const calls = [];
  return {
    calls,
    async request(path, options = {}) {
      calls.push({ path, method: options.method || 'GET', body: options.body });
      if (path.endsWith('/stream')) {
        const response = await stream();
        return options.read(response, { signal: new AbortController().signal, refuse: () => {} });
      }
      if (options.method === 'POST') return { posted: options.body.turn_id };
      return stored(calls) ? { messages: [{ turn_id: 'turn-9' }] } : { messages: [] };
    },
  };
}
const body = { turn_id: 'turn-9', text: 'hi' };

test('a missing stream route falls back to the plain POST with the same turn id', async () => {
  for (const status of [404, 405]) {
    const api = fakeApi(() => {
      throw Object.assign(new Error('not here'), { status });
    });
    assert.deepEqual(await sendTurn(api, '/v1/story-chats/p1', body), { posted: 'turn-9' });
    assert.deepEqual(
      api.calls.map((call) => [call.method, call.path, call.body?.turn_id]),
      [
        ['POST', '/v1/story-chats/p1/turns/stream', 'turn-9'],
        ['POST', '/v1/story-chats/p1/turns', 'turn-9'],
      ],
    );
  }
});

test('other refusals of the stream route are not retried', async () => {
  const api = fakeApi(() => {
    throw Object.assign(new Error('limit'), { status: 429 });
  });
  await assert.rejects(sendTurn(api, '/v1/story-chats/p1', body), /limit/);
  assert.equal(api.calls.length, 1);
});

test('a dropped stream waits for the stored turn instead of asking the model again', async () => {
  const wait = async () => {};
  const api = fakeApi(() => streamResponse([sse('progress', { step: 'thinking' })]), {
    stored: (calls) => calls.length >= 3,
  });
  const value = await sendTurn(api, '/v1/story-chats/p1', body, { wait });
  assert.deepEqual(value, { messages: [{ turn_id: 'turn-9' }] });
  assert.deepEqual(
    api.calls.map((call) => [call.method, call.path]),
    [
      ['POST', '/v1/story-chats/p1/turns/stream'],
      ['GET', '/v1/story-chats/p1'],
      ['GET', '/v1/story-chats/p1'],
    ],
  );
});

test('unknown turns are reconciled with reads and never reposted after the wait expires', async () => {
  for (const response of [
    () => streamResponse([], { fail: true }),
    () => streamResponse(['<html>'], { type: 'text/html' }),
    () => streamResponse(['{}'], { type: 'application/json' }),
    () => new Response(null, { status: 200 }),
    () => {
      throw new TypeError('connection lost before headers');
    },
  ]) {
    let clock = 0;
    const api = fakeApi(response);
    await assert.rejects(
      sendTurn(api, '/v1/story-chats/p1', body, {
        wait: async (ms) => {
          clock += ms;
        },
        now: () => clock,
      }),
      (error) => {
        assert.deepEqual(error.detail.detail, {
          code: 'story_chat_turn_unknown',
          turn_id: body.turn_id,
        });
        return true;
      },
    );
    assert.deepEqual(
      api.calls.filter((call) => call.method === 'POST').map((call) => call.path),
      ['/v1/story-chats/p1/turns/stream'],
    );
    assert.ok(api.calls.length > 3);
  }
});

test('the chat shows the reply being written until the server view replaces it', () => {
  let state = applyEvent(emptyChat(), { type: 'turn.started' });
  state = applyEvent(state, { type: 'turn.progress', step: 'thinking' });
  state = applyEvent(state, { type: 'turn.reply', text: '好的' });
  assert.equal(state.draft, '好的');
  assert.equal(state.step, 'thinking');
  const loaded = applyEvent(state, {
    type: 'chat.loaded',
    state: { messages: [{ id: 'm1', role: 'assistant', text: '好的，已添加。' }], busy: false },
  });
  assert.equal(loaded.draft, '');
  assert.equal(loaded.busy, false);
  // A late reply after the turn finished changes nothing.
  assert.equal(applyEvent(loaded, { type: 'turn.reply', text: 'late' }), loaded);
  assert.equal(applyEvent(state, { type: 'turn.finished' }).draft, '');
});
