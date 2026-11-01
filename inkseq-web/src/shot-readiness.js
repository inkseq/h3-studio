// What a shot is missing, from GET /v1/inky/readiness. Advice for a click: the user can
// still generate.
import { useQuery } from '@tanstack/react-query';
import { useStoryboardUI } from './storyboard-ui-context.jsx';
import { readinessHints } from './storyboard-domain.js';

export function useShotReadiness(shot, t) {
  const { state, adapter } = useStoryboardUI();
  const read = adapter?.readShotReadiness;
  const saved = state.shots.reduce((sum, row) => sum + (row.entity?.version || 0), 0);
  const query = useQuery({
    queryKey: ['shot-readiness', state.contextId, state.project.id, saved],
    queryFn: () => read(),
    enabled: !!read && state.workspaceMode === 'cloud',
    staleTime: 30000,
    placeholderData: (previous) => previous,
    // Not deployed yet: no hints rather than retries.
    retry: false,
  });
  const item = query.data?.items?.find((row) => row.shot_id === shot?.id);
  return readinessHints(item, shot?.entity?.version, t);
}
