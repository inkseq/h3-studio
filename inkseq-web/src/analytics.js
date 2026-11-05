// Product analytics (h3-studio#501): PostHog, only on the deployed sites, to see the
// signup → first story → generation → purchase funnel. Privacy first: no autocapture
// (clicked elements can carry story text), session replay with all page text masked,
// no cookies, anonymous
// ids, Do Not Track respected, and events never carry prompts, story text or names.
import { monitoringEnvironment } from './monitoring.js';
import { runningVersion } from './version-check.js';

const KEY = 'phc_rQdpUhKN9KJA8k9doCZGui6czQhqGj8NingDHmXmtEvc';
let ready = false,
  posthog = null,
  // Loaded while the browser is idle, so it never slows the first page (#1022).
  waiting = null;
const WAITING_MAX = 20;

// Attributes can hold what people wrote: shot titles in `title`, character names in `aria-label`.
const TEXT_ATTRIBUTES = new Set([
  'title',
  'aria-label',
  'aria-description',
  'aria-valuetext',
  'aria-placeholder',
  'alt',
  'label',
  'placeholder',
  'download',
]);
const ADDRESS_ATTRIBUTES = new Set(['src', 'href', 'xlink:href', 'poster', 'action', 'data-src']);
const PUBLIC_HOSTS = /^(fonts\.googleapis\.com|fonts\.gstatic\.com)$/;

/** An address as a replay may keep it: our own pages without their query, others by host only. */
export function replayAddress(value, origin = globalThis.location?.origin) {
  try {
    const url = new URL(value, origin);
    if (!/^https?:$/.test(url.protocol)) return url.protocol + '//';
    if (PUBLIC_HOSTS.test(url.hostname)) return url.href;
    return url.origin === origin ? url.origin + url.pathname : url.origin;
  } catch {
    return '';
  }
}

/** Replay attributes keep the page's shape, never its words or media links (#1021). */
export function replayAttribute(name, value) {
  const attribute = String(name).toLowerCase();
  if (TEXT_ATTRIBUTES.has(attribute)) return '*'.repeat(String(value).length);
  if (attribute === 'srcset') return '';
  if (ADDRESS_ATTRIBUTES.has(attribute)) return replayAddress(value);
  if (attribute === 'style' && String(value).includes('url('))
    return String(value).replace(/url\([^)]*\)/g, 'none');
  return value;
}

/** Speed events name the slowest element and its address; the numbers are what we need. */
export function withoutAttribution(event) {
  for (const [name, value] of Object.entries(event?.properties || {}))
    if (name.startsWith('$web_vitals_') && value?.attribution) {
      const { attribution: _attribution, ...kept } = value;
      event.properties[name] = kept;
    }
  return event;
}

export function replayRequest(request) {
  return { ...request, name: replayAddress(request.name) };
}

export function installAnalytics(location = globalThis.location) {
  const environment = monitoringEnvironment(location?.hostname);
  if (!environment) return false;
  waiting = [];
  const load = () =>
    import('posthog-js')
      .then(({ default: loaded }) => {
        posthog = loaded;
        posthog.init(KEY, {
          api_host: 'https://us.i.posthog.com',
          autocapture: false,
          capture_pageview: 'history_change',
          capture_pageleave: true,
          // Replay of page interactions for a sample of visits. Page text, inputs, text in
          // attributes and addresses are masked, so story text and media links never leave the
          // browser, as the privacy page says (#1021).
          enable_recording_console_log: false,
          disable_session_recording: false,
          session_recording: {
            maskAllInputs: true,
            maskTextSelector: '*',
            maskAttributeFn: replayAttribute,
            maskCapturedNetworkRequestFn: replayRequest,
          },
          before_send: withoutAttribution,
          persistence: 'localStorage',
          respect_dnt: true,
          person_profiles: 'identified_only',
          mask_all_text: true,
          mask_all_element_attributes: true,
        });
        posthog.register({ environment, app_version: runningVersion() });
        ready = true;
        for (const [event, properties] of waiting.splice(0)) track(event, properties);
        waiting = null;
      })
      .catch(() => {
        waiting = null;
      });
  const later = globalThis.requestIdleCallback || ((run) => setTimeout(run, 1500));
  later(() => void load());
  return true;
}

// Only numbers, booleans and short fixed codes are allowed as properties, so a caller
// cannot leak free text (a prompt or a title) by accident.
export function safeProperties(properties = {}) {
  return Object.fromEntries(
    Object.entries(properties).filter(
      ([, value]) =>
        typeof value === 'number' ||
        typeof value === 'boolean' ||
        (typeof value === 'string' && /^[a-z0-9_-]{1,40}$/i.test(value)),
    ),
  );
}

// Sign-out on a shared browser: the next person starts with a new anonymous id.
export function resetAnalytics() {
  if (!ready) return;
  try {
    posthog.reset();
  } catch {
    /* analytics must never break the product */
  }
}

// The anonymous PostHog id, for server events that close the checkout funnel (h3-studio#725).
// Only when analytics is running on this site and the visitor hasn't opted out or set Do Not
// Track; it is a random UUID, never an email, account or owner id.
export function checkoutAnalytics(dnt = globalThis.navigator?.doNotTrack) {
  if (!ready || dnt === '1' || dnt === 'yes') return undefined;
  try {
    if (posthog.has_opted_out_capturing()) return undefined;
    const id = posthog.get_distinct_id();
    return anonymousId(id) ? { consent: true, distinct_id: id } : undefined;
  } catch {
    return undefined;
  }
}

/** A PostHog anonymous id: a UUID (v4 or v7), nothing that could name a person. */
export function anonymousId(id) {
  return (
    typeof id === 'string' &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)
  );
}

/** Record one product step, e.g. track('story_chat_message', { turn: 3 }). */
export function track(event, properties) {
  if (!ready) {
    if (waiting && waiting.length < WAITING_MAX) waiting.push([event, properties]);
    return;
  }
  try {
    posthog.capture(event, safeProperties(properties));
  } catch {
    /* analytics must never break the product */
  }
}
