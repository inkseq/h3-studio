const amount = (value) => Number.isSafeInteger(value) && value >= 0;
const text = (value) => typeof value === 'string' && value.length > 0 && value.length <= 200;
const hash = (value) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const kind = (value) => ['job', 'instance'].includes(value);
const note = (value, max) =>
  typeof value === 'string' &&
  value === value.trim() &&
  value.length > 0 &&
  value.length <= max &&
  !/[\x00-\x1f\x7f]/.test(value);
export const billingReferenceKey = (row) => row?.reference_type + ':' + row?.reference_id;
const same = (left, right) => JSON.stringify(canonical(left)) === JSON.stringify(canonical(right));
function canonical(value) {
  return Array.isArray(value)
    ? value.map(canonical)
    : value && typeof value === 'object'
      ? Object.fromEntries(
          Object.keys(value)
            .sort()
            .map((key) => [key, canonical(value[key])]),
        )
      : value;
}
export function billingTimeBounds(start = '', end = '') {
  const parse = (value) => {
    if (value === '') return undefined;
    const match =
      /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,3})?)?(Z|[+-]\d{2}:\d{2})$/.exec(
        value,
      );
    if (!match) return NaN;
    const [, year, month, day, hour, minute, second = '0'] = match.map((value, index) =>
      index > 0 && index < 7 && value !== undefined ? Number(value) : value,
    );
    if (
      month < 1 ||
      month > 12 ||
      day < 1 ||
      day > new Date(Date.UTC(year, month, 0)).getUTCDate() ||
      hour > 23 ||
      minute > 59 ||
      second > 59
    )
      return NaN;
    return Date.parse(value) / 1000;
  };
  const after = parse(start),
    before = parse(end);
  if (
    [after, before].some(
      (value) => value !== undefined && (!Number.isFinite(value) || value < 0),
    ) ||
    (after !== undefined && before !== undefined && after >= before)
  )
    return null;
  return {
    ...(after !== undefined ? { created_after: after } : {}),
    ...(before !== undefined ? { created_before: before } : {}),
  };
}
export function validBillingReference(row, owner) {
  return (
    row?.owner_id === owner &&
    kind(row.reference_type) &&
    text(row.reference_id) &&
    hash(row.snapshot) &&
    typeof row.state === 'string' &&
    typeof row.waived === 'boolean' &&
    ['provider_actual_cost_microusd', 'allocated_cost_microusd', 'user_charge_microusd'].every(
      (key) => row[key] === null || amount(row[key]),
    ) &&
    amount(row.reserved_estimate_microusd) &&
    Array.isArray(row.eligible_actions) &&
    row.eligible_actions.every((action) => ['settle', 'waive'].includes(action)) &&
    (row.block_reason === null || typeof row.block_reason === 'string')
  );
}
export function validBillingPage(value, owner) {
  return (
    text(owner) &&
    value?.owner_id === owner &&
    Array.isArray(value.items) &&
    value.items.length <= 100 &&
    value.items.every((row) => validBillingReference(row, owner)) &&
    new Set(value.items.map(billingReferenceKey)).size === value.items.length &&
    (value.next_cursor === null ||
      (typeof value.next_cursor === 'string' &&
        value.next_cursor.length > 0 &&
        value.next_cursor.length <= 4096))
  );
}
export function validBillingRequest(body) {
  if (
    !body ||
    !text(body.owner_id) ||
    !['settle', 'waive'].includes(body.action) ||
    Object.keys(body).sort().join(',') !==
      'action,evidence_note,evidence_sha256,items,owner_id,reason' ||
    !note(body.reason, 1000) ||
    !note(body.evidence_note, 2000) ||
    !hash(body.evidence_sha256) ||
    !Array.isArray(body.items) ||
    !body.items.length ||
    body.items.length > 200
  )
    return false;
  if (new Set(body.items.map(billingReferenceKey)).size !== body.items.length) return false;
  return body.items.every(
    (item) =>
      kind(item?.reference_type) &&
      text(item.reference_id) &&
      hash(item.expected_snapshot) &&
      Object.keys(item).sort().join(',') ===
        (body.action === 'waive'
          ? 'expected_snapshot,reference_id,reference_type'
          : item.reference_type === 'job'
            ? 'actual_cost_microusd,allocation_instance_id,expected_snapshot,reference_id,reference_type'
            : 'actual_cost_microusd,expected_snapshot,reference_id,reference_type') &&
      (body.action === 'waive'
        ? item.actual_cost_microusd === undefined && item.allocation_instance_id === undefined
        : amount(item.actual_cost_microusd) &&
          (item.reference_type === 'instance'
            ? item.allocation_instance_id === undefined
            : text(item.allocation_instance_id))),
  );
}
// Import only amounts/lineage for references already selected by the operator.
// It cannot add a reference, change its owner/snapshot, or calculate allocations.
export function billingAllocationInputs(input, rows) {
  const value = JSON.parse(input),
    items = Array.isArray(value) ? value : value?.items;
  if (
    !Array.isArray(rows) ||
    !rows.length ||
    rows.length > 200 ||
    !Array.isArray(items) ||
    items.length !== rows.length ||
    new Set(items.map(billingReferenceKey)).size !== items.length ||
    (!Array.isArray(value) && value.owner_id !== undefined && value.owner_id !== rows[0].owner_id)
  )
    return null;
  const mapped = {};
  for (const row of rows) {
    const item = items.find((value) => billingReferenceKey(value) === billingReferenceKey(row));
    if (
      !item ||
      !amount(item.actual_cost_microusd) ||
      (item.expected_snapshot !== undefined && item.expected_snapshot !== row.snapshot) ||
      (row.reference_type === 'job' && !text(item.allocation_instance_id)) ||
      (row.reference_type === 'instance' && item.allocation_instance_id !== undefined)
    )
      return null;
    mapped[billingReferenceKey(row)] = {
      amount: item.actual_cost_microusd,
      allocation: item.allocation_instance_id,
    };
  }
  return mapped;
}
const totals = (value, count) =>
  value &&
  value.reference_count === count &&
  [
    'instance_actual_microusd',
    'job_allocated_microusd',
    'waived_reference_count',
    'waived_exposure_microusd',
  ].every((key) => amount(value[key]));
function sameReferences(items, request) {
  return (
    Array.isArray(items) &&
    items.length === request.items.length &&
    items.every((row) => row?.owner_id === request.owner_id) &&
    new Set(items.map(billingReferenceKey)).size === items.length &&
    request.items.every((item) =>
      items.some(
        (row) =>
          row.reference_type === item.reference_type && row.reference_id === item.reference_id,
      ),
    )
  );
}
export function validBillingPreview(value, request, now = Date.now()) {
  return (
    validBillingRequest(request) &&
    text(value?.preview_id) &&
    hash(value.snapshot) &&
    hash(value.request_hash) &&
    typeof value.expires_at === 'number' &&
    Number.isFinite(value.expires_at) &&
    value.expires_at * 1000 > now &&
    same(value.request, request) &&
    sameReferences(value.items, request) &&
    request.items.every((item) =>
      value.items.some(
        (row) =>
          billingReferenceKey(row) === billingReferenceKey(item) &&
          row.snapshot === item.expected_snapshot,
      ),
    ) &&
    totals(value.totals, request.items.length)
  );
}
export function billingOperation(preview) {
  if (!validBillingPreview(preview, preview?.request)) return null;
  return {
    body: { preview_id: preview.preview_id, expected_snapshot: preview.snapshot, confirmed: true },
    billing: {
      owner_id: preview.request.owner_id,
      action: preview.request.action,
      request_hash: preview.request_hash,
      items: preview.request.items.map(({ reference_type, reference_id }) => ({
        reference_type,
        reference_id,
      })),
    },
  };
}
export function validBillingOperation(operation) {
  const { body, billing } = operation || {};
  return (
    body &&
    Object.keys(body).sort().join(',') === 'confirmed,expected_snapshot,preview_id' &&
    body.confirmed === true &&
    text(body.preview_id) &&
    hash(body.expected_snapshot) &&
    text(billing?.owner_id) &&
    ['settle', 'waive'].includes(billing.action) &&
    hash(billing.request_hash) &&
    Array.isArray(billing.items) &&
    billing.items.length > 0 &&
    billing.items.length <= 200 &&
    billing.items.every((row) => kind(row?.reference_type) && text(row.reference_id)) &&
    new Set(billing.items.map(billingReferenceKey)).size === billing.items.length
  );
}
export function billingReceipt(value, operation) {
  const { body, billing } = operation || {};
  return (
    validBillingOperation(operation) &&
    value?.state === 'completed' &&
    text(value.action_id) &&
    text(value.audit_event_id) &&
    value.preview_id === body.preview_id &&
    value.owner_id === billing.owner_id &&
    value.action === billing.action &&
    value.request_hash === billing.request_hash &&
    sameReferences(value.items, billing) &&
    totals(value.totals, billing.items.length)
  );
}
