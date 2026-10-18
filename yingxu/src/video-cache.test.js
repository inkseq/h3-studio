import test from 'node:test';
import assert from 'node:assert/strict';
import { byteRange, cacheKey, createVideoCache, videoTarget } from './video-cache-core.js';
import { playedVideoTarget } from './video-cache-client.js';
import { createCloudClient } from './cloud-client.js';
import { createMediaReadGate } from './media-read-gate.js';
import { evictionPlan, VIDEO_CACHE_LIMIT, VIDEO_FILE_LIMIT } from './video-cache-store.js';

const ORIGIN = 'https://inkseq.example',
  A = 'superdan',
  B = 'supervan';
const path = (id = 'v1', account = A) => `/v1/artifacts/${id}/content?expected_account=${account}`;
const request = (id = 'v1', account = A, range = null) =>
  new Request(ORIGIN + path(id, account), { headers: range ? { Range: range } : {} });
const deferred = () => {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
};

// This fake is the disposable-byte repository boundary, not an auth authority.
// Its CAS semantics model transactions shared by multiple worker instances.
function memoryStore() {
  let meta = { epoch: 0, account: null },
    clock = 0;
  const entries = new Map(),
    writes = [];
  return {
    entries,
    writes,
    async snapshot() {
      return { ...meta };
    },
    async clear(account = null) {
      meta = { epoch: meta.epoch + 1, account };
      entries.clear();
      return { ...meta };
    },
    async selectAccount(account, epoch) {
      if (epoch !== meta.epoch) return null;
      if (account !== meta.account) {
        meta = { epoch: meta.account === null && meta.epoch === 0 ? 0 : meta.epoch + 1, account };
        entries.clear();
      }
      return { ...meta };
    },
    async get(key, epoch) {
      if (epoch !== meta.epoch) return null;
      const entry = entries.get(key);
      if (entry) entry.used = ++clock;
      return entry;
    },
    async put(entry, epoch) {
      if (epoch !== meta.epoch || entry.account !== meta.account) return false;
      for (const key of evictionPlan([...entries.values()], entry).remove) entries.delete(key);
      entries.set(entry.key, { ...entry, used: ++clock });
      writes.push(entry);
      return true;
    },
    async stats(account) {
      const values = account === meta.account ? [...entries.values()] : [];
      return {
        bytes: values.reduce((n, e) => n + e.size, 0),
        count: values.length,
        limit: VIDEO_CACHE_LIMIT,
      };
    },
  };
}
function fixture({ body = '0123456789', account = A, wrapFetcher = (fetcher) => fetcher } = {}) {
  const store = memoryStore(),
    calls = [];
  let headStatus = 200,
    headAccount = account,
    headMime = 'video/mp4',
    headSize = body.length,
    offline = false,
    full = () =>
      new Response(body, {
        headers: { 'Content-Type': 'video/mp4', 'Content-Length': String(body.length) },
      }),
    beforeHead = null,
    beforeFull = null;
  const fetcher = async (value, options = {}) => {
    const url = typeof value === 'string' ? value : value.url,
      method = options.method || value.method || 'GET';
    calls.push({ url, method, options, request: value });
    if (offline) throw Error('offline');
    if (url === '/api/auth/me') return Response.json({ username: headAccount });
    if (method === 'HEAD') {
      if (beforeHead) await beforeHead();
      return new Response(null, {
        status: headStatus,
        headers: {
          'Content-Type': headMime,
          'Content-Length': String(headSize),
          'X-Authenticated-Account': headAccount,
          'Content-Disposition': url.includes('download=1')
            ? 'attachment; filename="video.mp4"'
            : 'inline; filename="video.mp4"',
        },
      });
    }
    if (beforeFull) await beforeFull();
    return full();
  };
  const raw = createVideoCache({ store, origin: ORIGIN, fetcher: wrapFetcher(fetcher) }),
    cache = {
      ...raw,
      async handle(req) {
        const background = [];
        const response = await raw.handle(req, { waitUntil: (task) => background.push(task) });
        await Promise.all(background);
        return response;
      },
    };
  return {
    store,
    calls,
    cache,
    set(values) {
      ({
        headStatus = headStatus,
        headAccount = headAccount,
        headMime = headMime,
        headSize = headSize,
        offline = offline,
        full = full,
        beforeHead = beforeHead,
        beforeFull = beforeFull,
      } = values);
    },
  };
}

test('only canonical artifact URLs enter the cache, never assets/Agent credentials/signed URLs', async () => {
  assert.deepEqual(videoTarget(ORIGIN + path(), ORIGIN), { id: 'v1', expected: A, url: path() });
  for (const url of [
    ORIGIN + '/v1/assets/v1/content',
    ORIGIN + path() + '&token=secret',
    ORIGIN + path() + '&expected_account=' + A,
    ORIGIN + path() + '#fragment',
    'https://r2.example' + path(),
    ORIGIN + '/v1/artifacts/%2e%2e/content',
  ])
    assert.equal(videoTarget(url, ORIGIN), null);
  const f = fixture();
  assert.equal(
    await f.cache.handle(
      new Request(ORIGIN + path(), { headers: { Authorization: 'Bearer api-secret' } }),
    ),
    null,
  );
  assert.equal(f.calls.length, 0);
  assert.equal(cacheKey('../account', 'v1'), null);
});

test('a readable complete GET fills once; each hit rechecks authority and serves exact ranges', async () => {
  const f = fixture();
  const first = await f.cache.handle(request());
  assert.equal(await first.text(), '0123456789');
  assert.equal(first.headers.get('X-Inkseq-Video-Cache'), null);
  assert.equal(f.calls.filter((c) => c.method === 'GET').length, 1);
  const saved = f.store.writes[0];
  assert.deepEqual(Object.keys(saved).sort(), ['account', 'blob', 'id', 'key', 'size']);
  assert.equal(saved.key, A + '\0v1');
  for (const [range, status, body, contentRange] of [
    ['bytes=2-5', 206, '2345', 'bytes 2-5/10'],
    ['bytes=-3', 206, '789', 'bytes 7-9/10'],
    ['bytes=8-', 206, '89', 'bytes 8-9/10'],
    ['bytes=0-100', 206, '0123456789', 'bytes 0-9/10'],
    ['bytes=10-', 416, '', 'bytes */10'],
    ['bytes=0-1,3-4', 416, '', 'bytes */10'],
  ]) {
    const result = await f.cache.handle(request('v1', A, range));
    assert.equal(result.status, status);
    assert.equal(await result.text(), body);
    assert.equal(result.headers.get('Content-Range'), contentRange);
    assert.equal(result.headers.get('Cache-Control'), 'private, no-store');
    assert.equal(result.headers.get('Referrer-Policy'), 'no-referrer');
  }
  assert.equal(f.calls.filter((c) => c.method === 'HEAD').length, 7);
  assert.equal(f.calls.filter((c) => c.method === 'GET').length, 1);
  for (const c of f.calls.filter((c) => c.method === 'HEAD')) {
    assert.equal(c.options.cache, 'no-store');
    assert.equal(c.options.redirect, 'error');
    assert.equal(c.options.credentials, 'same-origin');
    assert.equal(c.options.headers['X-Expected-Account'], A);
  }
  const get = f.calls.find((c) => c.method === 'GET');
  assert.equal(get.request.url, ORIGIN + path());
  assert.equal(get.request.headers.has('Authorization'), false);
});

test('range parser rejects invalid, unsafe and multipart ranges', () => {
  assert.equal(byteRange(null, 10), null);
  for (const value of [
    'bytes=-0',
    'bytes=3-2',
    'bytes=-',
    'bytes=0-1,2-3',
    'items=0-1',
    'bytes=9007199254740992-',
    'bytes=0-9007199254740992',
  ])
    assert.equal(byteRange(value, 10), false);
  assert.deepEqual(byteRange('bytes=-100', 10), { start: 0, end: 9 });
});

test('a native Range miss remains streaming and only actual play explicitly fills it', async () => {
  const f = fixture();
  let reads = 0;
  f.set({
    full: () => {
      reads++;
      return reads === 1
        ? new Response('01', {
            status: 206,
            headers: {
              'Content-Type': 'video/mp4',
              'Content-Length': '2',
              'Content-Range': 'bytes 0-1/10',
            },
          })
        : new Response('0123456789', {
            headers: { 'Content-Type': 'video/mp4', 'Content-Length': '10' },
          });
    },
  });
  const partial = await f.cache.handle(request('v1', A, 'bytes=0-1'));
  assert.equal(partial.status, 206);
  assert.equal(await partial.text(), '01');
  assert.equal(f.store.entries.size, 0);
  assert.equal(reads, 1);
  assert.equal(f.calls.filter((c) => c.method === 'HEAD').length, 0);
  assert.deepEqual(await f.cache.ensure(A, 'v1'), { cached: true });
  assert.equal(reads, 2);
  const hit = await f.cache.handle(request('v1', A, 'bytes=2-3'));
  assert.equal(await hit.text(), '23');
  assert.equal(reads, 2);
});

test('deleted, unauthenticated, foreign and unavailable results never replay private bytes', async () => {
  for (const status of [401, 403, 404, 409, 429, 500]) {
    const f = fixture();
    await f.cache.handle(request());
    f.set({ headStatus: status });
    const result = await f.cache.handle(request());
    assert.equal(result.status, [401, 403, 404, 409].includes(status) ? status : 503);
    assert.notEqual(await result.text(), '0123456789');
    assert.equal(f.store.entries.size, 0);
    assert.equal(f.calls.filter((c) => c.method === 'GET').length, 1);
  }
  const f = fixture();
  await f.cache.handle(request());
  f.set({ offline: true });
  assert.equal((await f.cache.handle(request())).status, 503);
  assert.equal(f.calls.filter((c) => c.method === 'GET').length, 1);
  f.set({ offline: false, headAccount: B });
  assert.equal((await f.cache.handle(request())).status, 409);
  assert.equal(f.store.entries.size, 0);
});

test('changing accounts clears old entries; IDs cannot cross the owner namespace', async () => {
  const f = fixture();
  await f.cache.handle(request());
  f.set({
    headAccount: B,
    full: () =>
      new Response('abcdefghij', {
        headers: { 'Content-Type': 'video/mp4', 'Content-Length': '10' },
      }),
  });
  assert.equal(await (await f.cache.handle(request('v1', B))).text(), 'abcdefghij');
  assert.deepEqual([...f.store.entries.keys()], [B + '\0v1']);
  assert.equal((await f.cache.handle(request())).status, 409);
  assert.equal(f.store.entries.size, 0);
});

test('clear/logout fences an in-flight native body without caching its late bytes', async () => {
  const f = fixture(),
    gate = deferred(),
    started = deferred();
  f.set({
    beforeFull: () => {
      started.resolve();
      return gate.promise;
    },
  });
  const old = f.cache.handle(request());
  await started.promise;
  await f.cache.clear();
  gate.resolve();
  assert.equal((await old).status, 200);
  assert.equal(f.store.entries.size, 0);
  assert.equal(f.store.writes.length, 0);
  f.set({ beforeFull: null });
  assert.equal((await f.cache.handle(request())).status, 200);
  assert.equal(f.store.entries.size, 1);
});

test('two worker instances sharing IDB semantics reject a late old-account HEAD', async () => {
  const f = fixture(),
    gate = deferred(),
    started = deferred();
  await f.cache.handle(request());
  f.set({
    beforeHead: () => {
      started.resolve();
      return gate.promise;
    },
  });
  const old = f.cache.handle(request());
  await started.promise;
  const second = createVideoCache({
    store: f.store,
    origin: ORIGIN,
    fetcher: async () => Response.json({ username: B }),
  });
  await second.identity(B);
  gate.resolve();
  assert.equal((await old).status, 409);
  assert.deepEqual(await f.store.snapshot(), { epoch: 1, account: B });
  assert.equal(f.store.entries.size, 0);
});

test('a delayed identity check cannot re-open a namespace after logout', async () => {
  const store = memoryStore(),
    gate = deferred(),
    started = deferred();
  const cache = createVideoCache({
    store,
    origin: ORIGIN,
    fetcher: async () => {
      started.resolve();
      await gate.promise;
      return Response.json({ username: A });
    },
  });
  const identity = cache.identity(A);
  await started.promise;
  await cache.clear();
  gate.resolve();
  await assert.rejects(identity, /identity changed/);
  assert.deepEqual(await store.snapshot(), { epoch: 1, account: null });
});

test('fills deduplicate per artifact and cap concurrent full downloads at two', async () => {
  const f = fixture(),
    gate = deferred(),
    started = deferred();
  f.set({
    beforeFull: () => {
      if (f.calls.filter((c) => c.method === 'GET' && c.url !== '/api/auth/me').length === 2)
        started.resolve();
      return gate.promise;
    },
  });
  // Select the account before simultaneous requests, as the page identity does.
  await f.cache.identity(A);
  const one = f.cache.ensure(A, 'one'),
    same = f.cache.ensure(A, 'one'),
    two = f.cache.ensure(A, 'two');
  await started.promise;
  assert.deepEqual(await f.cache.ensure(A, 'three'), { cached: false });
  assert.equal(f.calls.filter((c) => c.method === 'GET' && c.url !== '/api/auth/me').length, 2);
  gate.resolve();
  assert.deepEqual(await Promise.all([one, same, two]), [
    { cached: true },
    { cached: true },
    { cached: true },
  ]);
  assert.equal(f.store.entries.size, 2);
});

test('partial, opaque, wrong-type, truncated and corrupt responses are never cached', async () => {
  for (const full of [
    () =>
      new Response('01', {
        status: 206,
        headers: { 'Content-Type': 'video/mp4', 'Content-Length': '2' },
      }),
    () =>
      new Response('0123456789', {
        headers: { 'Content-Type': 'audio/mpeg', 'Content-Length': '10' },
      }),
    () =>
      new Response('01234', { headers: { 'Content-Type': 'video/mp4', 'Content-Length': '10' } }),
    () =>
      new Response('01234567890', {
        headers: { 'Content-Type': 'video/mp4', 'Content-Length': '10' },
      }),
    () => new Response('0123456789', { headers: { 'Content-Type': 'video/mp4' } }),
    () =>
      new Response('0123456789', {
        headers: {
          'Content-Type': 'video/mp4',
          'Content-Length': '10',
          'Content-Range': 'bytes 0-9/10',
        },
      }),
    () => {
      const value = new Response('0123456789', {
        headers: { 'Content-Type': 'video/mp4', 'Content-Length': '10' },
      });
      Object.defineProperty(value, 'type', { value: 'opaque' });
      return value;
    },
    () => Response.error(),
  ]) {
    const f = fixture();
    f.set({ full });
    assert.deepEqual(await f.cache.ensure(A, 'v1'), { cached: false });
    assert.equal(f.store.entries.size, 0);
  }
  const f = fixture();
  f.set({ headSize: VIDEO_FILE_LIMIT + 1 });
  assert.deepEqual(await f.cache.ensure(A, 'v1'), { cached: false });
  assert.equal(f.calls.filter((c) => c.method === 'GET').length, 0);
});

test('metadata LRU is globally bounded by bytes and count, with correct replacement', () => {
  const entry = (key, size, used) => ({ key, size, used });
  assert.equal(VIDEO_CACHE_LIMIT, 1024 ** 3);
  assert.equal(VIDEO_FILE_LIMIT, 64 * 1024 ** 2);
  assert.deepEqual(
    evictionPlan([entry('old', 4, 1), entry('newer', 4, 2)], entry('in', 4, 3), { maxBytes: 8 }),
    { remove: ['old'], bytes: 8, count: 2 },
  );
  assert.deepEqual(
    evictionPlan([entry('same', 4, 1), entry('other', 4, 2)], entry('same', 3, 3), { maxBytes: 8 }),
    { remove: [], bytes: 7, count: 2 },
  );
  assert.deepEqual(
    evictionPlan([entry('old', 1, 1), entry('newer', 1, 2)], entry('in', 1, 3), { maxEntries: 2 }),
    { remove: ['old'], bytes: 2, count: 2 },
  );
  const all = Array.from({ length: 16 }, (_, i) =>
    entry(String(i).padStart(2, '0'), VIDEO_FILE_LIMIT, i),
  );
  assert.deepEqual(evictionPlan(all, entry('17', VIDEO_FILE_LIMIT, 17)), {
    remove: ['00'],
    bytes: VIDEO_CACHE_LIMIT,
    count: 16,
  });
});

test('only opted-in non-autoplay full players may enqueue an additional complete fill', () => {
  const full = {
    tagName: 'VIDEO',
    dataset: { videoCachePlay: 'true' },
    currentSrc: ORIGIN + path(),
  };
  assert.deepEqual(playedVideoTarget(full, A, ORIGIN), { account: A, id: 'v1' });
  for (const element of [
    { ...full, dataset: {} },
    { ...full, autoplay: true },
    { ...full, loop: true },
    { ...full, dataset: { videoCachePlay: 'false' } },
    { ...full, tagName: 'IMG' },
    { ...full, currentSrc: ORIGIN + path('v1', B) },
    { ...full, currentSrc: 'blob:' + ORIGIN + '/x' },
  ])
    assert.equal(playedVideoTarget(element, A, ORIGIN), null);
  assert.equal(playedVideoTarget(full, null, ORIGIN), null);
});

test('parallel first-owner requests do not invalidate each other on an empty cache', async () => {
  const f = fixture();
  const results = await Promise.all([
    f.cache.handle(request('one')),
    f.cache.handle(request('two')),
  ]);
  assert.deepEqual(
    results.map((r) => r.status),
    [200, 200],
  );
  assert.equal(f.store.entries.size, 2);
});

test('a full readable native response streams before its cache clone completes', async () => {
  const store = memoryStore(),
    done = deferred(),
    background = [];
  let emitted = false;
  const native = new Response(
    new ReadableStream({
      async pull(controller) {
        if (!emitted) {
          emitted = true;
          controller.enqueue(new TextEncoder().encode('01234'));
          return;
        }
        await done.promise;
        controller.enqueue(new TextEncoder().encode('56789'));
        controller.close();
      },
    }),
    { headers: { 'Content-Type': 'video/mp4', 'Content-Length': '10' } },
  );
  const cache = createVideoCache({
    store,
    origin: ORIGIN,
    fetcher: async (_url, options) =>
      options?.method === 'HEAD'
        ? new Response(null, {
            headers: {
              'Content-Type': 'video/mp4',
              'Content-Length': '10',
              'X-Authenticated-Account': A,
            },
          })
        : native,
  });
  const response = await cache.handle(request(), { waitUntil: (task) => background.push(task) }),
    reader = response.body.getReader();
  assert.equal(new TextDecoder().decode((await reader.read()).value), '01234');
  assert.equal(store.entries.size, 0);
  done.resolve();
  await reader.read();
  await Promise.all(background);
  assert.equal(store.entries.size, 1);
});

test('unresolved page auth does not clear on reload; same-owner resolution retains bytes, logout clears', async () => {
  const f = fixture();
  await f.cache.handle(request());
  const epoch = (await f.store.snapshot()).epoch,
    actions = [],
    rpc = [];
  const client = await import('./video-cache-client.js?reload-boundary');
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'isSecureContext');
  Object.defineProperty(globalThis, 'isSecureContext', { value: true, configurable: true });
  const worker = {
    postMessage(data, ports) {
      actions.push(data.action);
      const task = (async () => {
        try {
          const result =
            data.action === 'identity'
              ? await f.cache.identity(data.account)
              : data.action === 'clear'
                ? await f.cache.clear()
                : await f.cache.stats(data.account);
          ports[0].postMessage({ ok: true, result });
        } catch {
          ports[0].postMessage({ ok: false });
        } finally {
          ports[0].close();
        }
      })();
      rpc.push(task);
    },
  };
  try {
    client.installVideoCache('/assets/video-cache-worker-abcdefgh.js', {
      container: { register: async () => ({ active: worker }) },
      document: { addEventListener() {} },
    });
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(actions, []);
    assert.equal(f.store.entries.size, 1);
    client.videoCacheIdentity(A);
    await new Promise((resolve) => setImmediate(resolve));
    await Promise.all(rpc);
    assert.deepEqual(actions, ['identity']);
    assert.equal(f.store.entries.size, 1);
    assert.equal((await f.store.snapshot()).epoch, epoch);
    const heads = f.calls.filter((c) => c.method === 'HEAD').length;
    assert.equal((await f.cache.handle(request())).headers.get('X-Inkseq-Video-Cache'), 'hit');
    assert.equal(f.calls.filter((c) => c.method === 'HEAD').length, heads + 1);
    client.videoCacheIdentity(null);
    await new Promise((resolve) => setImmediate(resolve));
    await Promise.all(rpc);
    assert.equal(f.store.entries.size, 0);
    assert.equal((await f.store.snapshot()).account, null);
  } finally {
    if (previous) Object.defineProperty(globalThis, 'isSecureContext', previous);
    else delete globalThis.isSecureContext;
  }
});

test('cached downloads retain fresh attachment disposition without persisting bearer headers', async () => {
  const f = fixture();
  await f.cache.handle(request());
  const response = await f.cache.handle(new Request(ORIGIN + path() + '&download=1'));
  assert.equal(response.headers.get('X-Inkseq-Video-Cache'), 'hit');
  assert.equal(response.headers.get('Content-Disposition'), 'attachment; filename="video.mp4"');
  assert.equal(await response.text(), '0123456789');
  const preview = await f.cache.handle(request());
  assert.equal(preview.headers.get('Content-Disposition'), 'inline; filename="video.mp4"');
  assert.equal(f.calls.filter((c) => c.method === 'GET').length, 1);
});

test('header-only caller fences are checked on cached GETs and preserved in their authoritative HEAD', async () => {
  const f = fixture();
  await f.cache.handle(request());
  const allowed = await f.cache.handle(
    new Request(ORIGIN + '/v1/artifacts/v1/content', { headers: { 'X-Expected-Account': A } }),
  );
  assert.equal(allowed.status, 200);
  assert.equal(allowed.headers.get('X-Inkseq-Video-Cache'), 'hit');
  assert.equal(allowed.headers.get('X-Authenticated-Account'), A);
  assert.equal(await allowed.text(), '0123456789');
  assert.equal(f.calls.at(-1).method, 'HEAD');
  assert.equal(f.calls.at(-1).options.headers['X-Expected-Account'], A);
  const denied = await f.cache.handle(
    new Request(ORIGIN + '/v1/artifacts/v1/content', { headers: { 'X-Expected-Account': B } }),
  );
  assert.equal(denied.status, 409);
  assert.notEqual(await denied.text(), '0123456789');
  assert.equal(f.calls.at(-1).options.headers['X-Expected-Account'], B);
  assert.equal(f.store.entries.size, 0);
});

test('query/header conflicts and merged duplicate headers never bypass the native context guard', async () => {
  const f = fixture();
  await f.cache.handle(request());
  const count = f.calls.length;
  for (const value of [B, '', A + ', ' + A, A + ', ' + B]) {
    const response = await f.cache.handle(
      new Request(ORIGIN + path(), { headers: { 'X-Expected-Account': value } }),
    );
    assert.equal(response.status, 400);
    assert.equal(response.headers.get('Cache-Control'), 'no-store');
    assert.equal(response.headers.get('X-Inkseq-Video-Cache'), null);
    assert.notEqual(await response.text(), '0123456789');
  }
  assert.equal(f.calls.length, count);
  const matching = await f.cache.handle(
    new Request(ORIGIN + path(), { headers: { 'X-Expected-Account': A, Range: 'bytes=1-2' } }),
  );
  assert.equal(matching.status, 206);
  assert.equal(matching.headers.get('X-Authenticated-Account'), A);
  assert.equal(await matching.text(), '12');
  for (const value of ['', A + ', ' + A, 'invalid account']) {
    const result = await f.cache.handle(
      new Request(ORIGIN + '/v1/artifacts/v1/content', {
        headers: { 'X-Expected-Account': value },
      }),
    );
    assert.equal(result.status, 409);
    assert.equal(f.calls.at(-1).options.headers['X-Expected-Account'], value);
  }
  // Invalid/duplicate query fences are deliberately outside the worker route:
  // its predicate returns null, leaving the exact original request to the API.
  for (const suffix of [
    '?expected_account=' + A + '&expected_account=' + A,
    '?expected_account=',
    '?expected_account=invalid%20account',
  ]) {
    assert.equal(videoTarget(ORIGIN + '/v1/artifacts/v1/content' + suffix, ORIGIN), null);
    assert.equal(
      await f.cache.handle(new Request(ORIGIN + '/v1/artifacts/v1/content' + suffix)),
      null,
    );
  }
});

test('a mismatched header-only Range miss retains its original server fence instead of replaying another owner', async () => {
  const f = fixture();
  await f.cache.handle(request());
  f.set({
    full: () => new Response(null, { status: 409, headers: { 'X-Authenticated-Account': A } }),
  });
  const original = new Request(ORIGIN + '/v1/artifacts/v1/content', {
      headers: { 'X-Expected-Account': B, Range: 'bytes=0-1' },
    }),
    response = await f.cache.handle(original);
  assert.equal(response.status, 409);
  assert.equal(response.headers.get('X-Inkseq-Video-Cache'), null);
  assert.equal(f.calls.at(-1).request, original);
  assert.equal(f.calls.at(-1).request.headers.get('X-Expected-Account'), B);
});

test('the real cloud download client receives fresh account/disposition headers on a cached response', async () => {
  const f = fixture();
  await f.cache.handle(request());
  const responses = [];
  const client = createCloudClient({
    fetcher: async (url, options) => {
      const response = await f.cache.handle(new Request(ORIGIN + url, options));
      responses.push(response);
      return response;
    },
  });
  client.setAccount(A);
  const blob = await client.download('/v1/artifacts/v1/content?download=1');
  assert.equal(await blob.text(), '0123456789');
  assert.equal(responses[0].headers.get('X-Authenticated-Account'), A);
  assert.equal(responses[0].headers.get('Content-Disposition'), 'attachment; filename="video.mp4"');
  assert.equal(responses[0].headers.get('X-Inkseq-Video-Cache'), 'hit');
  assert.equal(f.calls.at(-1).options.headers['X-Expected-Account'], A);
  client.setAccount(B);
  await assert.rejects(
    client.download('/v1/artifacts/v1/content'),
    (error) => error.status === 409,
  );
  assert.equal(responses[1].status, 409);
});

test('one shared gate permits nested cache HEAD/native GET/full-fill progress without an outer-handler deadlock', async () => {
  const f = fixture({ wrapFetcher: (fetcher) => createMediaReadGate({ origin: ORIGIN, fetcher }) });
  await f.cache.identity(A);
  await Promise.all(
    ['one', 'two', 'three', 'four'].map(async (id) =>
      assert.equal(await (await f.cache.handle(request(id))).text(), '0123456789'),
    ),
  );
  const copied = f.store.entries.size;
  assert.ok(copied >= 2 && copied <= 4); // The existing two-fill cap may skip copies during a burst.
  assert.deepEqual(await Promise.all(['fill-one', 'fill-two'].map((id) => f.cache.ensure(A, id))), [
    { cached: true },
    { cached: true },
  ]);
  assert.equal(f.store.entries.size, copied + 2);
});

test('aborting a cache request removes its queued authorization HEAD without fetching or returning a synthetic success', async () => {
  let gate;
  const f = fixture({
    wrapFetcher: (fetcher) =>
      (gate = createMediaReadGate({ origin: ORIGIN, fetcher, maxActive: 1 })),
  });
  const held = await gate('/v1/assets/held/content'),
    controller = new AbortController();
  const pending = f.cache.handle(new Request(ORIGIN + path(), { signal: controller.signal }));
  await new Promise((resolve) => setImmediate(resolve));
  controller.abort();
  await assert.rejects(pending, (error) => error.name === 'AbortError');
  assert.equal(f.calls.filter((c) => c.method === 'HEAD').length, 0);
  assert.equal(f.store.entries.size, 0);
  await held.text();
});
