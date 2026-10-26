import { agentLinkSearch, readAgentLink } from './agent-navigation.js';

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
