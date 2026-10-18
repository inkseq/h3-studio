export const isActiveTrashItem = (row) =>
  row?.trash_state === undefined || row?.trash_state === null || row.trash_state === 'active';
import { digest, stableJSON } from './cloud-model.js';
export const trashClosedCodes = new Set([
  'trash_project_version_conflict',
  'trash_version_conflict',
  'trash_restore_reference_conflict',
  'trash_last_container_required',
  'jobs_active_or_uncertain',
  'upstream_stop_unconfirmed',
  'worker_obligation_unresolved',
  'admission_unresolved',
  'authoring_operation_unresolved',
  'upload_unresolved',
  'collection_unresolved',
  'job_binding_unknown',
  'history_cleanup_not_enabled',
  'trash_reference_scope_required',
  'trash_target_not_active',
  'trash_target_not_trashed',
  'trash_upload_unresolved',
  'trash_permanent_confirmation_required',
  'project_in_trash',
  'trash_overlapping_targets',
  'trash_dependent_items_retained',
  'trash_cleanup_scope_required',
]);
export function trashErrorKey(error) {
  const proof = error?.detail?.detail || error?.detail,
    code = proof?.code;
  if (code === 'trash_last_container_required') return 'trash.minimum';
  if (
    [
      'jobs_active_or_uncertain',
      'upstream_stop_unconfirmed',
      'worker_obligation_unresolved',
      'admission_unresolved',
      'authoring_operation_unresolved',
      'upload_unresolved',
      'collection_unresolved',
      'job_binding_unknown',
      'trash_upload_unresolved',
    ].includes(code)
  )
    return 'trash.running';
  if (
    [
      'trash_project_version_conflict',
      'trash_version_conflict',
      'trash_restore_reference_conflict',
    ].includes(code)
  )
    return 'trash.changed';
  if (trashClosedCodes.has(code)) return 'trash.unavailable';
  return error?.message?.startsWith('trash.') ? error.message : 'trash.failed';
}
const clone = (v) => structuredClone(v),
  kinds = new Set(['project', 'entity', 'look', 'location_version', 'asset', 'artifact', 'take']);
export const trashTargetKey = (t) => stableJSON(t);
export function checkedTarget(t) {
  if (
    !t ||
    !kinds.has(t.kind) ||
    typeof t.id !== 'string' ||
    !t.id ||
    typeof t.project_id !== 'string' ||
    !t.project_id ||
    Object.keys(t).some((k) => !['kind', 'id', 'project_id', 'parent_id'].includes(k)) ||
    (t.parent_id !== undefined && typeof t.parent_id !== 'string')
  )
    throw Error('trash.failed');
  return clone(t);
}
export async function checkedTrashPreview(value, request) {
  if (
    value?.eligible !== true ||
    value.action !== request.action ||
    value.request_hash !== (await digest(request)) ||
    value.auto_purge !== false ||
    value.permanent_confirmation_required !== (request.action === 'purge') ||
    !Array.isArray(value.items) ||
    value.items.length !== request.items.length
  )
    throw Error('trash.failed');
  const wanted = new Set(request.items.map(trashTargetKey)),
    seen = new Set();
  for (const row of value.items) {
    const key = trashTargetKey(checkedTarget(row.target));
    if (
      !wanted.has(key) ||
      seen.has(key) ||
      !Number.isSafeInteger(row.expected_version) ||
      row.expected_version < 1
    )
      throw Error('trash.failed');
    seen.add(key);
  }
  const versions = value.affected_project_versions;
  if (
    !versions ||
    Object.values(versions).some((v) => !Number.isSafeInteger(v) || v < 1) ||
    request.items.some((t) => !Object.hasOwn(versions, t.project_id))
  )
    throw Error('trash.failed');
  return clone(value);
}
export function actionFromPreview(preview) {
  return {
    action: preview.action,
    items: preview.items.map((row) => ({
      ...checkedTarget(row.target),
      expected_version: row.expected_version,
    })),
    expected_project_versions: clone(preview.affected_project_versions),
    ...(preview.action === 'purge' ? { confirmed: true } : {}),
  };
}
export async function checkedTrashReceipt(value, op) {
  if (
    !value ||
    typeof value.operation_id !== 'string' ||
    !value.operation_id ||
    (op.operationId && op.operationId !== value.operation_id) ||
    value.action !== op.body.action ||
    value.request_hash !== (await digest(op.body)) ||
    !['pending', 'completed'].includes(value.state) ||
    value.ledger_retained !== true ||
    value.auto_purge !== false ||
    !Array.isArray(value.items) ||
    value.items.length !== op.body.items.length
  )
    throw Error('trash.failed');
  const wanted = new Set(
      op.body.items.map(({ expected_version, ...target }) => trashTargetKey(target)),
    ),
    seen = new Set();
  for (const row of value.items) {
    const key = trashTargetKey(checkedTarget(row.target));
    if (
      !wanted.has(key) ||
      seen.has(key) ||
      row.auto_purge !== false ||
      !Number.isSafeInteger(row.version) ||
      row.version < 1
    )
      throw Error('trash.failed');
    seen.add(key);
    const expected =
      op.body.action === 'trash' ? 'trashed' : op.body.action === 'restore' ? 'active' : 'purged';
    if (value.state === 'completed' && row.state !== expected) throw Error('trash.failed');
  }
  return clone(value);
}
// Only the original command identity is durable. Backend remains lifecycle authority.
export function createTrashController({
  api,
  account,
  getAccount,
  storage,
  keyFactory = () => crypto.randomUUID(),
}) {
  const name = 'inkseq-trash-command-v1:' + account;
  let active = false;
  const guard = () => {
    if (!account || getAccount() !== account) throw Error('trash.changed');
  };
  const pending = () => {
    const raw = storage.getItem(name);
    if (!raw) return null;
    try {
      const op = JSON.parse(raw);
      if (op.account !== account || typeof op.key !== 'string' || !op.body) throw Error();
      return op;
    } catch {
      throw Error('trash.failed');
    }
  };
  const same = (op) => {
    if (stableJSON(pending()) !== stableJSON(op)) throw Error('trash.pending');
  };
  async function execute(op, observe) {
    guard();
    if (active) throw Error('trash.pending');
    same(op);
    active = true;
    try {
      let value;
      try {
        value = await api.request(
          observe && op.operationId
            ? '/v1/trash/actions/' + encodeURIComponent(op.operationId)
            : '/v1/trash/actions',
          observe && op.operationId ? {} : { method: 'POST', body: clone(op.body), key: op.key },
        );
        guard();
      } catch (error) {
        guard();
        const proof = error.detail?.detail || error.detail;
        if (
          proof?.resource === 'trash' &&
          trashClosedCodes.has(proof.code) &&
          proof.no_mutation_committed === true &&
          proof.request_hash === (await digest(op.body))
        ) {
          guard();
          same(op);
          storage.removeItem(name);
          throw Error(trashErrorKey(error));
        }
        throw error;
      }
      value = await checkedTrashReceipt(value, op);
      guard();
      same(op);
      if (value.state === 'completed') storage.removeItem(name);
      else storage.setItem(name, JSON.stringify({ ...op, operationId: value.operation_id }));
      return value;
    } finally {
      active = false;
    }
  }
  return {
    pending,
    async preview(action, items) {
      guard();
      if (pending()) throw Error('trash.pending');
      const body = { action, items: items.map(checkedTarget) };
      if (new Set(body.items.map(trashTargetKey)).size !== items.length)
        throw Error('trash.failed');
      const value = await api.request('/v1/trash/preview', { method: 'POST', body });
      guard();
      const checked = await checkedTrashPreview(value, body);
      guard();
      return checked;
    },
    async start(preview) {
      guard();
      if (pending()) throw Error('trash.pending');
      const op = { account, key: 'trash-' + keyFactory(), body: actionFromPreview(preview) };
      storage.setItem(name, JSON.stringify(op));
      return execute(op, false);
    },
    recover() {
      const op = pending();
      if (!op) throw Error('trash.pending');
      return execute(op, true);
    },
    resume() {
      const op = pending();
      if (!op) throw Error('trash.pending');
      return execute(op, false);
    },
    async list(kind, offset = 0) {
      guard();
      const value = await api.request(
        '/v1/trash?' + new URLSearchParams({ limit: 100, offset, ...(kind ? { kind } : {}) }),
      );
      guard();
      if (!Array.isArray(value?.items) || value.auto_purge !== false) throw Error('trash.failed');
      if (
        value.next_offset !== null &&
        (!Number.isSafeInteger(value.next_offset) || value.next_offset <= offset)
      )
        throw Error('trash.failed');
      const seen = new Set();
      value.items.forEach((row) => {
        checkedTarget(row.target);
        if (
          typeof row.trash_id !== 'string' ||
          seen.has(row.trash_id) ||
          !['trashed', 'purging'].includes(row.state) ||
          row.auto_purge !== false
        )
          throw Error('trash.failed');
        seen.add(row.trash_id);
      });
      return value;
    },
  };
}
