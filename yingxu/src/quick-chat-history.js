export const CARD_HISTORY_PAGE_SIZE = 10;
export const emptyCardHistory = () => ({
  cardHistoryCursor: null,
  cardHistoryLoaded: false,
  cardHistoryHasMore: false,
  cardHistoryLoading: false,
  cardHistoryError: null,
  textHistoryLoading: false,
  textHistoryError: null,
  cardTurns: {},
  liveCardIds: [],
});
const id = (value) => typeof value === 'string' && value.length > 0 && value.length <= 160;
export function checkedCardPage(value, sessionId, beforeCursor, limit = CARD_HISTORY_PAGE_SIZE) {
  if (
    value?.session_id !== sessionId ||
    value.limit !== limit ||
    !Array.isArray(value.cards) ||
    value.cards.length > limit ||
    typeof value.has_more !== 'boolean' ||
    value.start_reached !== !value.has_more ||
    (value.has_more &&
      (typeof value.next_cursor !== 'string' ||
        !value.next_cursor ||
        value.next_cursor.length > 4096 ||
        value.next_cursor === beforeCursor)) ||
    (!value.has_more && value.next_cursor != null)
  )
    throw Error('quickHistory.invalid');
  const seen = new Set();
  let previous;
  for (const card of value.cards) {
    if (
      !id(card.id) ||
      !Number.isFinite(card.created_at) ||
      seen.has(card.id) ||
      (card.submission_ids != null &&
        (!Array.isArray(card.submission_ids) || card.submission_ids.some((value) => !id(value))))
    )
      throw Error('quickHistory.invalid');
    const next = [card.created_at, card.id];
    if (previous && (next[0] < previous[0] || (next[0] === previous[0] && next[1] < previous[1])))
      throw Error('quickHistory.invalid');
    previous = next;
    seen.add(card.id);
  }
  if (!value.cards.length && value.has_more) throw Error('quickHistory.invalid');
  return value;
}
