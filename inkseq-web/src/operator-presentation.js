import {
  epochMs,
  nodeDeadline,
  nodeRemovalConfirmation,
  stateLabel,
  staleSnapshot,
} from './operator-model.js';

export function uniqueOperatorReasons(items = []) {
  const seen = new Set();
  return items.filter((item) => {
    const code = typeof item === 'string' ? item : item?.code;
    if (!code || seen.has(code)) return false;
    seen.add(code);
    return true;
  });
}
export function historicalNode(node) {
  const removal = nodeRemovalConfirmation(node);
  return (
    node?.state === 'destroyed' || removal?.reviewed === true || removal?.state === 'confirmed'
  );
}
export function operatorNodeGroup(node) {
  if (['current', 'pending_review', 'history'].includes(node?.record_group))
    return node.record_group;
  if (historicalNode(node)) return 'history';
  if (node?.review_required === true) return 'pending_review';
  const removal = nodeRemovalConfirmation(node);
  if (removal?.pending) return 'pending_review';
  if (
    ['creation_unknown', 'destroy_unknown'].includes(node?.state) ||
    ['blocked', 'failed', 'reconcile_required', 'bootstrap_reconciliation_required'].includes(
      node?.runtime_state,
    )
  )
    return 'pending_review';
  return 'current';
}
export function operatorNodeGroups(nodes = []) {
  const groups = { current: [], pending_review: [], history: [] };
  for (const node of nodes) groups[operatorNodeGroup(node)].push(node);
  for (const list of Object.values(groups))
    list.sort(
      (a, b) => (b.created_at ?? b.observed_at ?? 0) - (a.created_at ?? a.observed_at ?? 0),
    );
  return groups;
}
const phaseLabels = {
  model_download: '正在下载模型',
  model_download_waiting_for_shared_cache: '等待共享模型缓存',
  download: '正在下载',
  download_file: '正在下载模型文件',
  download_preflight: '检查下载条件',
  weights_ready: '模型文件已准备',
  preflight: '检查启动条件',
  checking_package: '检查运行包',
  clone_comfy: '准备运行源码',
  fetch_comfy: '获取运行源码',
  pin_comfy: '核对运行源码',
  install_dependencies: '安装依赖',
  dependency_download: '下载依赖',
  dependency_unpack: '解压依赖',
  dependency_install: '安装依赖',
  system_package_install: '安装系统依赖',
  system_package_restore: '恢复系统依赖',
  system_package_verification: '核对系统依赖',
  runtime_imports: '加载运行依赖',
  runtime_manifest: '核对模型清单',
  runtime_token: '核对服务认证',
  runtime_journal: '核对运行记录',
  runtime_inputs: '准备输入服务',
  runtime_host: '准备模型服务',
  runtime_verification: '核对运行环境',
  runtime_session_initialization: '初始化模型服务',
  runtime_http_service: '启动模型接口',
  runtime_start: '启动模型服务',
  start_comfy: '启动模型服务',
  runtime_ready: '模型接口已启动',
  comfy_ready: '模型接口已启动',
};
export function operatorBootstrap(node, now = Date.now()) {
  const bootstrap = node?.bootstrap,
    observed = epochMs(bootstrap?.observed_at);
  const freshness =
    observed === null || observed > now ? 'unknown' : now - observed > 60000 ? 'stale' : 'fresh';
  return {
    observed_at: bootstrap?.observed_at ?? null,
    freshness,
    historical: historicalNode(node),
    slots: (bootstrap?.slots || []).map((slot) => ({
      ...slot,
      label: phaseLabels[slot.phase] || '阶段尚未报告',
    })),
  };
}
export function operatorNodeStatus(node, now = Date.now()) {
  const removal = nodeRemovalConfirmation(node);
  if (removal) return { label: removal.label, tone: removal.tone };
  if (historicalNode(node)) return { label: '历史记录 · 已结束', tone: 'neutral' };
  if (unconfirmedObservation(node, now)) return { label: '状态待核对', tone: 'warn' };
  const boot = operatorBootstrap(node, now),
    phase = boot.slots.find((slot) => phaseLabels[slot.phase]);
  if (
    phase &&
    boot.freshness === 'fresh' &&
    !['ready', 'busy', 'draining', 'blocked', 'failed'].includes(node.runtime_state)
  )
    return { label: phase.label, tone: 'neutral' };
  if (node.runtime_state === 'ready') {
    const ready = (node.slots || []).filter((slot) => slot.state === 'ready' && !slot.stale);
    if (!ready.length) return { label: '等待执行槽就绪', tone: 'neutral' };
    if (
      !ready.some((slot) => slot.admission_allowed !== false) ||
      epochMs(node.hard_deadline) <= now + 300000
    )
      return { label: '暂不接单', tone: 'warn' };
  }
  return {
    label: stateLabel(node?.runtime_state || node?.state),
    tone: stateTone(node?.runtime_state),
  };
}
// Stale, missing, future or more than a minute old: the state cannot be shown as current.
function unconfirmedObservation(node, now) {
  const observed = epochMs(node?.observed_at);
  return (
    node?.stale ||
    observed === null ||
    observed > now ||
    now - observed > 60000 ||
    ['unknown', 'observation_failed'].includes(node?.runtime_state)
  );
}
const stateTone = (state) =>
  ['ready', 'busy'].includes(state)
    ? 'good'
    : ['blocked', 'failed'].includes(state)
      ? 'warn'
      : 'neutral';
export function operatorCapacitySummary(snapshot, now = Date.now()) {
  const groups = operatorNodeGroups(snapshot?.nodes),
    current = [...groups.current, ...groups.pending_review];
  const unknown = (node) => operatorNodeStatus(node, now).label === '状态待核对';
  const heartbeat = epochMs(snapshot?.controller?.last_heartbeat_at);
  const controllerCurrent =
    snapshot?.controller?.stale !== true &&
    ['running', 'degraded'].includes(snapshot?.controller?.state) &&
    heartbeat !== null &&
    heartbeat <= now &&
    now - heartbeat <= 30000;
  const deadlineCurrent = (value) => {
    const time = epochMs(value);
    return time !== null && time > now + 300000;
  };
  const readinessCurrent = (node) =>
    controllerCurrent &&
    !staleSnapshot(snapshot, now) &&
    operatorNodeGroup(node) === 'current' &&
    ['starting', 'ready', 'busy'].includes(node.state) &&
    node.desired_state === 'running' &&
    ['ready', 'busy'].includes(node.runtime_state) &&
    !unknown(node) &&
    !nodeRemovalConfirmation(node) &&
    !node.reason_code &&
    nodeDeadline(node, now).verified &&
    deadlineCurrent(node.hard_deadline) &&
    node.provider_safe_deadline >= node.hard_deadline;
  return {
    stale: staleSnapshot(snapshot, now),
    ready_slots: current.reduce(
      (n, node) =>
        n +
        (readinessCurrent(node)
          ? (node.slots || []).filter(
              (slot) => slot.state === 'ready' && !slot.stale && slot.admission_allowed !== false,
            ).length
          : 0),
      0,
    ),
    starting_nodes: current.filter(
      (node) =>
        !unknown(node) &&
        ['reserved', 'creating', 'starting'].includes(node.state) &&
        !['blocked', 'failed', 'ready', 'busy', 'draining'].includes(node.runtime_state),
    ).length,
    unknown_nodes: current.filter(unknown).length,
    pending_review: groups.pending_review.length,
  };
}
export function filterOperatorHistory(nodes, { search = '', provider = 'all' } = {}) {
  const text = search.trim().toLocaleLowerCase();
  return nodes.filter(
    (node) =>
      (provider === 'all' || node.provider === provider) &&
      (!text ||
        [
          node.id,
          node.provider,
          node.provider_instance_id,
          node.gpu_model,
          node.runtime_profile_id,
          node.mode,
          node.state,
        ].some((value) =>
          String(value ?? '')
            .toLocaleLowerCase()
            .includes(text),
        )),
  );
}
export function operatorExtensionCurrent(node, preview, additionalSeconds, now = Date.now()) {
  const observed = epochMs(node?.observed_at);
  return (
    !!node?.actions?.extend?.allowed &&
    node?.stale !== true &&
    !historicalNode(node) &&
    preview?.can_extend === true &&
    observed !== null &&
    observed <= now &&
    now - observed <= 60000 &&
    preview.node_id === node.id &&
    preview.node_version === node.version &&
    preview.current_deadline === node.hard_deadline &&
    Number.isInteger(additionalSeconds) &&
    additionalSeconds > 0 &&
    preview.additional_seconds === additionalSeconds &&
    preview.new_deadline === preview.current_deadline + additionalSeconds &&
    Number.isFinite(preview.expires_at) &&
    preview.expires_at * 1000 > now
  );
}
export function operatorExtensionReadback(node, preview, result) {
  const operation = result?.operation,
    extension = operation?.extension;
  return (
    operation?.state === 'completed' &&
    extension?.current_deadline === preview?.current_deadline &&
    extension?.new_deadline === preview?.new_deadline &&
    extension?.additional_seconds === preview?.additional_seconds &&
    node?.id === preview?.node_id &&
    node?.hard_deadline === extension?.new_deadline
  );
}
