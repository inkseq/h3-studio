import { digest, stableJSON } from './cloud-model.js';
const clone = (value) => structuredClone(value);
export const libraryKey = (target) => stableJSON(target);
export function libraryTarget(value) {
  if (
    !value ||
    !['entity', 'asset', 'artifact'].includes(value.kind) ||
    typeof value.id !== 'string' ||
    !value.id ||
    typeof value.project_id !== 'string' ||
    !value.project_id ||
    Object.keys(value).some((k) => !['kind', 'id', 'project_id'].includes(k))
  )
    throw Error('library.invalid');
  return clone(value);
}
export function checkedLibraryPage(value, offset = 0) {
  if (
    value?.storage_scope !== 'project' ||
    value.usage_scope !== 'authorized_projects' ||
    value.aggregate_read_only !== true ||
    !Array.isArray(value.items) ||
    (value.next_offset !== null &&
      (!Number.isSafeInteger(value.next_offset) || value.next_offset <= offset))
  )
    throw Error('library.invalid');
  const seen = new Set();
  for (const row of value.items) {
    const key = libraryKey(libraryTarget(row.target));
    if (
      seen.has(key) ||
      !Number.isSafeInteger(row.project_version) ||
      row.project_version < 1 ||
      typeof row.title !== 'string' ||
      !Array.isArray(row.tags) ||
      row.tags.some((t) => typeof t !== 'string') ||
      !Array.isArray(row.usage)
    )
      throw Error('library.invalid');
    seen.add(key);
  }
  if (value.counts !== undefined) {
    const keys = ['character', 'location', 'prop', 'file', 'image', 'video', 'audio', 'inbox'];
    if (
      value.counts_scope !== 'authorized_filtered_except_type' ||
      !value.counts ||
      Array.isArray(value.counts) ||
      Object.keys(value.counts).length !== keys.length ||
      keys.some((key) => !Number.isSafeInteger(value.counts[key]) || value.counts[key] < 0)
    )
      throw Error('library.invalid');
  }
  return value;
}
export async function checkedLibraryImport(value, op) {
  if (
    value?.request_hash !== (await digest(op.body)) ||
    stableJSON(value.source) !== stableJSON(op.body.source) ||
    value.target_project_id !== op.body.target_project_id ||
    value.expected_project_version !== op.body.expected_project_version ||
    typeof value.operation_id !== 'string' ||
    !value.operation_id ||
    (op.operationId && value.operation_id !== op.operationId) ||
    !['completed', 'pending', 'blocked'].includes(value.state) ||
    !Array.isArray(value.asset_ids)
  )
    throw Error('library.invalid');
  if (
    !Number.isSafeInteger(value.source_project_version) ||
    value.source_project_version < 1 ||
    (op.body.source.expected_version !== undefined &&
      value.source_project_version !== op.body.source.expected_version) ||
    typeof value.source_snapshot_sha256 !== 'string' ||
    !/^([a-f0-9]{64})$/.test(value.source_snapshot_sha256)
  )
    throw Error('library.invalid');
  if (
    op.sourceSnapshot &&
    (value.source_project_version !== op.sourceSnapshot.version ||
      value.source_snapshot_sha256 !== op.sourceSnapshot.sha256)
  )
    throw Error('library.invalid');
  if (
    value.state === 'completed' &&
    (!Number.isSafeInteger(value.project_version) ||
      value.project_version <= op.body.expected_project_version ||
      value.no_authoring_committed !== false ||
      !value.entity_ids ||
      Array.isArray(value.entity_ids) ||
      typeof value.entity_ids !== 'object' ||
      Object.values(value.entity_ids).some((id) => typeof id !== 'string' || !id))
  )
    throw Error('library.invalid');
  if (
    value.state === 'blocked' &&
    (value.no_authoring_committed !== true ||
      !['library_target_version_conflict', 'library_authoring_validation_failed'].includes(
        value.reason,
      ))
  )
    throw Error('library.invalid');
  return value;
}
export function createLibraryController({
  api,
  account,
  getAccount,
  storage,
  keyFactory = () => crypto.randomUUID(),
}) {
  const name = 'inkseq-library-command-v1:' + account;
  let active = false;
  const guard = () => {
    if (!account || getAccount() !== account) throw Error('library.changed');
  };
  const pending = () => {
    const raw = storage.getItem(name);
    if (!raw) return null;
    try {
      const op = JSON.parse(raw);
      if (
        op.account !== account ||
        !['import', 'actions'].includes(op.kind) ||
        typeof op.key !== 'string' ||
        !op.body
      )
        throw Error();
      return op;
    } catch {
      throw Error('library.invalid');
    }
  };
  const same = (op) => {
    if (stableJSON(pending()) !== stableJSON(op)) throw Error('library.pending');
  };
  async function execute(op, observe = false) {
    guard();
    same(op);
    if (active) throw Error('library.pending');
    active = true;
    try {
      let value;
      try {
        value = await api.request(
          op.kind === 'import'
            ? observe && op.operationId
              ? '/v1/library/imports/' + encodeURIComponent(op.operationId)
              : '/v1/library/imports'
            : '/v1/projects/' + encodeURIComponent(op.projectId) + '/actions',
          op.kind === 'import' && observe && op.operationId
            ? {}
            : { method: 'POST', body: clone(op.body), key: op.key },
        );
        guard();
      } catch (error) {
        guard();
        const proof = error.detail?.detail || error.detail;
        // Quick-create workspaces are managed by the service; it refuses library edits there
        // before changing anything, so the request is settled, not pending.
        if (
          op.kind === 'actions' &&
          error.status === 409 &&
          proof === 'quick_chat_managed_resource'
        ) {
          same(op);
          storage.removeItem(name);
          throw Error('library.managed', { cause: error });
        }
        if (
          ((op.kind === 'actions' &&
            proof?.code === 'document_version_conflict' &&
            proof.project_id === op.projectId) ||
            (op.kind === 'import' &&
              !op.operationId &&
              proof?.resource === 'library_import' &&
              ['library_source_version_conflict', 'library_target_version_conflict'].includes(
                proof.code,
              ))) &&
          proof.no_mutation_committed === true &&
          proof.request_hash === (await digest(op.body))
        ) {
          guard();
          same(op);
          storage.removeItem(name);
          throw Error('library.changed', { cause: error });
        }
        throw error;
      }
      if (op.kind === 'import') {
        value = await checkedLibraryImport(value, op);
        guard();
        same(op);
        if (value.state === 'pending')
          storage.setItem(
            name,
            JSON.stringify({
              ...op,
              operationId: value.operation_id,
              sourceSnapshot: {
                version: value.source_project_version,
                sha256: value.source_snapshot_sha256,
              },
            }),
          );
        else storage.removeItem(name);
      } else {
        if (
          value?.id !== op.projectId ||
          value.project?.id !== op.projectId ||
          !Number.isSafeInteger(value.version) ||
          value.version <= op.body.expected_version
        )
          throw Error('library.invalid');
        same(op);
        storage.removeItem(name);
      }
      return value;
    } finally {
      active = false;
    }
  }
  function begin(kind, body, projectId) {
    guard();
    if (pending()) throw Error('library.pending');
    const op = {
      account,
      kind,
      body: clone(body),
      key: 'library-' + keyFactory(),
      ...(projectId ? { projectId } : {}),
    };
    storage.setItem(name, JSON.stringify(op));
    return execute(op);
  }
  return {
    pending,
    startImport: (body) => begin('import', body),
    actions: (projectId, body) => begin('actions', body, projectId),
    recover() {
      const op = pending();
      if (!op) throw Error('library.pending');
      return execute(op, true);
    },
    resume() {
      const op = pending();
      if (!op) throw Error('library.pending');
      return execute(op);
    },
    async list(filters = {}, offset = 0) {
      guard();
      const query = new URLSearchParams({ limit: 100, offset });
      for (const [k, v] of Object.entries(filters))
        if (
          ['project_id', 'type', 'work', 'unused', 'tag', 'q'].includes(k) &&
          v !== '' &&
          v !== undefined
        )
          query.set(k, String(v));
      const result = await api.request('/v1/library?' + query);
      guard();
      return checkedLibraryPage(result, offset);
    },
    async usage(target) {
      guard();
      const result = await api.request(
        '/v1/library/usage?' + new URLSearchParams(libraryTarget(target)),
      );
      guard();
      if (
        libraryKey(result.target) !== libraryKey(target) ||
        result.retention_is_separate !== true ||
        !Array.isArray(result.usage)
      )
        throw Error('library.invalid');
      return result;
    },
  };
}
