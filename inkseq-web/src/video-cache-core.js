import { VIDEO_FILE_LIMIT } from './video-cache-store.js';
const ID = /^[A-Za-z0-9_-]{1,220}$/,
  ACCOUNT = /^[A-Za-z0-9_-]{1,80}$/,
  RENDITION = /^[a-z0-9_-]{1,40}$/;
// Original result media and owner uploads. Both are hash-verified objects that
// never change under the same id, so one copy can be kept across days.
const mediaType = (value) => {
  const type = value?.split(';')[0].trim().toLowerCase();
  return /^(?:image|video|audio)\/[a-z0-9.+-]{1,60}$/.test(type || '') ? type : null;
};
function videoTarget(value, origin) {
  try {
    const url = new URL(value, origin),
      match = /^\/v1\/(artifacts|assets)\/([A-Za-z0-9_-]{1,160})\/content$/.exec(url.pathname);
    if (url.origin !== origin || !match || url.hash || url.username || url.password) return null;
    for (const key of url.searchParams.keys())
      if (
        !['expected_account', 'download', 'rendition'].includes(key) ||
        url.searchParams.getAll(key).length !== 1
      )
        return null;
    const rendition = url.searchParams.get('rendition');
    if (rendition !== null && (!RENDITION.test(rendition) || rendition === 'thumb')) return null;
    const account = url.searchParams.get('expected_account');
    if (account !== null && !ACCOUNT.test(account)) return null;
    if (
      url.searchParams.has('download') &&
      !['0', '1', 'true', 'false'].includes(url.searchParams.get('download'))
    )
      return null;
    // Artifact originals keep their earlier cache key; others are namespaced.
    const id =
      (match[1] === 'assets' ? 'as_' : '') + match[2] + (rendition ? '_r_' + rendition : '');
    return { id, expected: account, url: url.pathname + url.search };
  } catch {
    return null;
  }
}
const cacheKey = (account, id) =>
  ACCOUNT.test(account) && ID.test(id) ? account + '\0' + id : null;
function byteRange(value, size) {
  if (value === null || value === void 0) return null;
  const match = /^bytes=(\d{0,20})-(\d{0,20})$/.exec(value);
  if (!match || (!match[1] && !match[2])) return false;
  const left = match[1] ? Number(match[1]) : null,
    right = match[2] ? Number(match[2]) : null;
  if (
    (left !== null && !Number.isSafeInteger(left)) ||
    (right !== null && !Number.isSafeInteger(right))
  )
    return false;
  let start, end;
  if (left === null) {
    if (right === 0) return false;
    start = Math.max(0, size - right);
    end = size - 1;
  } else {
    start = left;
    end = right === null ? size - 1 : Math.min(right, size - 1);
  }
  return start >= size || start > end ? false : { start, end };
}
function responseFor(blob, request, head) {
  const size = blob.size,
    range = byteRange(request.headers.get('Range'), size),
    headers = new Headers({
      'Content-Type': blob.type,
      'Accept-Ranges': 'bytes',
      'Cache-Control': 'private, no-store',
      'Referrer-Policy': 'no-referrer',
      Vary: 'Authorization, Cookie, X-Expected-Account',
    });
  const disposition = head.headers.get('Content-Disposition');
  if (disposition) headers.set('Content-Disposition', disposition);
  if (range === false) {
    headers.set('Content-Range', 'bytes */' + size);
    return new Response(null, { status: 416, headers });
  }
  headers.set('X-Inkseq-Video-Cache', 'hit');
  headers.set('X-Authenticated-Account', head.headers.get('X-Authenticated-Account'));
  const body = range ? blob.slice(range.start, range.end + 1, blob.type) : blob;
  headers.set('Content-Length', String(body.size));
  if (range) headers.set('Content-Range', `bytes ${range.start}-${range.end}/${size}`);
  return new Response(body, { status: range ? 206 : 200, headers });
}
const failure = (status) =>
  new Response('Video unavailable. Check your connection and sign-in.', {
    status: [400, 401, 403, 404, 409].includes(status) ? status : 503,
    headers: { 'Cache-Control': 'no-store' },
  });
async function completeBody(response, size, type) {
  if (
    response.status !== 200 ||
    ['opaque', 'opaqueredirect', 'error'].includes(response.type) ||
    !type ||
    mediaType(response.headers.get('Content-Type')) !== type ||
    response.headers.get('Content-Range') ||
    Number(response.headers.get('Content-Length')) !== size
  ) {
    void response.body?.cancel().catch(() => {});
    return null;
  }
  if (!response.body?.getReader) return null;
  const reader = response.body.getReader(),
    chunks = [];
  let bytes = 0;
  try {
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      bytes += part.value.byteLength;
      if (bytes > size || bytes > VIDEO_FILE_LIMIT) {
        void reader.cancel().catch(() => {});
        return null;
      }
      chunks.push(part.value);
    }
    return bytes === size ? new Blob(chunks, { type }) : null;
  } catch {
    void reader.cancel().catch(() => {});
    return null;
  }
}
// #232 (#44): scrubbing makes many range reads of one file; one access check covers them
// for a minute instead of a HEAD per read (which tripped the media read limit).
const AUTH_TTL_MS = 60000;
function createVideoCache({
  store,
  origin,
  fetcher = (...args) => fetch(...args),
  now = () => Date.now(),
  authTtlMs = AUTH_TTL_MS,
}) {
  const filling = new Map(),
    checked = new Map();
  async function authorize(target, epoch, signal) {
    const key = target.url + '|' + (target.expected ?? '') + '|' + epoch;
    const hit = checked.get(key);
    if (hit && now() - hit.at < authTtlMs) return hit.auth;
    const auth = await checkAccess(target, epoch, signal);
    if (authTtlMs > 0 && !auth.error && auth.epoch !== null) {
      if (checked.size > 512) checked.clear();
      checked.set(key, { at: now(), auth });
    }
    return auth;
  }
  async function checkAccess(target, epoch, signal) {
    let head;
    try {
      head = await fetcher(target.url, {
        method: 'HEAD',
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        signal,
        headers: target.expected !== null ? { 'X-Expected-Account': target.expected } : {},
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      return { error: 503 };
    }
    if (
      head.status !== 200 ||
      head.redirected ||
      ['opaque', 'opaqueredirect'].includes(head.type)
    ) {
      await store.clear();
      return { error: head.status };
    }
    const account = head.headers.get('X-Authenticated-Account');
    if (!ACCOUNT.test(account || '') || (target.expected !== null && account !== target.expected)) {
      await store.clear();
      return { error: 409 };
    }
    const token = epoch === null ? { epoch: null } : await store.selectAccount(account, epoch);
    if (!token) return { error: 409 };
    const length = head.headers.get('Content-Length'),
      size = /^[1-9]\d*$/.test(length || '') ? Number(length) : null;
    return {
      head,
      account,
      epoch: token.epoch,
      key: cacheKey(account, target.id),
      size,
      type: mediaType(head.headers.get('Content-Type')),
      eligible:
        !!mediaType(head.headers.get('Content-Type')) &&
        Number.isSafeInteger(size) &&
        size <= VIDEO_FILE_LIMIT,
    };
  }
  async function saveResponse(target, auth, response) {
    const blob = await completeBody(response, auth.size, auth.type);
    if (!blob) return null;
    const stored = await store.put(
      { key: auth.key, account: auth.account, id: target.id, size: blob.size, blob },
      auth.epoch,
    );
    return stored && (await store.snapshot()).epoch === auth.epoch ? blob : null;
  }
  async function readFull(target, auth) {
    const url = new URL(target.url, origin);
    url.searchParams.set('expected_account', auth.account);
    return saveResponse(
      target,
      auth,
      await fetcher(url.pathname + url.search, {
        method: 'GET',
        credentials: 'same-origin',
        mode: 'cors',
        cache: 'no-store',
        redirect: 'follow',
      }),
    );
  }
  function fill(target, auth, response) {
    const key = auth.key + '@' + auth.epoch;
    if (filling.has(key)) {
      void response?.body?.cancel().catch(() => {});
      return filling.get(key);
    }
    if (filling.size >= 2) {
      void response?.body?.cancel().catch(() => {});
      return Promise.resolve(null);
    }
    const task = (response ? saveResponse(target, auth, response) : readFull(target, auth))
      .catch(() => null)
      .finally(() => filling.delete(key));
    filling.set(key, task);
    return task;
  }
  async function ensure(account, id, path) {
    // `path` names an asset or rendition original; a bare id is a result original.
    const url = new URL(
      typeof path === 'string' ? path : '/v1/artifacts/' + id + '/content',
      origin,
    );
    url.searchParams.set('expected_account', account);
    const target = url.origin === origin ? videoTarget(url.pathname + url.search, origin) : null;
    if (!target || !cacheKey(account, target.id)) return { cached: false };
    try {
      const snapshot = await store.snapshot(),
        auth = await authorize(target, snapshot.epoch);
      if (auth.error || !auth.eligible) return { cached: false };
      const hit = await store.get(auth.key, auth.epoch);
      if (
        hit?.size === auth.size &&
        hit.blob instanceof Blob &&
        hit.blob.type === auth.type &&
        hit.blob.size === auth.size
      )
        return { cached: true };
      return { cached: !!(await fill(target, auth)) };
    } catch {
      return { cached: false };
    }
  }
  async function handle(request, { waitUntil = () => {} } = {}) {
    const parsed = videoTarget(request.url, origin);
    if (!parsed || request.method !== 'GET' || request.headers.has('Authorization')) return null;
    const headerAccount = request.headers.get('X-Expected-Account');
    // Match the native-content guard: a query fence and the original header
    // must agree. Header-only values are forwarded unchanged so the server
    // retains its exact mismatch/invalid-header semantics (including duplicates
    // combined by Fetch Headers). Never replace this fence with our cache hint.
    if (parsed.expected !== null && headerAccount !== null && parsed.expected !== headerAccount)
      return failure(400);
    const target = { ...parsed, expected: headerAccount ?? parsed.expected };
    try {
      const snapshot = await store.snapshot().catch(() => null);
      // A cold partial/metadata read cannot populate the cache. Its ordinary
      // authenticated GET streams directly without doubling thumbnail reads.
      if (request.headers.has('Range')) {
        const owner = target.expected || snapshot?.account;
        if (
          !snapshot ||
          owner !== snapshot.account ||
          !cacheKey(owner, target.id) ||
          !(await store.get(cacheKey(owner, target.id), snapshot.epoch))
        )
          return fetcher(request);
      }
      const auth = await authorize(target, snapshot?.epoch ?? null, request.signal);
      if (auth.error) return failure(auth.error);
      if (auth.epoch === null || !auth.eligible) return fetcher(request);
      const hit = await store.get(auth.key, auth.epoch);
      if (
        hit?.size === auth.size &&
        hit.blob instanceof Blob &&
        hit.blob.type === auth.type &&
        hit.blob.size === auth.size
      ) {
        if ((await store.snapshot()).epoch !== auth.epoch) return failure(409);
        return responseFor(hit.blob, request, auth.head);
      }
      const response = await fetcher(request);
      // Reuse an already readable complete response without blocking its
      // original stream or starting another full download.
      if (
        !request.headers.has('Range') &&
        response.status === 200 &&
        !['opaque', 'opaqueredirect', 'error'].includes(response.type)
      )
        waitUntil(fill(target, auth, response.clone()));
      return response;
    } catch (error) {
      if (request.signal.aborted) throw error;
      return failure(503);
    }
  }
  async function identity(account) {
    if (account === null) return store.clear();
    if (!ACCOUNT.test(account || '')) throw Error('invalid account');
    const before = await store.snapshot();
    try {
      const response = await fetcher('/api/auth/me', {
          credentials: 'same-origin',
          cache: 'no-store',
          redirect: 'error',
        }),
        body = await response.json(),
        actual = body?.username || body?.user?.username || body?.user || body?.account?.username;
      if (!response.ok || response.redirected || actual !== account)
        throw Error('identity unavailable');
    } catch {
      await store.clear();
      throw Error('identity unavailable');
    }
    const selected = await store.selectAccount(account, before.epoch);
    if (!selected) throw Error('identity changed');
    return selected;
  }
  return {
    handle,
    ensure,
    identity,
    clear: () => store.clear(),
    async stats(account) {
      await identity(account);
      return store.stats(account);
    },
  };
}
export { byteRange, cacheKey, createVideoCache, videoTarget };
