// CR43 automatic GPU start: exact admin `demand_start` contract.
const REASONS = [
  'disabled',
  'no_eligible_work',
  'capacity_available',
  'machine_limit',
  'allocation_uncertain',
  'policy_unavailable',
  'no_qualified_profile',
  'retry_backoff',
  'download_retry_limit',
];
const count = (value) => (Number.isInteger(value) && value >= 0 ? value : null);

export function validDemandStart(value) {
  return (
    !!value &&
    typeof value.enabled === 'boolean' &&
    (value.revision === null || /^[0-9a-f]{64}$/.test(value.revision)) &&
    count(value.hard_max_machines) !== null &&
    ['running_machines', 'starting_machines', 'uncertain_machines', 'queued_generation_jobs'].every(
      (key) => count(value[key]) !== null,
    ) &&
    count(value.queued_by_mode?.fl) !== null &&
    count(value.queued_by_mode?.ref) !== null &&
    (value.blocked_reason === null || REASONS.includes(value.blocked_reason))
  );
}
