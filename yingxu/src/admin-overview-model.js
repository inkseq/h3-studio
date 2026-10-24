// Admin overview (h3-studio#537): what is running for everyone, who is waiting, what it
// costs today and what needs attention, from the existing admin workspace, admin jobs and
// spending-settings reads. Plain words first; raw codes only as details.

import { phase, terminal } from './p4-operator-model.js';

const MINUTE = 60;

// Raw dispatch/registration/diagnostic codes → the plain reason a person can act on.
const REASONS = {
  global_capacity_exceeded: 'capacityFull',
  fleet_hourly_limit_exceeded: 'capacityFull',
  dstack_runtime_ssh_unavailable_or_host_key_untrusted: 'unreachable',
  upstream_status_unknown: 'unreachable',
  dstack_runtime_ready_model_load_unobserved: 'waitingFirstJob',
  dstack_runtime_not_observed: 'starting',
  dstack_idle_expired: 'idleStopped',
  dstack_run_terminal_billing_pending: 'stoppedBilling',
};
export const reasonKey = (code) => (code ? REASONS[code] || 'other' : null);

const secondsSince = (value, now) =>
  typeof value === 'number' && Number.isFinite(value) ? Math.max(0, now - value) : null;

/** One row per live machine, newest problems first. */
export function machineRows(workspace, now, locale = 'zh') {
  const queued = (workspace?.queue || []).reduce((sum, mode) => sum + (mode.waiting || 0), 0);
  return (workspace?.nodes || [])
    .filter((node) => !terminal(node))
    .map((node) => {
      const step = phase(node, locale);
      const refused = node.dispatch_reason_code || node.registration_reason_code || null;
      const started = node.preparation?.started_at ?? null;
      const age = secondsSince(started, now) ?? node.usage?.runtime_seconds ?? null;
      let issue = null;
      if (refused) issue = { kind: 'refused', reason: reasonKey(refused), code: refused };
      else if (node.preparation?.stalled)
        issue = {
          kind: 'stalled',
          reason: reasonKey(node.preparation.reason_code) || 'other',
          code: node.preparation.reason_code,
        };
      else if (step.id !== 'running' && step.id !== 'ready' && age !== null && age > 20 * MINUTE)
        issue = { kind: 'slowStart', reason: reasonKey(node.reason_code), code: node.reason_code };
      else if (step.id === 'ready' && queued > 0)
        issue = { kind: 'idleWithQueue', reason: null, code: null };
      return {
        id: node.node_id || node.id,
        owner: node.owner_id || '',
        gpu: `${node.gpu_type || 'GPU'}${node.gpu_count > 1 ? ' × ' + node.gpu_count : ''}`,
        mode: node.mode,
        provider: node.provider,
        region: node.host?.region || '',
        status: step,
        job: node.current_job?.id || node.current_job?.job_id || null,
        hourly: node.hourly_cost_microusd ?? null,
        today: node.usage?.today_cost_microusd ?? null,
        ageSeconds: age,
        issue,
        code: node.reason_code || null,
      };
    })
    .sort((a, b) => !!b.issue - !!a.issue || (b.ageSeconds || 0) - (a.ageSeconds || 0));
}

/** Waiting and stuck jobs across everyone (admin jobs list). */
export function jobRows(jobs, now) {
  return (jobs || []).map((job) => {
    const age = job.diagnostic?.age_seconds ?? secondsSince(job.created_at, now);
    const stuck =
      ['running', 'cancel_requested'].includes(job.status) &&
      job.diagnostic?.error_code === 'upstream_status_unknown';
    return {
      id: job.id,
      owner: job.owner_id,
      project: job.project_id,
      status: job.status,
      ageSeconds: age,
      stuck,
      waitingLong: job.status === 'queued' && age !== null && age > 30 * MINUTE,
      cancellable: job.cancellable === true,
      reason: reasonKey(job.diagnostic?.error_code || job.diagnostic?.reason_code),
      code: job.diagnostic?.error_code || job.diagnostic?.reason_code || null,
      raw: job,
    };
  });
}

/** Today's supplier spend against the daily cap (spending-settings). */
export function spendSummary(settings) {
  const day = settings?.tenant_daily_supplier_spending;
  if (!day) return null;
  const used = (day.settled_microusd || 0) + (day.committed_microusd || 0);
  const limit = day.limit_microusd || 0;
  return {
    used,
    pending: day.pending_microusd || 0,
    limit,
    fraction: limit ? used / limit : 0,
    // "Near the cap" only when spend is actually near it; unconfirmed supplier bills are a
    // separate note, not a near-cap warning (the old card said near-cap at $0 / $100).
    nearCap: limit > 0 && used >= limit * (day.warning_fraction ?? 0.8),
    blocked: day.admission_blocked === true,
    unconfirmed: day.unresolved_count || 0,
    resetsAt: day.resets_at || null,
  };
}

/** Things that need a person, most urgent first. */
export function alerts({ machines, jobs, spend, workspace }) {
  const list = [];
  for (const row of machines)
    if (row.issue)
      list.push({
        tone: row.issue.kind === 'idleWithQueue' || row.issue.kind === 'refused' ? 'bad' : 'warn',
        kind: row.issue.kind,
        reason: row.issue.reason,
        machine: row,
      });
  const stuck = jobs.filter((job) => job.stuck);
  if (stuck.length) list.push({ tone: 'bad', kind: 'stuckJobs', count: stuck.length, jobs: stuck });
  const waiting = jobs.filter((job) => job.waitingLong);
  if (waiting.length)
    list.push({ tone: 'warn', kind: 'longWait', count: waiting.length, jobs: waiting });
  const uncertain = workspace?.demand_start?.uncertain_machines || 0;
  if (uncertain) list.push({ tone: 'warn', kind: 'uncertainMachines', count: uncertain });
  if (spend?.blocked) list.push({ tone: 'bad', kind: 'spendBlocked' });
  else if (spend?.nearCap) list.push({ tone: 'warn', kind: 'spendNear' });
  return list.sort((a, b) => (a.tone === 'bad' ? 0 : 1) - (b.tone === 'bad' ? 0 : 1));
}

export function duration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

export const usd = (micro) =>
  micro === null || micro === undefined ? '—' : '$' + (micro / 1e6).toFixed(2);
