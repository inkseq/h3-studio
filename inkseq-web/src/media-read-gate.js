// One transient read gate shared by native passthrough, cache HEAD and fills.
// It never selects an owner, retries a read or keeps a URL after completion.
export function nativeContentRequest(value, origin, init = {}) {
  try {
    const url = new URL(typeof value === 'string' ? value : value.url, origin);
    const method = init.method || value.method || 'GET';
    const renditions = url.searchParams.getAll('rendition');
    // Small rendition responses use their own visible-image lane. They must
    // never enter the heavy-video gate or be mistaken for original videos.
    if (renditions.length === 1 && renditions[0] === 'thumb') return false;
    return (
      ['GET', 'HEAD'].includes(method) &&
      url.origin === origin &&
      /^\/v1\/(?:assets|artifacts)\/[A-Za-z0-9_-]{1,200}\/content$/.test(url.pathname)
    );
  } catch {
    return false;
  }
}

const aborted = (signal) =>
  signal?.reason || new DOMException('Media read cancelled', 'AbortError');
class QueueBusy extends Error {}
const busy = () =>
  Response.json(
    { code: 'media_read_queue_busy', detail: 'Media reads are busy. Retry the original file.' },
    { status: 429, headers: { 'Cache-Control': 'no-store', 'Retry-After': '3' } },
  );

export function createMediaReadGate({
  origin,
  fetcher = (...args) => fetch(...args),
  maxActive = 2,
  maxQueued = 128,
  queueMs = 30000,
} = {}) {
  let active = 0;
  const waiting = [];

  function lease() {
    active++;
    let released = false;
    return () => {
      if (released) return;
      released = true;
      active--;
      while (active < maxActive && waiting.length) waiting.shift().grant();
    };
  }
  function acquire(signal) {
    if (signal?.aborted) return Promise.reject(aborted(signal));
    if (active < maxActive && waiting.length === 0) return Promise.resolve(lease());
    if (waiting.length >= maxQueued) return Promise.reject(new QueueBusy());
    return new Promise((resolve, reject) => {
      let timer;
      const remove = () => {
        clearTimeout(timer);
        signal?.removeEventListener('abort', cancel);
        const index = waiting.indexOf(entry);
        if (index !== -1) waiting.splice(index, 1);
      };
      const cancel = () => {
        remove();
        reject(aborted(signal));
      };
      const entry = {
        grant() {
          remove();
          if (signal?.aborted) reject(aborted(signal));
          else resolve(lease());
        },
      };
      waiting.push(entry);
      signal?.addEventListener('abort', cancel, { once: true });
      timer = setTimeout(() => {
        remove();
        reject(new QueueBusy());
      }, queueMs);
    });
  }
  function appHopFinished(response, method) {
    if (
      method === 'HEAD' ||
      !response.body ||
      ['opaque', 'opaqueredirect', 'error'].includes(response.type)
    )
      return true;
    if (response.url) {
      try {
        if (new URL(response.url).origin !== origin) return true;
      } catch {
        /* Treat unknown as a local body. */
      }
    }
    return false;
  }
  function holdLocalBody(response, signal, release) {
    const reader = response.body.getReader();
    let finished = false,
      cancelling = false,
      controller;
    const finish = () => {
      if (finished) return;
      finished = true;
      signal?.removeEventListener('abort', cancel);
      release();
    };
    const cancel = () => {
      if (finished || cancelling) return;
      cancelling = true;
      controller.error(aborted(signal));
      void reader
        .cancel(aborted(signal))
        .catch(() => {})
        .finally(finish);
    };
    const body = new ReadableStream({
      start(value) {
        controller = value;
      },
      async pull(value) {
        try {
          const part = await reader.read();
          if (cancelling) return;
          if (part.done) {
            finish();
            value.close();
          } else value.enqueue(part.value);
        } catch (error) {
          if (!cancelling) {
            finish();
            value.error(error);
          }
        }
      },
      async cancel(reason) {
        cancelling = true;
        try {
          await reader.cancel(reason);
        } finally {
          finish();
        }
      },
    });
    signal?.addEventListener('abort', cancel, { once: true });
    if (signal?.aborted) cancel();
    return new Response(body, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  }
  return async (value, init = {}) => {
    if (!nativeContentRequest(value, origin, init)) return fetcher(value, init);
    const signal = init.signal || value.signal,
      method = init.method || value.method || 'GET';
    let release;
    try {
      release = await acquire(signal);
    } catch (error) {
      if (error instanceof QueueBusy) return busy();
      throw error;
    }
    try {
      if (signal?.aborted) throw aborted(signal);
      // Forward the original Request/init, including ranges, credentials,
      // account fences, redirect mode and AbortSignal, without rebuilding it.
      const response = await fetcher(value, init);
      if (appHopFinished(response, method)) {
        release();
        return response;
      }
      return holdLocalBody(response, signal, release);
    } catch (error) {
      release();
      throw error;
    }
  };
}
