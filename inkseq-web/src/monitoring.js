// Error monitoring (h3-studio#501): Sentry, only on the deployed sites. Local development,
// previews and tests send nothing. No personal data: no request bodies, no form or chat
// text, no console messages (they can echo prompts), and no IP or cookies.
import * as Sentry from '@sentry/react';

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
  Sentry.init({
    dsn: DSN,
    environment,
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
  return true;
}

export const reactErrorHandler = Sentry.reactErrorHandler;
