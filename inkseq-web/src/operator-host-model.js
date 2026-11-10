import { translate } from './i18n/locale.js';
import { referenceOffers } from './operator-budget-model.js';

export const hostPolicyOptions = [
  { value: 'default', label: '按默认规则', reason: 'operator_host_default' },
  { value: 'prefer', label: '优先选用', reason: 'operator_host_preferred' },
  { value: 'deny', label: '禁止选用', reason: 'operator_host_denied' },
];
// Hosts are keyed on provider + physical id (h3-studio#900): a Vast machine id or a
// Lium executor id. A Lium host can also be denied for bad output.
export const hostProviders = ['vastai', 'lium'];
const HOST_SOURCES = { vastai: 'vast_machine_id', lium: 'lium_executor_id' };
const POLICY_ONLY_SOURCES = { vastai: 'operator_supplied_numeric', lium: 'operator_supplied_id' };
const EXECUTOR = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export const knownHostProvider = (value) => hostProviders.includes(value);
export const physicalHostId = (value, provider = 'vastai') =>
  provider === 'lium'
    ? typeof value === 'string' && EXECUTOR.test(value)
    : provider === 'vastai' &&
      ((typeof value === 'string' && /^[1-9][0-9]{0,17}$/.test(value)) ||
        (Number.isSafeInteger(value) && value > 0));
// One choice per policy and reason, so the editor is a single select.
export const hostPolicyChoices = [
  ...hostPolicyOptions.map((option) => ({ ...option, key: option.value, policy: option.value })),
  { key: 'deny_bad_output', policy: 'deny', reason: 'operator_host_bad_output' },
];
export const hostPolicyChoice = (policyValue) =>
  hostPolicyChoices.find(
    (choice) => choice.policy === policyValue.manual && choice.reason === policyValue.reason_code,
  )?.key ?? policyValue.manual;
export const hostText = (value, locale = 'zh') =>
  typeof value === 'string' &&
  value.trim().length > 0 &&
  value.length <= 200 &&
  !/[\r\n]/.test(value)
    ? value
    : translate(locale, 'operatorPanel.unknown');
// Known policy reasons read as words; anything else shows its code (h3-studio#901).
const HOST_REASONS = [
  'operator_host_default',
  'operator_host_preferred',
  'operator_host_denied',
  'operator_host_bad_output',
  'automatic_slow_download',
];
export const hostReasonText = (value, locale = 'zh') =>
  HOST_REASONS.includes(value)
    ? translate(locale, 'operatorPanel.reason.' + value)
    : hostText(value, locale);
const nonnegative = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const count = (value) => Number.isSafeInteger(value) && value >= 0;
const snapshot = (value) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const policy = (value) => hostPolicyOptions.some((option) => option.value === value);
export const knownNumber = (value) => (nonnegative(value) ? value : null);
export const secondsLabel = (value, locale = 'zh') =>
  nonnegative(value)
    ? translate(locale, 'operator.display.seconds', { value: Math.round(value) })
    : translate(locale, 'operator.display.notObserved');
export function failureLabel(value, locale = 'zh') {
  if (!value) return translate(locale, 'operatorPanel.noFailure');
  const stages = {
    provision: '供应商准备',
    provisioning: '供应商准备',
    image_pull: '镜像下载',
    weights_download: '权重下载',
    model_load: '模型加载',
    generate: '生成',
    collect: '结果收集',
    validate: '媒体校验',
    upload: '结果上传',
    disconnect: '连接中断',
    ssh_loss: 'SSH 连接丢失',
    oom: '内存不足',
  };
  if (value.reason_code === 'waiting_instance_limit_exceeded')
    return translate(locale, 'operatorPanel.waitingFailure');
  if (value.reason_code === 'model_load_unconfirmed' || value.stage === 'model_load_unconfirmed')
    return translate(locale, 'operatorPanel.modelFailure');
  if (value.reason_code === 'wangp_profile_one_visible_gpu_required')
    return translate(locale, 'operatorPanel.cudaFailure');
  return translate(
    locale,
    stages[value.stage]
      ? 'operatorPanel.failureStage.' + value.stage
      : 'operatorPanel.unknownFailure',
  );
}
export function inventoryOutcome(item, locale = 'zh') {
  if (item.state === 'pending')
    return { state: 'pending', label: translate(locale, 'operatorPanel.inventoryPending') };
  if (item.state === 'empty') {
    const hostEmpty = item.reasonCode === 'dstack_no_compatible_host_offers';
    return {
      state: 'empty',
      label: translate(
        locale,
        hostEmpty ? 'operatorPanel.inventoryHostEmpty' : 'operatorPanel.inventoryEmpty',
      ),
      message: translate(
        locale,
        hostEmpty ? 'operatorPanel.inventoryHostHint' : 'operatorPanel.inventoryEmptyHint',
      ),
    };
  }
  if (item.state === 'failed')
    return { state: 'unknown', label: translate(locale, 'operatorPanel.inventoryFailed') };
  return { state: 'ready', label: translate(locale, 'operatorPanel.inventoryReturned') };
}
export function inventoryError(error) {
  const code = error?.detail?.code || error?.detail?.detail;
  const definiteEmpty =
    ['dstack_no_compatible_offers', 'dstack_no_compatible_host_offers'].includes(code) &&
    [undefined, 409].includes(error?.status);
  return definiteEmpty
    ? { state: 'empty', reasonCode: code }
    : { state: 'failed', error: error?.message || '查询未完成，不能当作零库存。' };
}
export function referenceHostOffers(offers = [], profileId, now = Date.now()) {
  return referenceOffers(offers).map((group) => {
    const members = offers.filter((offer) => referenceOffers([offer])[0]?.key === group.key);
    return {
      ...group,
      members,
      confirmed: members.filter(
        (offer) =>
          offer.inventory_confirmed === true &&
          (offer.backend !== 'vastai' || hostInventoryConfirmed(offer, profileId, now)),
      ).length,
    };
  });
}
export function validHostPolicy(value) {
  return (
    value &&
    policy(value.manual) &&
    policy(value.effective) &&
    count(value.revision) &&
    snapshot(value.snapshot)
  );
}
export function hostPolicyBody(host, selection) {
  const choice = hostPolicyChoices.find((value) => value.key === selection);
  if (!validHostSummary(host, { allowPolicyOnly: true }) || !choice) return null;
  return {
    provider: host.provider,
    machine_id: String(host.machine_id),
    policy: choice.policy,
    reason_code: choice.reason,
    expected_revision: host.policy.revision,
    expected_snapshot: host.policy.snapshot,
  };
}
export function validHostPolicyBody(body) {
  return (
    body &&
    Object.keys(body).sort().join(',') ===
      'expected_revision,expected_snapshot,machine_id,policy,provider,reason_code' &&
    knownHostProvider(body.provider) &&
    physicalHostId(body.machine_id, body.provider) &&
    typeof body.machine_id === 'string' &&
    count(body.expected_revision) &&
    snapshot(body.expected_snapshot) &&
    hostPolicyChoices.some(
      (choice) => choice.policy === body.policy && choice.reason === body.reason_code,
    )
  );
}
export function hostPolicyReceipt(value, body) {
  return (
    validHostPolicyBody(body) &&
    value?.state === 'completed' &&
    typeof value.command_id === 'string' &&
    !!value.command_id &&
    typeof value.changed === 'boolean' &&
    value.host?.provider === body.provider &&
    String(value.host.machine_id) === body.machine_id &&
    validHostPolicy(value.policy) &&
    value.policy.manual === body.policy &&
    (value.changed
      ? value.policy.revision > body.expected_revision &&
        value.policy.snapshot !== body.expected_snapshot &&
        typeof value.audit_event_id === 'string' &&
        !!value.audit_event_id
      : value.policy.revision === body.expected_revision &&
        value.policy.snapshot === body.expected_snapshot)
  );
}
export function validHostPage(value, account, { admin = false, provider = 'vastai' } = {}) {
  return (
    value?.account === account &&
    value.admin === admin &&
    value.provider === provider &&
    (admin || provider === 'vastai') &&
    nonnegative(value.as_of) &&
    count(value.offset) &&
    count(value.limit) &&
    value.limit > 0 &&
    value.limit <= 100 &&
    typeof value.has_more === 'boolean' &&
    Array.isArray(value.hosts) &&
    value.hosts.length <= value.limit &&
    new Set(value.hosts.map((host) => String(host?.machine_id))).size === value.hosts.length &&
    value.hosts.every(
      (host) => host?.provider === provider && validHostSummary(host, { allowPolicyOnly: admin }),
    )
  );
}
export function validHostSummary(host, { allowPolicyOnly = false } = {}) {
  const provider = host?.provider;
  const observed =
    host?.identity_confirmed === true && host.physical_id_source === HOST_SOURCES[provider];
  const policyOnly =
    allowPolicyOnly &&
    host?.identity_confirmed === false &&
    host.physical_id_source === POLICY_ONLY_SOURCES[provider] &&
    host.runs === 0 &&
    host.failure_count === 0 &&
    host.last_observed_at === null &&
    host.latest_observation === null &&
    host.last_failure === null &&
    host.groups?.length === 0 &&
    host.rentals?.length === 0;
  return (
    knownHostProvider(provider) &&
    physicalHostId(host.machine_id, provider) &&
    (observed || policyOnly) &&
    validHostPolicy(host.policy) &&
    count(host.runs) &&
    count(host.failure_count) &&
    (host.failure_rate === null || (nonnegative(host.failure_rate) && host.failure_rate <= 1)) &&
    (host.last_observed_at === null || nonnegative(host.last_observed_at)) &&
    Array.isArray(host.groups) &&
    host.groups.length <= 1000 &&
    host.groups.every(validHostGroup) &&
    (host.latest_observation === null ||
      (host.latest_observation &&
        [
          'observed_at',
          'gpu_count',
          'allocated_ram_mib',
          'allocated_vram_mib',
          'cpu',
          'reliability',
          'quoted_hourly_microusd',
        ].every(
          (key) =>
            host.latest_observation[key] === null || nonnegative(host.latest_observation[key]),
        ))) &&
    Array.isArray(host.rentals) &&
    host.rentals.length <= 10 &&
    host.rentals.every(
      (row) =>
        row &&
        ['node_id', 'dstack_run_id', 'state', 'stage', 'region', 'profile_id', 'mode'].every(
          (key) => row[key] === null || typeof row[key] === 'string',
        ) &&
        ['observed_at', 'quoted_hourly_microusd'].every(
          (key) => row[key] === null || nonnegative(row[key]),
        ),
    )
  );
}
export function hostInventoryConfirmed(offer, profileId, now = Date.now()) {
  const rows = offer?.host_history;
  if (!physicalHostId(offer?.offer_id) || !Array.isArray(rows) || rows.length !== 1) return false;
  const host = rows[0];
  return (
    validHostSummary(host) &&
    String(host.offer_id) === String(offer.offer_id) &&
    host.capacity_profile_id === profileId &&
    host.policy.effective !== 'deny' &&
    nonnegative(host.offer_observed_at) &&
    host.offer_observed_at * 1000 <= now &&
    nonnegative(host.selection_expires_at) &&
    host.selection_expires_at * 1000 > now
  );
}
export function validHostGroup(group) {
  return (
    group &&
    typeof group.profile_id === 'string' &&
    group.profile_id.length > 0 &&
    group.profile_id.length <= 200 &&
    ['fl', 'ref', 'both'].includes(group.mode) &&
    count(group.samples) &&
    count(group.cold_start_samples) &&
    count(group.generation_samples) &&
    group.cost_basis === 'quote_estimate' &&
    [
      'median_cold_start_s',
      'median_generation_s',
      'cost_per_video_microusd',
      'download_mb_s',
    ].every((key) => group[key] === null || nonnegative(group[key]))
  );
}
export function validHostAudit(value, account) {
  return (
    value?.account === account &&
    value.admin === true &&
    nonnegative(value.as_of) &&
    count(value.offset) &&
    count(value.limit) &&
    value.limit > 0 &&
    value.limit <= 100 &&
    typeof value.has_more === 'boolean' &&
    Array.isArray(value.events) &&
    value.events.length <= value.limit &&
    new Set(value.events.map((event) => event?.id)).size === value.events.length &&
    value.events.every(
      (event) =>
        typeof event.id === 'string' &&
        !!event.id &&
        nonnegative(event.created_at) &&
        knownHostProvider(event.provider) &&
        physicalHostId(event.machine_id, event.provider) &&
        [event.old, event.new].every(
          (row) => row && policy(row.manual) && count(row.revision) && snapshot(row.snapshot),
        ) &&
        typeof event.command_id === 'string' &&
        !!event.command_id,
    )
  );
}
