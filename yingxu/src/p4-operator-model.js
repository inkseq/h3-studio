import { formatMoneyMicros } from './money.js';
import { operatorReasonCodes } from './i18n/operatorReasonCatalogs.js';
import { translate } from './i18n/locale.js';
import { dstackProvider, dstackMode, dstackLifecycle } from './dstack-presentation.js';

export { dstackProvider };
export const modeLabel = (mode, locale = 'zh') =>
  locale === 'zh'
    ? mode === 'both'
      ? 'FL / REF · 可切换'
      : dstackMode(mode)
    : ['fl', 'ref', 'both'].includes(mode)
      ? translate(locale, 'operator.display.mode.' + mode)
      : translate(locale, 'operator.more.modeUnknown');
export const finite = (value) => typeof value === 'number' && Number.isFinite(value);
// Readback renders can occur after the timer tick which began the request.
// Allow small server/client clock skew, retaining the age and future guards.
export function workspaceStale(snapshot, now = Date.now()) {
  return (
    !!snapshot &&
    (!finite(snapshot.observed_at) ||
      !finite(now) ||
      now - snapshot.observed_at * 1000 > 30000 ||
      snapshot.observed_at > (now + 5000) / 1000)
  );
}
export const money = (value) => (finite(value) ? formatMoneyMicros(Math.round(value)) : '—');
export const time = (value, locale = 'zh') =>
  finite(value) ? new Date(value * 1000).toLocaleString(locale === 'zh' ? 'zh-CN' : 'en') : '—';
export const elapsed = (value, locale = 'zh') =>
  !finite(value)
    ? '—'
    : translate(
        locale,
        'operator.display.' + (value < 60 ? 'seconds' : value < 3600 ? 'minutes' : 'hours'),
        {
          value:
            value < 60
              ? Math.round(value)
              : value < 3600
                ? Math.round(value / 60)
                : (value / 3600).toFixed(1),
        },
      );
export const terminal = (node) => node?.state === 'stopped' || node?.lifecycle_phase === 'stopped';
export const stopping = (node) =>
  !terminal(node) &&
  (node?.desired_state === 'stopped' || ['draining', 'stopping'].includes(node?.state));
export function phase(node, locale = 'zh') {
  if (terminal(node))
    return { id: 'stopped', label: translate(locale, 'workspace.sharedDetail1'), tone: 'muted' };
  if (stopping(node))
    return { id: 'stopping', label: translate(locale, 'workspace.sharedDetail2'), tone: 'muted' };
  if (node?.current_job)
    return { id: 'running', label: translate(locale, 'workspace.sharedDetail3'), tone: 'good' };
  if (node?.ready === true && node?.observation_fresh === true)
    return { id: 'ready', label: translate(locale, 'workspace.sharedDetail4'), tone: 'good' };
  const id = node?.lifecycle_phase;
  return {
    id: id || 'preparing',
    label:
      {
        renting: translate(locale, 'workspace.sharedDetail5'),
        downloading_weights: translate(locale, 'workspace.sharedDetail6'),
        loading_model: translate(locale, 'workspace.sharedDetail7'),
        preparing: translate(locale, 'workspace.sharedDetail8'),
        starting: translate(locale, 'workspace.sharedDetail8'),
        runtime_unconfirmed: translate(locale, 'workspace.sharedDetail8'),
        ready: translate(locale, 'workspace.sharedDetail8'),
      }[id] || translate(locale, 'workspace.sharedDetail8'),
    tone: 'warm',
  };
}
const preparationLabels = {
  renting: '申请机器中',
  pulling_image: '拉取镜像中',
  downloading_weights: '下载权重中',
  starting_runtime: '启动推理服务',
  verifying_runtime: '检查运行接口',
  loading_model: '加载模型中',
  awaiting_first_job: '等待首个任务加载模型',
  ready: '就绪',
  running_job: '正在生成',
  stopping: '停止中',
  stopped: '已停止',
  preparing: '等待准备观测',
};
const nonnegative = (value) => finite(value) && value >= 0;
const diagnosticCode = (value) =>
  typeof value === 'string' && /^[a-zA-Z0-9_.:-]{1,160}$/.test(value) ? value : null;
export function bytes(value) {
  if (!nonnegative(value)) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let amount = value,
    index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index++;
  }
  return (
    (index === 0 ? Math.round(amount) : amount.toFixed(amount >= 100 ? 0 : amount >= 10 ? 1 : 2)) +
    ' ' +
    units[index]
  );
}
export function transferProgress(value, locale = 'zh') {
  if (!value || typeof value !== 'object')
    return { label: translate(locale, 'operator.more.downloadUnknown'), percent: null };
  const complete = nonnegative(value.bytes_complete) ? value.bytes_complete : null,
    total = nonnegative(value.bytes_total) && value.bytes_total > 0 ? value.bytes_total : null;
  const files =
      Number.isSafeInteger(value.files_complete) && value.files_complete >= 0
        ? value.files_complete
        : null,
    fileTotal =
      Number.isSafeInteger(value.files_total) && value.files_total > 0 ? value.files_total : null;
  const prefix =
    value.basis === 'download_and_extract'
      ? translate(locale, 'operator.more.pullExtract')
      : value.basis === 'completed_files'
        ? translate(locale, 'operator.more.completedFiles')
        : '';
  const amount =
    complete !== null
      ? total !== null && complete <= total
        ? prefix + bytes(complete) + ' / ' + bytes(total)
        : (prefix || translate(locale, 'operator.more.downloaded')) + bytes(complete)
      : translate(locale, 'operator.more.downloadBytesUnknown');
  const fileAmount =
    files === null
      ? ''
      : fileTotal !== null && files <= fileTotal
        ? translate(locale, 'operator.more.fileProgress', { complete: files, total: fileTotal })
        : translate(locale, 'operator.more.files', { count: files });
  // A progress bar is only shown for a reported percentage backed by a valid
  // known total. File/byte counters are not converted into an invented rate.
  const percent =
    finite(value.percent) &&
    value.percent >= 0 &&
    value.percent <= 100 &&
    complete !== null &&
    total !== null &&
    complete <= total
      ? value.percent
      : null;
  const downloaded = nonnegative(value.downloaded_bytes)
      ? translate(locale, 'operator.more.downloadAmount', { bytes: bytes(value.downloaded_bytes) })
      : '',
    extraction = nonnegative(value.extracted_bytes)
      ? translate(locale, 'operator.more.extractedAmount', { bytes: bytes(value.extracted_bytes) })
      : '';
  return {
    label: amount + (fileAmount ? ' · ' + fileAmount : '') + downloaded + extraction,
    percent,
  };
}
export function preparationView(node, locale = 'zh') {
  const source =
      node?.preparation && typeof node.preparation === 'object' ? node.preparation : null,
    authority = phase(node, locale);
  let id = source && Object.hasOwn(preparationLabels, source.phase) ? source.phase : authority.id;
  if (terminal(node)) id = 'stopped';
  else if (stopping(node)) id = 'stopping';
  else if (node?.current_job)
    id =
      source?.model?.state === 'loading' && source?.stale !== true
        ? 'loading_model'
        : 'running_job';
  else if (authority.id === 'ready')
    id = source?.model?.state === 'loading' && source?.stale !== true ? 'loading_model' : 'ready';
  const status = {
    id,
    label: Object.hasOwn(preparationLabels, id)
      ? translate(locale, 'workspace.preparation.' + id)
      : authority.label,
    tone: ['ready', 'running_job', 'running'].includes(id)
      ? 'good'
      : ['stopping', 'stopped'].includes(id)
        ? 'muted'
        : 'warm',
  };
  if (id === 'ready' && authority.id !== 'ready' && authority.id !== 'running')
    status.label = translate(locale, 'workspace.preparationDetail0');
  const elapsedSeconds = nonnegative(source?.elapsed_seconds) ? source.elapsed_seconds : null,
    phaseElapsed = nonnegative(source?.phase_elapsed_seconds) ? source.phase_elapsed_seconds : null;
  const download = transferProgress(source?.download, locale),
    image = transferProgress(source?.image, locale);
  const modelState = ['not_loaded', 'loading', 'loaded', 'not_observed'].includes(
    source?.model?.state,
  )
    ? source.model.state
    : 'not_observed';
  const modelLabel =
    modelState === 'not_loaded' && terminal(node)
      ? translate(locale, 'workspace.preparationDetail1')
      : {
          not_loaded: translate(locale, 'workspace.preparationDetail2'),
          loading: translate(locale, 'workspace.preparationDetail3'),
          loaded: translate(locale, 'workspace.preparationDetail4'),
          not_observed: translate(locale, 'workspace.preparationDetail5'),
        }[modelState];
  const reasonCode = diagnosticCode(source?.reason_code ?? node?.reason_code);
  let reasonText =
    typeof source?.reason_text === 'string' && source.reason_text.trim()
      ? source.reason_text.trim().slice(0, 600)
      : null;
  // The service emits this closed diagnostic projection; user content is never translated.
  if (locale !== 'zh' && reasonText) {
    const reason = operatorReasonCodes.includes(reasonCode)
      ? reasonCode
      : reasonCode
        ? 'unknown'
        : source?.stale === true
          ? 'stale'
          : source?.stalled === true
            ? 'stalled'
            : null;
    if (reason) reasonText = translate(locale, 'operator.reason.' + reason);
  }
  const stale = source?.stale === true,
    stalled = source?.stalled === true;
  const observationAge = nonnegative(source?.observation_age_seconds)
    ? source.observation_age_seconds
    : null;
  return {
    source,
    status,
    download,
    image,
    modelState,
    modelLabel,
    elapsedSeconds,
    phaseElapsed,
    reasonCode,
    reasonText,
    stale,
    stalled,
    observationAge,
  };
}
export function preparationSteps(view, locale = 'zh') {
  const steps = [
    ['renting', translate(locale, 'workspace.preparationDetail6')],
    ['pulling_image', translate(locale, 'workspace.preparationDetail7')],
    ['downloading_weights', translate(locale, 'workspace.preparationDetail8')],
    ['starting_runtime', translate(locale, 'workspace.preparationDetail9')],
    ['loading_model', translate(locale, 'workspace.preparationDetail10')],
    ['ready', translate(locale, 'workspace.preparationDetail11')],
  ];
  const current =
    {
      verifying_runtime: 'starting_runtime',
      awaiting_first_job: 'loading_model',
      running_job: 'ready',
      running: 'ready',
    }[view.status.id] || view.status.id;
  const result = steps.map(([id, label]) => ({
    id,
    label,
    current: id === current,
    pending: id === 'loading_model' && view.status.id === 'awaiting_first_job',
  }));
  if (
    view.status.id === 'ready' &&
    view.status.label !== translate(locale, 'workspace.preparationDetail12')
  )
    result[result.length - 1].label = translate(locale, 'workspace.preparationDetail13');
  if (['running', 'running_job'].includes(view.status.id))
    result[result.length - 1].label = translate(locale, 'workspace.preparationDetail14');
  if (['stopping', 'stopped'].includes(current))
    result.push({
      id: current,
      label: translate(locale, 'workspace.preparation.' + current),
      current: true,
      pending: false,
    });
  return result;
}
export function keepText(node, locale = 'zh') {
  const policy = dstackLifecycle(node);
  if (!policy)
    return finite(node?.hard_deadline)
      ? translate(locale, 'operator.more.until', { time: time(node.hard_deadline, locale) })
      : translate(locale, 'operator.more.policyPending');
  if (policy.keep_on === 'until_stopped') return translate(locale, 'operator.more.manualStop');
  const idle = translate(locale, 'operator.more.idle', {
    minutes: Math.round(policy.idle_shutdown_seconds / 60),
  });
  return policy.keep_on === 'until'
    ? translate(locale, 'operator.more.keepUntil', { time: time(policy.hold_until, locale) })
    : idle;
}
export function rate(node) {
  const quoted = node?.quoted_hourly_cost_microusd ?? node?.hourly_price_microusd;
  return finite(quoted)
    ? { value: quoted, label: money(quoted), basis: '供应商报价' }
    : {
        value: node?.hourly_cost_microusd,
        label: finite(node?.hourly_cost_microusd) ? '≤ ' + money(node.hourly_cost_microusd) : '—',
        basis: '每小时上限',
      };
}
export function costLabel(basis, locale = 'zh') {
  const key =
    {
      settled: 'settled',
      actual: 'actual',
      dstack_estimate: 'estimate',
      quoted_estimate: 'estimate',
      runtime_quote_estimate: 'estimate',
      estimated_compute: 'estimate',
      approved_ceiling: 'ceiling',
      approved_ceiling_estimate: 'ceiling',
      provisioning_quote_estimate: 'estimate',
      unavailable: 'unknown',
    }[basis] || 'unknown';
  return translate(locale, 'operator.display.cost.' + key);
}
export function queueLabel(queue, locale = 'zh') {
  if (!queue?.waiting) return translate(locale, 'operator.more.noWaiting');
  if (!finite(queue.rough_wait_seconds)) return translate(locale, 'operator.more.waitCompatible');
  return queue.rough_wait_seconds <= 0
    ? translate(locale, 'operator.more.canAccept')
    : translate(locale, 'operator.more.roughWait', {
        time: elapsed(queue.rough_wait_seconds, locale),
      });
}
export function initialRows(profiles, { profileId, mode, one = false } = {}) {
  const selected =
    profiles.find((profile) => profile.id === profileId) ||
    profiles.find((profile) => profile.mode === mode) ||
    profiles[0];
  if (!selected) return [];
  const make = (profile) => ({
    id: globalThis.crypto.randomUUID(),
    profile_id: profile.id,
    quantity: 1,
  });
  if (one || profileId || mode) return [make(selected)];
  const ref = profiles.find(
    (profile) =>
      profile.model_id === selected.model_id &&
      profile.backend === selected.backend &&
      profile.mode === 'ref',
  );
  const fl = profiles.find(
    (profile) =>
      profile.model_id === selected.model_id &&
      profile.backend === selected.backend &&
      profile.mode === 'fl',
  );
  return fl && ref ? [make(fl), make(ref)] : [make(selected)];
}
export function lifetimeBody({ keep_on = 'idle', idle_minutes = 10, until }, now = Date.now()) {
  const idle = Number(idle_minutes) * 60;
  if (
    !Number.isInteger(idle) ||
    idle < 1 ||
    idle > 86400 ||
    !['idle', 'until', 'until_stopped'].includes(keep_on)
  )
    return null;
  const seconds = keep_on === 'until' ? Math.ceil((Date.parse(until) - now) / 1000) : 0;
  if (
    !Number.isInteger(seconds) ||
    seconds < 0 ||
    seconds > 86400 ||
    (keep_on === 'until' && seconds < 1)
  )
    return null;
  return { keep_on, hold_seconds: seconds, idle_shutdown_seconds: idle };
}
export function previewCurrent(preview, selection, now = Date.now()) {
  const policy = dstackLifecycle(preview);
  if (
    Object.hasOwn(selection || {}, 'test_task_id') &&
    (policy?.test_gpu_guard?.version !== 2 ||
      policy.test_gpu_guard.task_id !== selection.test_task_id)
  )
    return false;
  return (
    preview?.capacity_backend === 'dstack-v1' &&
    preview.can_start === true &&
    typeof preview.preview_id === 'string' &&
    finite(preview.expires_at) &&
    preview.expires_at * 1000 > now &&
    policy &&
    typeof selection?.profile_id === 'string' &&
    policy.keep_on === selection.keep_on &&
    (!Object.hasOwn(selection, 'idle_shutdown_seconds') ||
      policy.idle_shutdown_seconds === selection.idle_shutdown_seconds) &&
    (!preview.profile_id || preview.profile_id === selection.profile_id) &&
    (!Object.hasOwn(preview, 'group_id') || preview.group_id === selection.group_id) &&
    finite(preview.hourly_cost_microusd) &&
    preview.hourly_cost_microusd > 0 &&
    (!Object.hasOwn(selection, 'max_price_microusd') ||
      (preview.max_price_microusd === selection.max_price_microusd &&
        preview.hourly_cost_microusd === selection.max_price_microusd)) &&
    finite(preview.reservation_microusd) &&
    preview.reservation_microusd >= 0
  );
}
export function testTaskBody(required, value) {
  if (!required) return {};
  return typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,100}$/.test(value)
    ? { test_task_id: value }
    : null;
}
export function batchRows(rows, profiles) {
  if (!Array.isArray(rows) || !rows.length || rows.length > 16) return null;
  const result = [];
  for (const row of rows) {
    if (
      !Number.isInteger(row.quantity) ||
      row.quantity < 1 ||
      row.quantity > 8 ||
      !profiles.some((profile) => profile.id === row.profile_id)
    )
      return null;
    for (let count = 0; count < row.quantity; count++)
      result.push({ profile_id: row.profile_id, row_id: row.id, number: count + 1 });
  }
  return result.length <= 16 ? result : null;
}
export function groupedNodes(nodes, groups, locale = 'zh') {
  const live = (nodes || []).filter((node) => !terminal(node));
  const buckets = (groups || []).map((group) => ({
    ...group,
    nodes: live.filter((node) => node.group_id === group.id),
  }));
  const ungrouped = live.filter(
    (node) => !node.group_id || !groups.some((group) => group.id === node.group_id),
  );
  if (ungrouped.length || !buckets.length)
    buckets.push({
      id: null,
      name: translate(locale, 'operator.more.ungrouped'),
      nodes: ungrouped,
    });
  return buckets;
}
export function todayMetrics(node, locale = 'zh') {
  const usage = node?.usage || {};
  return {
    cost: money(usage.today_cost_microusd),
    count: Number.isInteger(usage.today_video_count) ? usage.today_video_count : '—',
    basis: costLabel(usage.today_cost_basis || usage.cost_basis, locale),
  };
}
