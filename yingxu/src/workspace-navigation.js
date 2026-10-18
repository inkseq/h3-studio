import { agentLinkSearch, readAgentLink } from './agent-navigation.js';

export function workspaceHref(path, search = '') {
  if (!['/', '/freestyle'].includes(path)) throw Error('Unknown workspace view');
  const target = readAgentLink(search);
  return path + (target ? agentLinkSearch(target) : '');
}
// Change the view without reloading the shared project store or its cloud draft.
// Only public location identifiers cross views; arbitrary query values never do.
export function navigateWorkspace(event, path, { search = window.location.search } = {}) {
  if (
    event &&
    (event.defaultPrevented ||
      event.button > 0 ||
      event.ctrlKey ||
      event.metaKey ||
      event.shiftKey ||
      event.altKey)
  )
    return;
  const href = workspaceHref(path, search);
  event?.preventDefault();
  window.history.pushState(null, '', href);
  window.dispatchEvent(new PopStateEvent('popstate'));
  window.scrollTo(0, 0);
}

// Compatibility routing only: no store replacement, write, job or rental.
export function freestyleWorkspaceHref(search = '', state = {}) {
  const params = new URLSearchParams(search),
    target = readAgentLink(search);
  if (params.has('project') && !target) return null;
  const current = state.workspace?.mode === 'cloud' ? state.project?.id : null;
  const next = new URLSearchParams(
    target
      ? agentLinkSearch(target)
      : current
        ? agentLinkSearch({ projectId: current, entityId: state.selectedId || null })
        : '',
  );
  next.set('view', 'workspace');
  return '/?' + next.toString();
}
