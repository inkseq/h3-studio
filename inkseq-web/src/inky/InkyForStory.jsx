// Inky on the storyboard page: suggestions come from the server's Jev judgements
// (adapter.readInkySuggestions → GET /v1/inky/suggestions, h3-studio#685). With no reader,
// an error or no items, Inky stays hidden. A local development preview can use ?inky=demo, which shows one sample item per kind on the first shots.
import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useStoryboardUI } from '../storyboard-ui-context.jsx';
import { Inky } from './Inky.jsx';

const DEMO_KEY = 'inky-demo';
function demoOn() {
  if (!import.meta.env?.DEV) return false;
  try {
    if (new URLSearchParams(window.location.search).get('inky') === 'demo')
      sessionStorage.setItem(DEMO_KEY, '1');
    return sessionStorage.getItem(DEMO_KEY) === '1';
  } catch {
    return false; // Storage blocked: the preview simply stays off.
  }
}

const demoItems = (shots) =>
  ['vague_action', 'needs_keyframe'].flatMap((kind, i) =>
    shots[i % Math.max(shots.length, 1)]
      ? [{ id: 'demo-' + kind, kind, shot_id: shots[i % shots.length].id, example: '' }]
      : [],
  );

export function InkyForStory() {
  const { state, adapter } = useStoryboardUI();
  const demo = demoOn(),
    read = adapter.readInkySuggestions;
  const query = useQuery({
    queryKey: ['inky', state.contextId, state.project.id],
    queryFn: () => read(),
    enabled: !!read && !demo && state.workspaceMode === 'cloud',
    staleTime: 60000,
    // Not deployed yet or switched off: stay hidden rather than retrying.
    retry: false,
  });
  const items = demo ? demoItems(state.shots) : query.data?.items || [];
  const shotIds = new Set(state.shots.map((shot) => shot.id));
  const shotNumber = (id) => state.shots.findIndex((shot) => shot.id === id) + 1;
  if (!demo && !read) return null;
  return (
    <Inky
      projectId={state.project.id}
      items={items}
      shotIds={shotIds}
      shotNumber={shotNumber}
      onIgnore={(item) => adapter.ignoreInkySuggestion?.(item.id)}
    />
  );
}
