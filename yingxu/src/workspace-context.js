// A workspace visit is distinct from a project ID: an imported copy, logout,
// or leaving and reopening the same project must invalidate pending writes.
export function captureWorkspace(store) {
  const s = store.getState();
  return {
    epoch: s.workspaceEpoch,
    mode: s.workspace.mode,
    account: s.workspace.account || null,
    projectId: s.project.id,
  };
}
export function assertWorkspace(store, context) {
  const live = captureWorkspace(store);
  if (Object.keys(context).some((key) => context[key] !== live[key]))
    throw Error('账户或作品已经切换；迟到的素材结果没有写入当前作品，已保存的原文件不会被删除。');
}

// Set synchronously before the first await; React state alone cannot prevent
// two drop/change events in the same render from starting duplicate uploads.
export function createUploadGate() {
  let busy = false;
  return {
    get busy() {
      return busy;
    },
    async run(fn) {
      if (busy) return false;
      busy = true;
      try {
        await fn();
        return true;
      } finally {
        busy = false;
      }
    },
  };
}
