// Disposable media bytes only. This database never authorizes a user or a job.
const VIDEO_CACHE_LIMIT = 1024 * 1024 * 1024, VIDEO_FILE_LIMIT = 64 * 1024 * 1024, VIDEO_ENTRY_LIMIT = 256;
function evictionPlan(entries, incoming, { maxBytes = VIDEO_CACHE_LIMIT, maxEntries = VIDEO_ENTRY_LIMIT } = {}) {
  const others = entries.filter((item) => item.key !== incoming.key).sort((a, b) => a.used - b.used || a.key.localeCompare(b.key));
  let bytes = others.reduce((sum, item) => sum + item.size, 0), count = others.length;
  const remove = [];
  while (others.length && (bytes + incoming.size > maxBytes || count >= maxEntries)) {
    const item = others.shift();
    bytes -= item.size;
    count--;
    remove.push(item.key);
  }
  return { remove, bytes: bytes + incoming.size, count: count + 1 };
}
function createVideoCacheStore({ indexedDB = globalThis.indexedDB, now = Date.now } = {}) {
  let ready;
  const open = () => ready ??= new Promise((resolve, reject) => {
    const request = indexedDB.open("inkseq-video-cache-v1", 1);
    request.onupgradeneeded = () => {
      for (const name of ["state", "entries", "bodies"]) request.result.createObjectStore(name);
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => {
      ready = null;
      reject(request.error);
    };
  });
  async function transaction(work) {
    const db = await open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(["state", "entries", "bodies"], "readwrite"), state = tx.objectStore("state"), entries = tx.objectStore("entries"), bodies = tx.objectStore("bodies");
      let result;
      const meta = state.get("meta");
      meta.onsuccess = () => {
        try {
          work(meta.result || { epoch: 0, account: null }, { state, entries, bodies }, (value) => {
            result = value;
          });
        } catch (error) {
          tx.abort();
          reject(error);
        }
      };
      tx.oncomplete = () => resolve(result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || Error("cache transaction cancelled"));
    });
  }
  const clear = (account = null) => transaction((meta, stores, done) => {
    const value = { epoch: meta.epoch + 1, account };
    stores.entries.clear();
    stores.bodies.clear();
    stores.state.put(value, "meta");
    done(value);
  });
  return {
    snapshot: () => transaction((meta, _stores, done) => done(meta)),
    clear,
    selectAccount: (account, epoch) => transaction((meta, stores, done) => {
      if (meta.epoch !== epoch) {
        done(null);
        return;
      }
      if (meta.account !== account) {
        // First verified owner initializes an empty namespace without rejecting
        // parallel cold requests; every later identity change advances CAS.
        meta = { epoch: meta.account === null && meta.epoch === 0 ? 0 : meta.epoch + 1, account };
        stores.entries.clear();
        stores.bodies.clear();
        stores.state.put(meta, "meta");
      }
      done(meta);
    }),
    get: (key, epoch) => transaction((meta, stores, done) => {
      if (meta.epoch !== epoch) {
        done(null);
        return;
      }
      const request = stores.entries.get(key);
      request.onsuccess = () => {
        const entry = request.result;
        if (!entry || entry.account !== meta.account) {
          done(null);
          return;
        }
        const body = stores.bodies.get(key);
        body.onsuccess = () => {
          if (!body.result) {
            stores.entries.delete(key);
            done(null);
            return;
          }
          stores.entries.put({ ...entry, used: now() }, key);
          done({ ...entry, blob: body.result });
        };
      };
    }),
    put: (entry, epoch) => transaction((meta, stores, done) => {
      if (meta.epoch !== epoch || meta.account !== entry.account || !(entry.blob instanceof Blob) || entry.blob.size !== entry.size || entry.size < 1 || entry.size > VIDEO_FILE_LIMIT) {
        done(false);
        return;
      }
      const request = stores.entries.getAll();
      request.onsuccess = () => {
        const plan = evictionPlan(request.result, entry);
        for (const key of plan.remove) {
          stores.entries.delete(key);
          stores.bodies.delete(key);
        }
        stores.entries.put({ key: entry.key, account: entry.account, id: entry.id, size: entry.size, used: now() }, entry.key);
        stores.bodies.put(entry.blob, entry.key);
        done(true);
      };
    }),
    stats: (account) => transaction((meta, stores, done) => {
      if (meta.account !== account) {
        done({ bytes: 0, count: 0, limit: VIDEO_CACHE_LIMIT });
        return;
      }
      const request = stores.entries.getAll();
      request.onsuccess = () => done({ bytes: request.result.reduce((sum, item) => sum + item.size, 0), count: request.result.length, limit: VIDEO_CACHE_LIMIT });
    })
  };
}
export {
  VIDEO_CACHE_LIMIT,
  VIDEO_ENTRY_LIMIT,
  VIDEO_FILE_LIMIT,
  createVideoCacheStore,
  evictionPlan
};
