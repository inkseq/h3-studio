// Which step of a story is showing (h3-studio#995). Each has its own address, so a reload or a
// shared link stays on it. Every step is a view of the same project.
export const STAGES = Object.freeze(['storyboard', 'library', 'edit']);
const DEFAULT = 'storyboard';

export function stageFrom(search) {
  const value = new URLSearchParams(search || '').get('stage');
  return STAGES.includes(value) ? value : DEFAULT;
}

// The same address with the stage in it. The default step keeps the address short.
export function withStage(url, stage) {
  const next = new URL(url);
  if (stage === DEFAULT) next.searchParams.delete('stage');
  else next.searchParams.set('stage', stage);
  return next.pathname + next.search + next.hash;
}
