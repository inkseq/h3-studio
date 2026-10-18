import { mergeActivity } from './agent-navigation.js';

export const emptyActivityWindow = () => ({
  events: [],
  cursor: null,
  loaded: false,
  windowReset: false,
});

/** Poll one newest page only. Never display an unmarked gap as a contiguous history. */
export function updateActivityWindow(current, result, { older = false } = {}) {
  const items = result?.items,
    cursor = result?.next_before_version ?? null;
  if (!Array.isArray(items) || (cursor !== null && (!Number.isInteger(cursor) || cursor < 1)))
    throw Error('Invalid activity page');
  if (!current.loaded)
    return { events: mergeActivity([], items), cursor, loaded: true, windowReset: false };
  if (older)
    return {
      events: mergeActivity(current.events, items),
      cursor,
      loaded: true,
      windowReset: false,
    };
  // An empty, complete initial window can safely start tracking later additions.
  if (!current.events.length)
    return { events: mergeActivity([], items), cursor, loaded: true, windowReset: false };
  const ids = new Set(current.events.map((event) => event.id));
  const overlaps = items.some((event) => ids.has(event.id));
  if (items.length && !overlaps) {
    // More than one page may have arrived since the last poll. Reset the visible
    // window and cursor together; keeping the old cursor would skip those events.
    return { events: mergeActivity([], items), cursor, loaded: true, windowReset: cursor !== null };
  }
  return { ...current, events: mergeActivity(current.events, items) };
}
