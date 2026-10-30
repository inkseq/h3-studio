// Project-open presentation is deliberately separate from account/submit errors.
// A failed read must not become a sticky global generation error.
export function projectOpenFailure(error) {
  if (error?.code === 'storyOpen.removed') return { key: 'storyOpen.removed' };
  if (error?.code === 'storyOpen.versionConflict') return { key: 'storyOpen.versionConflict' };
  if (error?.code === 'storyOpen.preserveFailed') return { key: 'storyOpen.preserveFailed' };
  if ([401, 403, 404].includes(error?.status))
    return { key: error.status === 401 ? 'storyOpen.signIn' : 'storyOpen.notFound' };
  if (error?.code === 'storyOpen.invalid') return { key: 'storyOpen.invalid' };
  return {
    key: 'storyOpen.failed',
    statusCode: Number.isInteger(error?.status) ? String(error.status) : 'read',
  };
}

export const storyTitle = (project, fallback) =>
  typeof project?.title === 'string' && project.title.trim() ? project.title : fallback;

export function openedStoryHref(href, detail, workspace, account) {
  const url = new URL(href);
  if (
    url.pathname !== '/' ||
    !detail?.account ||
    detail.account !== account ||
    workspace.workspace.mode !== 'cloud' ||
    workspace.workspace.account !== detail.account ||
    workspace.project.id !== detail.projectId
  )
    return null;
  const same = url.searchParams.get('project') === detail.projectId;
  url.searchParams.set('view', 'workspace');
  url.searchParams.set('project', detail.projectId);
  if (!same) {
    url.searchParams.delete('entity');
    url.searchParams.delete('panel');
  }
  return '/' + url.search;
}

// Identity remains mandatory even for a read-only legacy document. Unsupported
// schemas are never relabelled v4 or silently posted back to the server.
export function readableProjectRecord(record, expectedId) {
  if (
    !record ||
    record.id !== expectedId ||
    !Number.isInteger(record.version) ||
    record.version < 1 ||
    !record.project ||
    typeof record.project !== 'object' ||
    Array.isArray(record.project) ||
    (record.project.id != null && record.project.id !== record.id)
  ) {
    const error = Error('storyOpen.invalid');
    error.code = 'storyOpen.invalid';
    throw error;
  }
  return record;
}

// React and tests share this small navigation lease. A changed destination
// invalidates a pending read before it can replace the current workspace.
export function createStoryRouteLoader({ cloud, store }) {
  let destination = null,
    attempted = null;
  return {
    update({ account, projectId, active = true, busy = false }) {
      const key = active && projectId ? `${account || ''}:${projectId}` : null;
      if (key !== destination) {
        destination = key;
        attempted = null;
        const opening = cloud.getState().projectOpen;
        if (!key || opening?.projectId !== projectId || opening?.account !== account)
          cloud.dismissProjectOpen();
        else if (['draft', 'legacy', 'error'].includes(opening.status)) attempted = key;
      }
      if (!key || !account || busy) return null;
      const live = store.getState();
      if (
        live.workspace.mode === 'cloud' &&
        live.workspace.account === account &&
        live.project.id === projectId
      )
        return null;
      if (attempted === key) return null;
      attempted = key;
      return cloud.open(projectId, { isCurrent: () => destination === key }).catch(() => false);
    },
    destroy() {
      destination = null;
      attempted = null;
      cloud.dismissProjectOpen();
    },
  };
}
