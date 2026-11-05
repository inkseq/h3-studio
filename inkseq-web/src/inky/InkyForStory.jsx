// Inky on the storyboard page: suggestions come from the server's Jev judgements
// (adapter.readInkySuggestions → GET /v1/inky/suggestions, h3-studio#685). The button is always
// there on a cloud story and says nothing until it has something to say (owner, 2026-11-05). Opening a story asks the server
// to judge shots saved before Inky was on (POST /v1/inky/refresh). A local development preview can use ?inky=demo, which shows one sample item per kind on the first shots.
import React, { useEffect } from 'react';
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

// The server judges at most three shots per call, after it answers.
const JUDGED_PER_CALL = 3,
  MAX_CALLS = 10,
  JUDGING_MS = 6000;

// Ask, wait, read again, until nothing is left or the page moves on.
function judgeWaiting(refresh, reread) {
  let stopped = false,
    timer = 0;
  const wait = () =>
    new Promise((done) => {
      timer = setTimeout(done, JUDGING_MS);
    });
  (async () => {
    for (let call = 0; call < MAX_CALLS && !stopped; call += 1) {
      const pending = (await refresh())?.pending || 0;
      if (!pending || stopped) return;
      await wait();
      if (stopped) return;
      reread();
      if (pending <= JUDGED_PER_CALL) return;
    }
  })().catch(() => {}); // Advice is optional: the story works without it.
  return () => {
    stopped = true;
    clearTimeout(timer);
  };
}

const demoItems = (shots) =>
  ['vague_action', 'needs_keyframe'].flatMap((kind, i) =>
    shots[i % Math.max(shots.length, 1)]
      ? [{ id: 'demo-' + kind, kind, shot_id: shots[i % shots.length].id, example: '' }]
      : [],
  );

export function InkyForStory({ selectedShot }) {
  const { state, dispatch, adapter } = useStoryboardUI();
  const demo = demoOn(),
    read = adapter.readInkySuggestions,
    refresh = adapter.refreshInky;
  const live = !!read && !demo && state.workspaceMode === 'cloud' && !state.placeholder;
  const query = useQuery({
    queryKey: ['inky', state.contextId, state.project.id],
    queryFn: () => read(),
    enabled: live,
    staleTime: 60000,
    // Not deployed yet or switched off: stay hidden rather than retrying.
    retry: false,
  });
  const reread = query.refetch;
  useEffect(() => {
    if (!live || !refresh) return undefined;
    return judgeWaiting(refresh, reread);
  }, [live, refresh, reread, state.project.id]);
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
      selectedShot={selectedShot}
      onIgnore={(item) => adapter.ignoreInkySuggestion?.(item.id)}
      onRewrite={adapter.rewriteInkySuggestion}
      onApply={(id, prompt) => dispatch({ type: 'update', id, patch: { prompt } })}
    />
  );
}
