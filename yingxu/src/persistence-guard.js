export function hasPersistenceRisk(store) {
  const risk = store.getPersistenceRisk();
  return risk.current || risk.otherCloudDrafts > 0;
}

// Only attach while edits exist solely in memory. A dirty cloud draft already
// saved to browser storage is safe to refresh and must not trigger a warning.
export function installPersistenceGuard(store, target = globalThis.window) {
  let attached = false;
  const beforeUnload = (event) => {
    if (!store.checkpoint()) {
      event.preventDefault();
      event.returnValue = true;
    }
  };
  const update = () => {
    const needed = hasPersistenceRisk(store);
    if (needed === attached) return;
    attached = needed;
    if (needed) target.addEventListener('beforeunload', beforeUnload);
    else target.removeEventListener('beforeunload', beforeUnload);
  };
  update();
  const unsubscribe = store.subscribe(update);
  return () => {
    unsubscribe();
    if (attached) target.removeEventListener('beforeunload', beforeUnload);
  };
}
