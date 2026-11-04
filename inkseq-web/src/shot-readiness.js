// What a shot is missing, from GET /v1/inky/readiness. Advice for a click: the user can
// still generate.
import { useEffect, useState } from 'react';
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
  const fresh = item?.base_version === shot?.entity?.version,
    hints = readinessHints(item, shot?.entity?.version, t),
    [kept, setKept] = useState({ id: null, hints: [] }),
    known = hints.join('\n');
  useEffect(() => {
    if (fresh) setKept({ id: shot?.id, hints: known ? known.split('\n') : [] });
  }, [fresh, shot?.id, known]);
  // While a save outruns the read, the last advice stays, so controls below do not move (#1033).
  return !fresh && kept.id === shot?.id ? kept.hints : hints;
}
