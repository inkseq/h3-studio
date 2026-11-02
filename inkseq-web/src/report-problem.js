// The report-a-problem form (h3-studio POST /v1/feedback, source "web").
export const REPORT_CATEGORIES = ['page', 'generation', 'payment', 'account', 'other'];
export const REPORT_MAX = 4000;

// Only the path and view: a query can hold ids and tokens, and ids travel as their own fields.
export function reportPage(location = globalThis.location) {
  const view = new URLSearchParams(location?.search || '').get('view');
  return ((location?.pathname || '/') + (view ? '?view=' + view : '')).slice(0, 200);
}
export function reportClient(global = globalThis) {
  const size = global.innerWidth + 'x' + global.innerHeight;
  return ((global.navigator?.userAgent || '').slice(0, 280) + ' ' + size).trim();
}
export function reportBody({ category, text, projectId, shotId, page, client }) {
  return {
    category,
    severity: 'medium',
    summary: text.trim(),
    source: 'web',
    page,
    client,
    ...(projectId ? { project_id: projectId } : {}),
    ...(projectId && shotId ? { shot_id: shotId } : {}),
  };
}
export function reportProblemKey(error) {
  if (error?.status === 429) return 'report.tooMany';
  if (error?.status === 422) return 'report.privateText';
  return 'report.failed';
}
// A short reference the person can quote. The full id stays on the server.
export const reportShortId = (id) =>
  String(id || '')
    .slice(0, 8)
    .toUpperCase();
