const seconds = (value) => (typeof value === 'number' && Number.isFinite(value) ? value : null);
export const dstackProvider = (value) =>
  ({ vast: 'Vast.ai', vastai: 'Vast.ai', runpod: 'RunPod' })[value] || value || '尚未确认';
export const dstackMode = (value) =>
  ({ fl: 'FL2VA 首尾帧', ref: 'Ref2VA 全能参考' })[value] || '模式尚未确认';

const between = (value, low, high) => Number.isSafeInteger(value) && value >= low && value <= high;
const uniqueIds = (ids) =>
  Array.isArray(ids) &&
  ids.length > 0 &&
  ids.every((id) => typeof id === 'string' && id) &&
  new Set(ids).size === ids.length;
// A held machine has a future hold time; every other keep-on rule holds nothing.
const validHold = (policy) =>
  seconds(policy.hold_until) !== null &&
  (policy.keep_on === 'until' ? policy.hold_until > 0 : policy.hold_until === 0);

export function dstackLifecycle(value) {
  const policy = value?.lifecycle;
  if (
    value?.hard_deadline !== null ||
    policy?.version !== 2 ||
    !['idle', 'until', 'until_stopped'].includes(policy.keep_on) ||
    !validHold(policy) ||
    !between(policy.idle_shutdown_seconds, 1, 86400) ||
    !between(policy.reservation_seconds, 120, 86400) ||
    seconds(policy.expected_runtime_s) === null ||
    policy.expected_runtime_s <= 0 ||
    policy.expected_runtime_s > 86400 ||
    !uniqueIds(policy.budget_account_ids) ||
    (Object.hasOwn(value, 'hold_until') && value.hold_until !== policy.hold_until)
  )
    return null;
  return policy;
}

export function dstackKeepOnSelection(keepOn, minutes) {
  if (!['idle', 'until', 'until_stopped'].includes(keepOn)) return null;
  if (keepOn === 'until' && (!Number.isInteger(minutes) || minutes < 1 || minutes > 1440))
    return null;
  return { keep_on: keepOn, hold_seconds: keepOn === 'until' ? minutes * 60 : 0 };
}

export function dstackLifetime(value) {
  const policy = dstackLifecycle(value);
  if (!policy)
    return typeof value?.hard_deadline === 'number'
      ? new Date(value.hard_deadline * 1000).toLocaleString('zh-CN') + ' 停止上限'
      : '运行策略尚未确认';
  if (policy.keep_on === 'until_stopped') return '保留至手动关机 · 受原预算限制';
  const idle = `空闲 ${policy.idle_shutdown_seconds / 60} 分钟后关机`;
  return policy.keep_on === 'until'
    ? `保留至 ${new Date(policy.hold_until * 1000).toLocaleString('zh-CN')}，之后${idle}`
    : idle;
}

export function dstackNodeStatus(node, now = Date.now()) {
  const observed = seconds(node.observed_at),
    deadline = seconds(node.hard_deadline);
  if (node.state === 'stopped')
    return {
      label:
        node.billing_state === 'no_charge_confirmed'
          ? '已结束 · 未产生费用'
          : '运行任务已结束 · 删除与费用待核对',
      tone: node.billing_state === 'no_charge_confirmed' ? 'neutral' : 'warn',
      group: node.billing_state === 'no_charge_confirmed' ? 'history' : 'review',
    };
  if (node.desired_state === 'stopped')
    return { label: '排空与停止中', tone: 'neutral', group: 'current' };
  if (
    !node.observation_fresh ||
    observed === null ||
    observed * 1000 > now ||
    now - observed * 1000 > 30000 ||
    node.state?.includes('unknown')
  )
    return { label: '状态待核对', tone: 'warn', group: 'review' };
  if (!dstackLifecycle(node) && (node.lifecycle || deadline === null || deadline * 1000 <= now))
    return {
      label: node.lifecycle || deadline === null ? '运行策略待核对' : '期限已到 · 等待停止核对',
      tone: 'warn',
      group: 'review',
    };
  if (node.ready === true && node.state === 'ready')
    return { label: '运行接口就绪', tone: 'good', group: 'current' };
  if (node.state === 'ready')
    return { label: '等待业务执行槽与准入', tone: 'neutral', group: 'current' };
  return {
    label:
      {
        reserved: '启动请求已保存',
        creating: '正在申请机器',
        starting: '正在准备机器',
        runtime_unconfirmed: '等待运行接口验收',
        busy: '正在处理原任务',
        draining: '正在排空',
        stopping: '停止中',
      }[node.state] || '等待状态核对',
    tone: 'neutral',
    group: 'current',
  };
}

export function dstackGroups(nodes = [], now = Date.now()) {
  const groups = { current: [], review: [], history: [] };
  for (const node of nodes) groups[dstackNodeStatus(node, now).group].push(node);
  return groups;
}

export function dstackPreviewCurrent(preview, profileId, selection, now = Date.now()) {
  const policy = dstackLifecycle(preview),
    expected =
      selection && typeof selection === 'object'
        ? { ...selection, profile_id: profileId }
        : { profile_id: profileId, ttl_seconds: selection };
  const selected = preview?.selection;
  const matches =
    !!selected &&
    Object.keys(selected).length === Object.keys(expected).length &&
    Object.entries(expected).every(([key, value]) => selected[key] === value);
  const lifetime = policy
    ? Number.isSafeInteger(preview.initial_reservation_seconds) &&
      preview.initial_reservation_seconds >= 120 &&
      preview.price_basis === 'approved_ceiling' &&
      preview.can_start === true &&
      !!selection &&
      typeof selection === 'object' &&
      Object.keys(selection).every((key) =>
        ['keep_on', 'hold_seconds', 'idle_shutdown_seconds'].includes(key),
      ) &&
      policy.keep_on === selection.keep_on &&
      dstackKeepOnSelection(selection.keep_on, selection.hold_seconds / 60)?.hold_seconds ===
        selection.hold_seconds &&
      (!Object.hasOwn(selection, 'idle_shutdown_seconds') ||
        policy.idle_shutdown_seconds === selection.idle_shutdown_seconds)
    : !preview?.lifecycle &&
      seconds(preview?.hard_deadline) !== null &&
      preview.hard_deadline * 1000 > now;
  return (
    !!preview &&
    matches &&
    lifetime &&
    typeof preview.preview_id === 'string' &&
    preview.capacity_backend === 'dstack-v1' &&
    seconds(preview.expires_at) !== null &&
    preview.expires_at * 1000 > now &&
    Number.isSafeInteger(preview.reservation_microusd) &&
    preview.reservation_microusd >= 0 &&
    Number.isSafeInteger(preview.hourly_cost_microusd) &&
    preview.hourly_cost_microusd > 0
  );
}
