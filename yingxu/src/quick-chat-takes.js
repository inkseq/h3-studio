export function mergeTakes(previous, incoming) {
  return [...new Map([...previous, ...incoming].map((item) => [item.id, item])).values()].sort(
    (a, b) =>
      b.created_at - a.created_at ||
      String(b.revision_id || '').localeCompare(String(a.revision_id || '')) ||
      b.index - a.index ||
      b.id.localeCompare(a.id),
  );
}
export function checkedPreferences(value) {
  if (
    !Number.isSafeInteger(value?.version) ||
    value.version < 0 ||
    !['copies_max', 'takes_fold_count'].every(
      (key) => Number.isInteger(value[key]) && value[key] >= 1 && value[key] <= 2147483647,
    )
  )
    throw Error('takes.invalidPreferences');
  return value;
}
export function preferenceBody(previous, copies, fold) {
  const value = checkedPreferences({
    ...previous,
    copies_max: Number(copies),
    takes_fold_count: Number(fold),
  });
  return {
    expected_version: previous.version,
    copies_max: value.copies_max,
    takes_fold_count: value.takes_fold_count,
  };
}
export function takeMedia(take) {
  const list = take.artifacts || take.job?.artifacts || [];
  return (
    list.find((item) => item.kind === 'video') || list.find((item) => item.kind === 'audio') || null
  );
}
export function quoteMachineMinutes(preflight) {
  const seconds = preflight?.estimate?.machine_seconds;
  return typeof seconds === 'number' && Number.isFinite(seconds) && seconds >= 0
    ? seconds / 60
    : null;
}
export function storyScenes(project) {
  const result = [];
  function walk(node) {
    if (!node || typeof node !== 'object') return;
    if (node.type === 'scene' && typeof node.id === 'string') result.push(node);
    for (const key of ['children', 'chapters', 'scenes', 'entities'])
      for (const child of node[key] || []) walk(child);
  }
  walk(project);
  return result;
}
// Unsent Files cannot be serialized safely. Keep them scoped to their original
// account/conversation in memory; a switch never silently uploads them elsewhere.
export function createConversationFiles() {
  const rows = new Map();
  const key = (account, id) => JSON.stringify([account, id || null]);
  return {
    save(account, id, files) {
      rows.set(key(account, id), [...files]);
    },
    read(account, id) {
      return [...(rows.get(key(account, id)) || [])];
    },
    hasFiles() {
      return [...rows.values()].some((files) => files.length > 0);
    },
    clear(revoke) {
      for (const files of rows.values()) for (const item of files) revoke(item.url);
      rows.clear();
    },
  };
}

export function takeError(error, t) {
  if (typeof error?.message === 'string' && error.message.startsWith('takes.'))
    return t(error.message);
  const key = {
    version_conflict: 'takes.conflict',
    copies_limit_exceeded: 'takes.capExceeded',
    invalid_take_preferences: 'takes.invalidPreferences',
    project_changed: 'takes.conflict',
    idempotency_conflict: 'takes.wait',
  }[error?.code];
  return t(key || 'takes.operationFailed');
}
export function createReadFence() {
  let generation = 0;
  return {
    begin() {
      return ++generation;
    },
    invalidate() {
      generation++;
    },
    current(token) {
      return token === generation;
    },
    async read(read, apply) {
      const token = ++generation;
      const value = await read();
      if (token !== generation) return false;
      apply(value);
      return true;
    },
  };
}

export function selectedTakeForRevision(takes, selected, revisionId) {
  return (
    takes.find((item) => item.id === selected && item.revision_id === revisionId) ||
    takes.find((item) => item.revision_id === revisionId) ||
    null
  );
}
