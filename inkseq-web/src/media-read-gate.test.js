import test from 'node:test';
import assert from 'node:assert/strict';
import { createMediaReadGate, nativeContentRequest } from './media-read-gate.js';
const ORIGIN = 'https://inkseq.example';
const path = (n) => `/v1/assets/media${n}/content?expected_account=superdan`;
const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { resolve, promise };
};

test('only same-origin asset/artifact content GET and HEAD use the gate', () => {
  for (const value of [path(1), '/v1/artifacts/abc/content?download=1&expected_account=superdan']) {
    assert.equal(nativeContentRequest(value, ORIGIN), true);
    assert.equal(nativeContentRequest(value, ORIGIN, { method: 'HEAD' }), true);
    assert.equal(nativeContentRequest(value, ORIGIN, { method: 'POST' }), false);
  }
  for (const value of [
    'https://r2.example' + path(1),
    '/v1/projects',
    '/api/auth/me',
    '/v1/assets/abc/content/other',
  ])
    assert.equal(nativeContentRequest(value, ORIGIN), false);
});

test('a burst of local content streams stays at two actual in-flight reads and keeps headers/ranges unchanged', async () => {
  let active = 0,
    maximum = 0;
  const calls = [];
  const gate = createMediaReadGate({
    origin: ORIGIN,
    fetcher: async (value, init) => {
      calls.push({ value, init });
      active++;
      maximum = Math.max(active, maximum);
      let sent = false;
      return new Response(
        new ReadableStream({
          pull(c) {
            if (!sent) {
              sent = true;
              c.enqueue(new TextEncoder().encode('ab'));
            } else {
              active--;
              c.close();
            }
          },
        }),
        {
          status: 206,
          headers: {
            'Content-Range': 'bytes 0-1/10',
            'Content-Length': '2',
            'X-Authenticated-Account': 'superdan',
          },
        },
      );
    },
  });
  const originals = Array.from(
    { length: 12 },
    (_, n) =>
      new Request(ORIGIN + path(n), {
        headers: { Range: 'bytes=0-1', 'X-Expected-Account': 'superdan' },
      }),
  );
  await Promise.all(
    originals.map(async (original) => {
      const response = await gate(original);
      assert.equal(response.status, 206);
      assert.equal(response.headers.get('Content-Range'), 'bytes 0-1/10');
      assert.equal(response.headers.get('X-Authenticated-Account'), 'superdan');
      assert.equal(await response.text(), 'ab');
    }),
  );
  assert.equal(maximum, 2);
  assert.equal(active, 0);
  assert.equal(calls.length, 12);
  for (let n = 0; n < 12; n++) {
    assert.equal(calls[n].value, originals[n]);
    assert.equal(calls[n].value.headers.get('Range'), 'bytes=0-1');
    assert.equal(calls[n].value.headers.get('X-Expected-Account'), 'superdan');
  }
});

test('local GET slots last through body EOF while HEAD releases immediately', async () => {
  const calls = [];
  const gate = createMediaReadGate({
    origin: ORIGIN,
    fetcher: async (value, init) => {
      calls.push(value);
      return init.method === 'HEAD' ? new Response(null) : new Response('local');
    },
  });
  const first = await gate(path(1)),
    second = await gate(path(2));
  let started = false;
  const third = gate(path(3), { method: 'HEAD' }).then((r) => {
    started = true;
    return r;
  });
  await tick();
  assert.equal(started, false);
  assert.equal(calls.length, 2);
  await first.text();
  await third;
  assert.equal(calls.length, 3);
  await second.text();
  const fourth = await gate(path(4), { method: 'HEAD' });
  assert.equal(fourth.status, 200);
});

test('R2 redirected/opaque final responses release the app hop at headers without rewriting their bodies', async () => {
  const originals = [];
  const gate = createMediaReadGate({
    origin: ORIGIN,
    fetcher: async () => {
      const response = new Response(new ReadableStream({ start() {} }), {
        headers: { 'Content-Type': 'video/mp4' },
      });
      Object.defineProperties(response, {
        url: { value: 'https://private-r2.example/object' },
        redirected: { value: true },
        type: { value: originals.length % 2 ? 'opaque' : 'cors' },
      });
      originals.push(response);
      return response;
    },
  });
  const responses = await Promise.all(Array.from({ length: 6 }, (_, n) => gate(path(n))));
  assert.equal(responses.length, 6);
  for (let n = 0; n < 6; n++) {
    assert.equal(responses[n], originals[n]);
    await responses[n].body.cancel();
  }
});

test('the bounded queue rejects overflow/expiry without fetching and removes aborted waiters', async () => {
  const calls = [];
  const gate = createMediaReadGate({
    origin: ORIGIN,
    maxActive: 1,
    maxQueued: 1,
    queueMs: 10,
    fetcher: async (value) => {
      calls.push(value);
      return new Response('local');
    },
  });
  const first = await gate(path(1)),
    abort = new AbortController(),
    second = gate(new Request(ORIGIN + path(2), { signal: abort.signal }));
  const overflow = await gate(path(3));
  assert.equal(overflow.status, 429);
  assert.equal(overflow.headers.get('Cache-Control'), 'no-store');
  assert.equal((await overflow.json()).code, 'media_read_queue_busy');
  assert.equal(calls.length, 1);
  abort.abort();
  await assert.rejects(second, (error) => error.name === 'AbortError');
  const expired = await gate(path(4));
  assert.equal(expired.status, 429);
  assert.equal(calls.length, 1);
  const next = gate(path(5));
  await first.text();
  assert.equal(await (await next).text(), 'local');
  assert.equal(calls.length, 2);
});

test('cancelling a local body releases only after upstream cancellation settles', async () => {
  const ack = deferred(),
    calls = [];
  const gate = createMediaReadGate({
    origin: ORIGIN,
    maxActive: 1,
    fetcher: async (value) => {
      calls.push(value);
      return calls.length === 1
        ? new Response(
            new ReadableStream({
              cancel() {
                return ack.promise;
              },
            }),
          )
        : new Response('next');
    },
  });
  const first = await gate(path(1)),
    next = gate(path(2));
  const cancellation = first.body.cancel();
  await tick();
  assert.equal(calls.length, 1);
  ack.resolve();
  await cancellation;
  assert.equal(await (await next).text(), 'next');
  assert.equal(calls.length, 2);
});

test('active abort without a consumer and stream/fetch failures release their slots', async () => {
  const ack = deferred(),
    calls = [],
    abort = new AbortController();
  const gate = createMediaReadGate({
    origin: ORIGIN,
    maxActive: 1,
    fetcher: async (value, _init) => {
      calls.push(value);
      if (calls.length === 1)
        return new Response(
          new ReadableStream({
            cancel() {
              return ack.promise;
            },
          }),
        );
      if (calls.length === 2)
        return new Response(
          new ReadableStream({
            start(c) {
              c.error(Error('body failed'));
            },
          }),
        );
      if (calls.length === 3) throw Error('fetch failed');
      return new Response('done');
    },
  });
  const first = await gate(new Request(ORIGIN + path(1), { signal: abort.signal })),
    next = gate(path(2));
  abort.abort();
  await tick();
  assert.equal(calls.length, 1);
  ack.resolve();
  const failed = await next;
  await assert.rejects(failed.text(), /body failed/);
  await assert.rejects(first.text(), (error) => error.name === 'AbortError');
  await assert.rejects(gate(path(3)), /fetch failed/);
  assert.equal(await (await gate(path(4))).text(), 'done');
});

test('original init/signal and non-content requests pass through unchanged, with no automatic retry', async () => {
  const calls = [],
    controller = new AbortController(),
    init = {
      method: 'HEAD',
      redirect: 'error',
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal,
      headers: { 'X-Expected-Account': 'supervan' },
    };
  const gate = createMediaReadGate({
    origin: ORIGIN,
    fetcher: async (value, options) => {
      calls.push({ value, options });
      return new Response(null, { status: 429 });
    },
  });
  assert.equal((await gate(path(1), init)).status, 429);
  assert.equal(calls[0].options, init);
  assert.equal(calls[0].options.signal, controller.signal);
  const outside = '/v1/generation-availability';
  await gate(outside, init);
  assert.equal(calls[1].value, outside);
  assert.equal(calls[1].options, init);
  assert.equal(calls.length, 2);
});

test('the actual worker flow-controls native asset GET/HEAD and streams when the cache is unavailable', async () => {
  const previousSelf = globalThis.self,
    previousFetch = globalThis.fetch,
    previousDB = globalThis.indexedDB,
    handlers = new Map(),
    calls = [];
  globalThis.self = {
    location: { origin: ORIGIN },
    addEventListener(type, handler) {
      handlers.set(type, handler);
    },
  };
  globalThis.indexedDB = {
    open() {
      throw Error('cache storage unavailable');
    },
  };
  globalThis.fetch = async (value, init = {}) => {
    calls.push({ value, init });
    return new Response((init.method || value.method) === 'HEAD' ? null : 'asset', {
      headers: { 'X-Authenticated-Account': 'superdan' },
    });
  };
  try {
    await import('./video-cache-worker.js?asset-gate-integration');
    const values = await Promise.all(
      ['GET', 'GET', 'HEAD', 'GET'].map(async (method, n) => {
        let response;
        const original = new Request(ORIGIN + path(n), {
          method,
          headers: { 'X-Expected-Account': 'superdan' },
        });
        handlers.get('fetch')({
          request: original,
          respondWith(value) {
            response = value;
          },
          waitUntil() {
            throw Error('assets must not enqueue cache writes');
          },
        });
        assert.ok(response);
        const result = await response;
        assert.equal(result.headers.get('X-Authenticated-Account'), 'superdan');
        assert.equal(result.headers.get('X-Inkseq-Video-Cache'), null);
        return result.text();
      }),
    );
    assert.deepEqual(values, ['asset', 'asset', '', 'asset']);
    // Each asset GET first rechecks its owner with HEAD (uploads are now cacheable);
    // without IndexedDB the original response still streams through the gate.
    assert.equal(calls.length, 7);
    assert.equal(
      calls.filter((call) => (call.init.method || call.value.method) === 'HEAD').length,
      4,
    );
    for (const call of calls)
      assert.equal(
        new Headers(call.init.headers || call.value.headers).get('X-Expected-Account'),
        'superdan',
      );
  } finally {
    globalThis.self = previousSelf;
    globalThis.fetch = previousFetch;
    if (previousDB === undefined) delete globalThis.indexedDB;
    else globalThis.indexedDB = previousDB;
  }
});
