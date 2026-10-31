import { translate } from './i18n/locale.js';
export const HISTORY_ACTIONS = {
  trash: '移到回收站',
  restore: '恢复创作',
  purge: '永久删除',
  empty_trash: '清空回收站',
};
const integer = (value) => Number.isSafeInteger(value) && value >= 0;
const identifier = (value) => typeof value === 'string' && value.length > 0 && value.length <= 200;
const states = new Set(['active', 'trashed', 'purging', 'purged']);
const fail = () => {
  throw Error('回收站收据不完整或与原选择不一致，请核对原操作。');
};
export function historyReason(code, locale = 'zh') {
  return (
    {
      active_work: translate(locale, 'history.reason0'),
      session_busy: translate(locale, 'history.reason0'),
      active_job: translate(locale, 'history.reason0'),
      running_job: translate(locale, 'history.reason0'),
      jobs_active_or_uncertain: translate(locale, 'history.reason1'),
      job_binding_unknown: translate(locale, 'history.reason2'),
      upstream_stop_unconfirmed: translate(locale, 'history.reason3'),
      worker_obligation_unresolved: translate(locale, 'history.reason4'),
      admission_unresolved: translate(locale, 'history.reason5'),
      authoring_operation_unresolved: translate(locale, 'history.reason6'),
      upload_unresolved: translate(locale, 'history.reason7'),
      collection_unresolved: translate(locale, 'history.reason8'),
      purge_already_started: translate(locale, 'history.reason9'),
      session_not_active: translate(locale, 'history.reason10'),
      session_not_in_trash: translate(locale, 'history.reason11'),
      uncertain_work: translate(locale, 'history.reason12'),
      upstream_unconfirmed: translate(locale, 'history.reason3'),
      version_conflict: translate(locale, 'history.reason13'),
      review_hold: translate(locale, 'history.reason14'),
      purging: translate(locale, 'history.reason15'),
      purged: translate(locale, 'history.reason16'),
      not_trashed: translate(locale, 'history.reason11'),
      shared_reference: translate(locale, 'history.reason17'),
      chosen_story_reference: translate(locale, 'history.reason18'),
    }[code] || translate(locale, 'history.reason19')
  );
}
export function historySelectable(row, view) {
  return (
    !!row &&
    identifier(row.id) &&
    integer(row.version) &&
    row.version > 0 &&
    (view === 'active'
      ? row.trash_state === 'active' && row.trashable === true
      : row.trash_state === 'trashed')
  );
}
export function historySelection(rows, ids, view) {
  const selected = ids.map((id) => rows.find((row) => row.id === id));
  if (
    !selected.length ||
    selected.length > 100 ||
    new Set(ids).size !== ids.length ||
    selected.some((row) => !historySelectable(row, view))
  )
    throw Error('请选择当前可操作的创作；任务未结束或删除中的记录不能选择。');
  return selected.map(({ id, version }) => ({ id, expected_version: version }));
}
function totals(value, keys) {
  if (!value || keys.some((key) => !integer(value[key]))) fail();
  return Object.fromEntries(keys.map((key) => [key, value[key]]));
}
export function checkedHistoryPreview(value, request, now = Date.now() / 1000) {
  if (
    !value ||
    !identifier(value.preview_id) ||
    value.action !== request.action ||
    !HISTORY_ACTIONS[value.action] ||
    !Number.isFinite(value.expires_at) ||
    value.expires_at <= now ||
    value.ledger_retained !== true ||
    typeof value.review_hold_release_required !== 'boolean' ||
    !Array.isArray(value.items) ||
    value.items.length > 10000 ||
    !Array.isArray(value.warnings) ||
    value.warnings.length > 10000
  )
    fail();
  const items = value.items.map((item) => {
    if (
      !identifier(item.id) ||
      !integer(item.expected_version) ||
      item.expected_version < 1 ||
      typeof item.eligible !== 'boolean'
    )
      fail();
    return { ...item };
  });
  if (new Set(items.map((item) => item.id)).size !== items.length) fail();
  if (
    request.action !== 'empty_trash' &&
    (items.length !== request.sessions.length ||
      request.sessions.some(
        (wanted) =>
          !items.some(
            (item) => item.id === wanted.id && item.expected_version === wanted.expected_version,
          ),
      ))
  )
    fail();
  return {
    ...value,
    items,
    removed: totals(value.removed, [
      'sessions',
      'turns',
      'cards',
      'assets',
      'artifacts',
      'objects',
      'bytes',
    ]),
    kept: totals(value.kept, ['assets', 'artifacts', 'objects', 'bytes']),
  };
}
export function checkedHistoryResult(value, operation) {
  if (
    !value ||
    !identifier(value.action_id) ||
    value.action !== operation.action ||
    !['pending', 'completed'].includes(value.state) ||
    value.ledger_retained !== true ||
    (operation.actionId && operation.actionId !== value.action_id) ||
    !Array.isArray(value.items) ||
    value.items.length !== operation.items.length
  )
    fail();
  if (
    new Set(value.items.map((item) => item.id)).size !== value.items.length ||
    operation.items.some((wanted) => !value.items.some((item) => item.id === wanted.id))
  )
    fail();
  for (const item of value.items) {
    if (!states.has(item.trash_state) || !['completed', 'pending', 'blocked'].includes(item.state))
      fail();
    if (
      value.state === 'completed' &&
      item.state !== 'blocked' &&
      (item.state !== 'completed' ||
        (value.action === 'trash'
          ? item.trash_state !== 'trashed'
          : value.action === 'restore'
            ? item.trash_state !== 'active'
            : item.trash_state !== 'purged'))
    )
      fail();
  }
  return {
    ...value,
    removed: totals(value.removed, [
      'sessions',
      'turns',
      'cards',
      'assets',
      'artifacts',
      'objects',
      'bytes',
    ]),
    kept: totals(value.kept, ['assets', 'artifacts', 'objects', 'bytes']),
  };
}
export function purgeCountdown(row, asOf, locale = 'zh') {
  if (row.trash_state === 'purging') return translate(locale, 'history.reason15');
  if (row.trash_state === 'purged') return translate(locale, 'history.reason20');
  if (row.review_hold) return translate(locale, 'history.reason21');
  if (row.auto_purge_enabled !== true)
    return row.purge_blocked_reason
      ? translate(locale, 'history.reason22')
      : translate(locale, 'history.reason23');
  if (!Number.isFinite(row.purge_at) || !Number.isFinite(asOf))
    return translate(locale, 'history.reason24');
  const seconds = row.purge_at - asOf;
  if (seconds <= 0) return translate(locale, 'history.reason25');
  return seconds >= 86400
    ? translate(locale, 'history.countdown.days', { count: Math.ceil(seconds / 86400) })
    : translate(locale, 'history.countdown.hours', {
        count: Math.max(1, Math.ceil(seconds / 3600)),
      });
}
export function historyBytes(value, locale = 'zh') {
  if (!integer(value)) return translate(locale, 'legacy.historyBytesUnknown');
  return value >= 1024 ** 3
    ? (value / 1024 ** 3).toFixed(2) + ' GiB'
    : value >= 1024 ** 2
      ? (value / 1024 ** 2).toFixed(1) + ' MiB'
      : value.toLocaleString() + ' B';
}
