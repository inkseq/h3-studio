// Error monitoring (h3-studio#501): Sentry, only on the deployed sites. Local development,
// previews and tests send nothing. No personal data: no request bodies, no form or chat
// text, no console messages (they can echo prompts), and no IP or cookies.
// Loaded after the page has drawn (#1022); errors before that wait in a short queue.
import { runningVersion } from './version-check.js';
let Sentry = null;
const early = [];
const EARLY_MAX = 10;
const remember = (error) => {
  if (early.length < EARLY_MAX) early.push(error);
};

const DSN =
  'https://cb0e80ebfcd9cd965b42321da8a819e4@o4512306171019264.ingest.us.sentry.io/4512306294161408';

/** 'production' or 'staging' for our deployed hosts, otherwise null (no reporting). */
export function monitoringEnvironment(hostname) {
  if (hostname === 'inkseq.com' || hostname === 'www.inkseq.com') return 'production';
  if (hostname === 'staging.inkseq.com') return 'staging';
  return null;
}

/** Breadcrumbs keep navigation and request status, never text a person typed. */
export function scrubBreadcrumb(crumb) {
  if (crumb.category === 'console') return null;
  if (crumb.category === 'ui.input') return null;
  if (crumb.data && (crumb.category === 'fetch' || crumb.category === 'xhr')) {
    const { method, status_code, url } = crumb.data;
    return { ...crumb, data: { method, status_code, url: String(url || '').split('?')[0] } };
  }
  return crumb;
}

export function installMonitoring(location = globalThis.location) {
  const environment = monitoringEnvironment(location?.hostname);
  if (!environment) return false;
  const onError = (event) => remember(event.error || event.reason);
  globalThis.addEventListener?.('error', onError);
  globalThis.addEventListener?.('unhandledrejection', onError);
  void import('@sentry/react')
    .then((loaded) => {
      loaded.init({
        dsn: DSN,
        environment,
        release: runningVersion() || undefined,
        sendDefaultPii: false,
        tracesSampleRate: 0,
        beforeBreadcrumb: scrubBreadcrumb,
        beforeSend(event) {
          if (event.request) {
            delete event.request.data;
            delete event.request.cookies;
            if (event.request.url) event.request.url = event.request.url.split('?')[0];
          }
          return event;
        },
      });
      Sentry = loaded;
      for (const error of early.splice(0)) if (error) Sentry.captureException(error);
    })
    .catch(() => {})
    .finally(() => {
      globalThis.removeEventListener?.('error', onError);
      globalThis.removeEventListener?.('unhandledrejection', onError);
    });
  return true;
}

export function reactErrorHandler() {
  return (error, info) => (Sentry ? Sentry.reactErrorHandler()(error, info) : remember(error));
}
