import { translate } from './i18n/locale.js';
export const cancellationReasons = [
  { code: 'operator_queue_cleared', label: '管理员清理队列' },
  { code: 'operator_request_cancelled', label: '管理员取消请求' },
  { code: 'capacity_unavailable', label: '暂时没有可用算力' },
];
const snapshot = (value) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const identity = (value) => typeof value === 'string' && value.length > 0 && value.length <= 200;
export const canCancelJob = (job) =>
  identity(job?.id) &&
  identity(job.owner_id) &&
  ['queued', 'waiting_capacity'].includes(job.status) &&
  job.cancellable === true &&
  snapshot(job.snapshot);
export function validAdminJobs(value, account) {
  return (
    value?.operator?.account === account &&
    value.operator.admin === true &&
    Array.isArray(value.jobs) &&
    value.jobs.length <= 100 &&
    new Set(value.jobs.map((job) => job?.id)).size === value.jobs.length &&
    value.jobs.every(
      (job) =>
        identity(job?.id) &&
        identity(job.owner_id) &&
        typeof job.status === 'string' &&
        typeof job.cancellable === 'boolean' &&
        snapshot(job.snapshot),
    ) &&
    (value.next_after_id === null ||
      value.next_after_id === undefined ||
      identity(value.next_after_id))
  );
}
export function cancellationBody(jobs, reason) {
  if (
    !Array.isArray(jobs) ||
    !jobs.length ||
    jobs.length > 100 ||
    new Set(jobs.map((job) => job?.id)).size !== jobs.length ||
    !jobs.every(canCancelJob) ||
    !cancellationReasons.some((item) => item.code === reason)
  )
    return null;
  return {
    jobs: jobs.map((job) => ({ id: job.id, expected_snapshot: job.snapshot })),
    reason_code: reason,
  };
}
export function validCancellationBody(body) {
  return (
    body &&
    Object.keys(body).sort().join(',') === 'jobs,reason_code' &&
    cancellationReasons.some((item) => item.code === body.reason_code) &&
    Array.isArray(body.jobs) &&
    body.jobs.length > 0 &&
    body.jobs.length <= 100 &&
    new Set(body.jobs.map((job) => job?.id)).size === body.jobs.length &&
    body.jobs.every(
      (job) =>
        job &&
        Object.keys(job).sort().join(',') === 'expected_snapshot,id' &&
        identity(job.id) &&
        snapshot(job.expected_snapshot),
    )
  );
}
export function cancellationReceipt(value, body) {
  return (
    identity(value?.command_id) &&
    value.state === 'completed' &&
    value.reason_code === body.reason_code &&
    Array.isArray(value.cancelled_job_ids) &&
    value.cancelled_job_ids.length === body.jobs.length &&
    new Set(value.cancelled_job_ids).size === body.jobs.length &&
    body.jobs.every((job) => value.cancelled_job_ids.includes(job.id))
  );
}
export function selectionCurrent(selected, jobs) {
  return (
    selected.length > 0 &&
    selected.length <= 100 &&
    selected.every((original) =>
      jobs.some(
        (job) =>
          job.id === original.id &&
          job.owner_id === original.owner_id &&
          job.snapshot === original.snapshot &&
          canCancelJob(job),
      ),
    )
  );
}
export const jobStatus = (status, locale = 'zh') =>
  translate(
    locale,
    'operator.display.job.' +
      ([
        'queued',
        'waiting_capacity',
        'claimed',
        'submitting',
        'running',
        'submission_unknown',
        'collecting',
        'reconciling',
        'recovery_hold',
        'cancel_requested',
        'cancelled',
        'succeeded',
        'failed',
      ].includes(status)
        ? status
        : 'unknown'),
  );
const diagnosticReasons = {
  job_cancel_pending: '取消已请求，等待原执行确认',
  original_attempt_requires_reconciliation: '保留原执行，等待核对',
  upstream_status_unknown: '上游结果未知，等待原执行核对',
  collection_pending: '正在保存原执行结果',
  matching_capacity_unavailable: '等待兼容且可接任务的机器',
  awaiting_worker_claim: '等待机器领取',
};
const dispatchStates = {
  pending: '待通知',
  publishing: '通知中',
  published: '已通知',
  unknown: '通知结果待核对',
  finished: '原通知已结束',
  obsolete: '原通知已停用',
  blocked: '通知需核对',
};
const publicId = (value) => typeof value === 'string' && /^[a-zA-Z0-9_-]{1,200}$/.test(value);
export function jobDiagnostics(job, locale = 'zh') {
  const value = job?.diagnostic;
  if (!value || typeof value !== 'object') return [];
  const lines = [];
  if (diagnosticReasons[value.reason_code])
    lines.push(translate(locale, 'operatorPanel.reason.' + value.reason_code));
  if (Number.isFinite(value.age_seconds) && value.age_seconds >= 0)
    lines.push(
      translate(
        locale,
        value.age_seconds < 60 ? 'operatorPanel.ageSeconds' : 'operatorPanel.ageMinutes',
        { value: Math.floor(value.age_seconds < 60 ? value.age_seconds : value.age_seconds / 60) },
      ),
    );
  if (
    publicId(value.current_attempt_id) &&
    Number.isSafeInteger(value.attempt_no) &&
    value.attempt_no > 0
  )
    lines.push(
      translate(locale, 'operatorPanel.attempt', {
        number: value.attempt_no,
        id: value.current_attempt_id.slice(0, 8),
      }),
    );
  const receipt = value.dispatch;
  if (receipt && publicId(receipt.event_id) && dispatchStates[receipt.state])
    lines.push(
      translate(locale, 'operatorPanel.dispatch', {
        id: receipt.event_id.slice(0, 8),
        state: translate(locale, 'operatorPanel.dispatchState.' + receipt.state),
      }),
    );
  return lines;
}
