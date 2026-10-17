import { createVideoCacheStore } from "./video-cache-store.js";
import { createVideoCache, videoTarget } from "./video-cache-core.js";
import { createMediaReadGate, nativeContentRequest } from "./media-read-gate.js";
const contentFetch = createMediaReadGate({origin:self.location.origin});
const cache = createVideoCache({ store: createVideoCacheStore(), origin: self.location.origin, fetcher:contentFetch });
self.addEventListener("install", (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  if (!nativeContentRequest(event.request,self.location.origin)) return;
  event.respondWith(event.request.method === "GET" && !event.request.headers.has("Authorization") && videoTarget(event.request.url, self.location.origin)
    ? cache.handle(event.request, { waitUntil: (promise) => event.waitUntil(promise) })
    : contentFetch(event.request));
});
self.addEventListener("message", (event) => {
  const data = event.data;
  if (!event.source?.id || data?.channel !== "inkseq-video-cache-v1" || !event.ports?.[0]) return;
  const reply = event.ports[0];
  event.waitUntil((async () => {
    try {
      const result = data.action === "identity" ? await cache.identity(data.account) : data.action === "clear" ? await cache.clear() : data.action === "status" ? await cache.stats(data.account) : data.action === "played" ? await cache.ensure(data.account, data.id) : null;
      if (result === null) throw Error("invalid action");
      reply.postMessage({ ok: true, result });
    } catch {
      reply.postMessage({ ok: false });
    } finally {
      reply.close();
    }
  })());
});
