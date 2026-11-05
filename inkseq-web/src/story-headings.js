// Default chapter and scene names come from whoever made the story (assistant, server or an older
// build), often in the other language. A default chapter name is not worth showing, and "Scene N"
// shows in the reader's language. Names the user typed stay as they are.
export const DEFAULT_CHAPTER_NAMES = new Set(['Story', '故事', '第一章', 'Chapter 1']);

export function sceneHeading(title, t) {
  const n = /^(?:Scene |场景 ?)(\d+)$/.exec(title || '')?.[1];
  return n ? t('storyboard.sceneN', { number: n }) : title;
}
