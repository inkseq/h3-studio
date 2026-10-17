// Page-to-worker hints are not credentials. Initial unresolved auth must not
// clear the previous cache while an ordinary page reload checks /me.
let workerReady = null, currentAccount = null, identityObserved = false;
const ACCOUNT = /^[A-Za-z0-9_-]{1,80}$/;
async function request(action, values = {}) {
  if (!workerReady) throw Error("cache unavailable");
  let readyTimer, worker;
  try {
    worker = await Promise.race([workerReady, new Promise((_, reject) => {
      readyTimer = setTimeout(() => reject(Error("cache unavailable")), 2500);
    })]);
  } finally {
    clearTimeout(readyTimer);
  }
  return new Promise((resolve, reject) => {
    const channel = new MessageChannel(), timer = setTimeout(() => {
      channel.port1.close();
      reject(Error("cache unavailable"));
    }, 1e4);
    channel.port1.onmessage = (event) => {
      clearTimeout(timer);
      channel.port1.close();
      event.data?.ok ? resolve(event.data.result) : reject(Error("cache unavailable"));
    };
    worker.postMessage({ channel: "inkseq-video-cache-v1", action, ...values }, [channel.port2]);
  });
}
function videoCacheIdentity(account) {
  currentAccount = typeof account === "string" && ACCOUNT.test(account) ? account : null;
  identityObserved = true;
  void request("identity", { account: currentAccount }).catch(() => {
  });
}
function clearVideoCache() {
  return request("clear");
}
function videoCacheStatus(account) {
  return request("status", { account });
}
function playedVideoTarget(element, account, origin) {
  if (element?.tagName !== "VIDEO" || element.dataset?.videoCachePlay !== "true" || element.autoplay || element.loop || !account) return null;
  try {
    const url = new URL(element.currentSrc || element.src, origin), match = /^\/v1\/artifacts\/([A-Za-z0-9_-]{1,160})\/content$/.exec(url.pathname);
    if (url.origin !== origin || !match || url.searchParams.get("expected_account") && url.searchParams.get("expected_account") !== account) return null;
    return { account, id: match[1] };
  } catch {
    return null;
  }
}
function installVideoCache(scriptURL, { container = globalThis.navigator?.serviceWorker, document = globalThis.document } = {}) {
  if (!container || !globalThis.isSecureContext) return;
  workerReady = container.register(scriptURL, { scope: "/", updateViaCache: "none" }).then(async (registration) => {
    if (registration.active) return registration.active;
    await container.ready;
    return registration.active || registration.waiting;
  });
  void workerReady.then(() => {
    if (identityObserved) videoCacheIdentity(currentAccount);
  }).catch(() => {
  });
  // Only opted-in full players may warm a partial response; metadata reads,
  // autoplay and hover thumbnails never enqueue another full GET.
  document?.addEventListener("play", (event) => {
    const target = playedVideoTarget(event.target, currentAccount, globalThis.location.origin);
    if (target) void request("played", target).catch(() => {
    });
  }, true);
}
export {
  clearVideoCache,
  installVideoCache,
  playedVideoTarget,
  videoCacheIdentity,
  videoCacheStatus
};
