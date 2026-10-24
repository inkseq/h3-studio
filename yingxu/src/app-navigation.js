// Only app pages use client navigation. API/media, downloads and external links
// retain their native browser behavior.
export const APP_PATHS = Object.freeze([
  '/',
  '/quick-chat',
  '/library',
  '/operator',
  '/admin',
  '/settings',
  '/trash',
  '/freestyle',
]);
const navigators = new WeakMap();
export const appPath = (pathname) => pathname.replace(/\/+$/, '') || '/';

export function appLocation(href, base = 'http://localhost/') {
  const url = new URL(href, base);
  return {
    pathname: url.pathname,
    path: appPath(url.pathname),
    search: url.search,
    hash: url.hash,
    href: url.pathname + url.search + url.hash,
  };
}

export function createAppNavigation(browser) {
  const listeners = new Set(),
    visited = new Map();
  let running = false,
    users = 0,
    originalPush,
    originalReplace,
    push,
    replace;
  const snapshot = () =>
    browser.location.pathname + browser.location.search + browser.location.hash;
  const remember = () => {
    const route = appLocation(browser.location.href);
    if (APP_PATHS.includes(route.path)) visited.set(route.path, route.href);
  };
  const changed = () => {
    remember();
    for (const listener of [...listeners]) listener();
  };
  const announce = () =>
    browser.dispatchEvent(new browser.PopStateEvent('popstate', { state: browser.history.state }));
  function start() {
    users++;
    if (running) return stop;
    running = true;
    remember();
    originalPush = browser.history.pushState;
    originalReplace = browser.history.replaceState;
    const wrap = (original) =>
      function (...args) {
        remember();
        const result = original.apply(this, args);
        announce();
        return result;
      };
    push = wrap(originalPush);
    replace = wrap(originalReplace);
    browser.history.pushState = push;
    browser.history.replaceState = replace;
    browser.addEventListener('popstate', changed);
    return stop;
  }
  function stop() {
    if (--users > 0) return;
    users = 0;
    running = false;
    browser.removeEventListener('popstate', changed);
    if (browser.history.pushState === push) browser.history.pushState = originalPush;
    if (browser.history.replaceState === replace) browser.history.replaceState = originalReplace;
  }
  function navigate(href, { replace = false, restore = true, state = null, scroll = true } = {}) {
    const current = new URL(browser.location.href),
      url = new URL(href, current);
    if (url.origin !== current.origin || !APP_PATHS.includes(appPath(url.pathname))) return false;
    remember();
    // Works has a deliberate empty URL. Reopening Quick Create resumes the
    // latest selected conversation, including a replaceState from its view.
    if (
      restore &&
      url.pathname === '/quick-chat' &&
      !url.search &&
      !url.hash &&
      visited.has('/quick-chat')
    ) {
      const remembered = new URL(visited.get('/quick-chat'), current);
      url.search = remembered.search;
      url.hash = remembered.hash;
    }
    const next = url.pathname + url.search + url.hash;
    if (next === snapshot()) return true;
    browser.history[replace ? 'replaceState' : 'pushState'](state, '', next);
    if (!running) changed();
    if (scroll) {
      const show = () => {
        if (url.hash) {
          let id;
          try {
            id = decodeURIComponent(url.hash.slice(1));
          } catch {
            return;
          }
          browser.document.getElementById(id)?.scrollIntoView();
        } else browser.scrollTo?.(0, 0);
      };
      browser.requestAnimationFrame ? browser.requestAnimationFrame(show) : show();
    }
    return true;
  }
  function intercept(event) {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    )
      return false;
    const element = event.target?.closest ? event.target : event.target?.parentElement;
    const anchor = element?.closest?.('a[href]');
    if (
      !anchor ||
      anchor.hasAttribute('download') ||
      (anchor.getAttribute('target') && !/^_self$/i.test(anchor.getAttribute('target'))) ||
      anchor.relList?.contains('external')
    )
      return false;
    const href = anchor.getAttribute('href');
    if (!href || href.startsWith('#')) return false;
    let url;
    try {
      url = new URL(href, browser.location.href);
    } catch {
      return false;
    }
    const current = new URL(browser.location.href);
    if (url.hash && url.pathname === current.pathname && url.search === current.search)
      return false;
    if (url.origin !== current.origin || !APP_PATHS.includes(appPath(url.pathname))) return false;
    if (!navigate(url.href)) return false;
    event.preventDefault();
    return true;
  }
  return {
    getSnapshot: snapshot,
    subscribe(listener) {
      const release = start();
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
        release();
      };
    },
    install() {
      const release = start();
      browser.document.addEventListener('click', intercept);
      return () => {
        browser.document.removeEventListener('click', intercept);
        release();
      };
    },
    navigate,
    intercept,
  };
}

export function appNavigation(browser = window) {
  if (!navigators.has(browser)) navigators.set(browser, createAppNavigation(browser));
  return navigators.get(browser);
}
export const navigateApp = (href, options) => appNavigation().navigate(href, options);
export const installAppNavigation = () => appNavigation().install();
export const subscribeAppLocation = (listener) => appNavigation().subscribe(listener);
export const getAppLocation = () => appNavigation().getSnapshot();
