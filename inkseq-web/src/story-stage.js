// Which step of a story is showing (h3-studio#995). Each has its own address, so a reload or a
// shared link stays on it. Every step is a view of the same project.
export const STAGES = Object.freeze(['script', 'world', 'storyboard', 'library', 'edit']);
const DEFAULT = 'storyboard';

export function stageFrom(search) {
  const value = new URLSearchParams(search || '').get('stage');
  return STAGES.includes(value) ? value : DEFAULT;
}

// Which chapter the Edit step shows, on a story with several.
export function chapterFrom(search) {
  return new URLSearchParams(search || '').get('chapter') || null;
}

// The same address with the stage in it. The default step keeps the address short. Only Edit is
// about one chapter, so only Edit keeps a chapter in the address.
export function withStage(url, stage, chapter = null) {
  const next = new URL(url);
  if (stage === DEFAULT) next.searchParams.delete('stage');
  else next.searchParams.set('stage', stage);
  if (stage === 'edit' && chapter) next.searchParams.set('chapter', chapter);
  else next.searchParams.delete('chapter');
  return next.pathname + next.search + next.hash;
}
