// A tab left open across a release keeps running the old bundle until it reloads
// (owner, 2026-10-25). Compare this page's entry script with the one the site serves
// now and offer a reload; never reload on its own, so a draft is never lost.
import { readBrowserLocale, translate } from './i18n/locale.js';

const ENTRY = /\/assets\/index-[\w-]+\.js/;
const EVERY = 5 * 60 * 1000;

/** The hashed entry script named in a page's HTML, or null (dev server, odd page). */
export function entryOf(html) {
  return String(html || '').match(ENTRY)?.[0] || null;
}

const entryScript = (doc) =>
  entryOf(
    [...doc.querySelectorAll('script[src]')].map((script) => script.getAttribute('src')).join(' '),
  );

/** This page's build: the hash in its entry script's name. Null on the dev server. */
export function runningVersion(doc = globalThis.document) {
  return (doc && entryScript(doc)?.match(/index-([\w-]+)\.js/)?.[1]) || null;
}

/** True only when both entries are known and differ. */
export const isNewer = (running, served) => !!running && !!served && running !== served;

export function installVersionCheck({
  doc = document,
  fetcher = (url, options) => fetch(url, options),
  notify,
  reload = () => window.location.reload(),
  every = EVERY,
} = {}) {
  const running = entryScript(doc);
  if (!running) return () => {};
  let shown = false,
    busy = false;
  async function check() {
    if (shown || busy || doc.visibilityState === 'hidden') return;
    busy = true;
    try {
      const response = await fetcher('/', { cache: 'no-store', credentials: 'same-origin' });
      if (!response.ok) return;
      if (!isNewer(running, entryOf(await response.text()))) return;
      shown = true;
      const say = (key) => translate(readBrowserLocale(), key);
      notify(say('client.newVersion'), {
        action: { label: say('client.newVersionReload'), onClick: reload },
      });
    } catch {
      // Offline or mid-deploy: try again on the next tick.
    } finally {
      busy = false;
    }
  }
  const timer = setInterval(check, every);
  const onVisible = () => void check();
  doc.addEventListener('visibilitychange', onVisible);
  return () => {
    clearInterval(timer);
    doc.removeEventListener('visibilitychange', onVisible);
  };
}
