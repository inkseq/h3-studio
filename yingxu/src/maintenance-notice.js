// Release notice (#453): before a release window the server publishes
// `maintenance_notice = {active, message?, started_at, expected_end_at, severity}` on the anonymous
// /api/auth/config, and refuses new paid work with `maintenance_in_progress {expected_end_at}`.
// Nothing here shows unless the server says so.

export const MAINTENANCE_CHANGED = 'inkseq:maintenance-changed';

// Seconds since the epoch or an ISO string; anything else is unknown.
export function instant(value) {
  if (Number.isFinite(value) && value > 0) return value * 1000;
  if (typeof value === 'string') {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function noticeState(config, now = Date.now()) {
  const notice = config?.maintenance_notice;
  if (!notice || notice.active !== true) return null;
  const endsAt = instant(notice.expected_end_at);
  return {
    message:
      typeof notice.message === 'string' && notice.message.trim() ? notice.message.trim() : null,
    severity: notice.severity === 'critical' ? 'critical' : 'info',
    endsAt,
    // A passed end time must not leave a stale promise on screen.
    overdue: endsAt !== null && now > endsAt,
  };
}

// The end time in the viewer's own timezone; the date only when it is not today.
export function formatEnd(endsAt, locale, now = Date.now()) {
  if (!Number.isFinite(endsAt)) return null;
  const tag = locale === 'zh' ? 'zh-CN' : 'en';
  const sameDay = new Date(endsAt).toDateString() === new Date(now).toDateString();
  return new Intl.DateTimeFormat(tag, {
    ...(sameDay ? {} : { month: 'short', day: 'numeric' }),
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(endsAt));
}

// A refusal during the window carries the end time, so it can say when rather than show a code.
export function maintenanceRefusal(detail) {
  const body = detail?.code ? detail : detail?.detail;
  if (body?.code !== 'maintenance_in_progress') return null;
  return { endsAt: instant(body.expected_end_at) };
}
