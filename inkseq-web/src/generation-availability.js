import { translate } from './i18n/locale.js';
/* Advisory capacity only. Submission and original-operation recovery stay server-owned. */
const MODE_LABELS = { fl: '首尾帧', ref: '全能参考' };
const STATES = new Set([
  'ready',
  'busy',
  'starting',
  'unavailable',
  'disabled',
  'unknown',
  'stale',
]);
export function modeAvailability(
  snapshot,
  capabilities,
  settings = {},
  mode,
  now = Date.now(),
  locale = 'zh',
) {
  const selected = capabilities?.recipes?.find((item) => item.id === settings.recipe_id);
  mode = mode || selected?.mode;
  const result = (state, available = false, reason_code = null) => ({
    mode,
    state,
    available,
    reason_code,
    label: translate(locale, 'availability.' + state),
    message: translate(locale, 'availability.' + state + 'Message'),
  });
  if (
    !['fl', 'ref'].includes(mode) ||
    !selected ||
    snapshot?.version !== 1 ||
    snapshot.advisory_only !== true ||
    !Array.isArray(snapshot.profiles)
  )
    return result('unknown');
  const observed = snapshot.observed_at,
    expires = snapshot.expires_at,
    seconds = now / 1000;
  if (
    !Number.isFinite(observed) ||
    !Number.isFinite(expires) ||
    expires <= observed ||
    expires - observed > 10.01 ||
    observed > seconds + 5
  )
    return result('unknown');
  if (seconds >= expires) return result('stale');
  // Never borrow another profile or the service default for an authored choice.
  const profiles = snapshot.profiles.filter(
    (item) => item?.deployment_profile_id === (settings.deployment_profile_id ?? null),
  );
  const row = profiles.length === 1 ? profiles[0].modes?.[mode] : null;
  const recipe =
    mode === selected.mode ? selected : capabilities.recipes.find((item) => item.mode === mode);
  if (
    !row ||
    row.recipe_id !== recipe?.id ||
    !STATES.has(row.state) ||
    row.state === 'stale' ||
    typeof row.available !== 'boolean'
  )
    return result('unknown');
  const available = ['ready', 'busy'].includes(row.state);
  if (row.available !== available) return result('unknown');
  // CR43 demand start: no ready machine, but the server says this account may
  // queue and a machine will start. `available` here gates the ordinary
  // preflight/confirm action only; the server still checks everything.
  if (!available && row.can_queue === true && ['unavailable', 'starting'].includes(row.state))
    return { ...result('cold', true, row.reason_code || null), physical: false };
  return result(row.state, available, row.reason_code || null);
}

export function requireModeAvailable(snapshot, capabilities, settings, mode, now) {
  const status = modeAvailability(snapshot, capabilities, settings, mode, now);
  if (!status.available)
    throw Error(
      `${MODE_LABELS[status.mode] || '当前模式'}：${status.message} 原草稿和素材保持不变。`,
    );
  return status;
}
