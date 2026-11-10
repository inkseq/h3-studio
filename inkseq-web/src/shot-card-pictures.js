// Which of a card's pictures a reference shot sends (h3-studio#504, manual override).
// The shot saves storyboard.cardImages = { cardId: [imageId, imageId?] }; no entry means the
// card's main picture. The server compiler applies the same rule, so the preview matches.

export const MAX_CARD_PICTURES = 2;

// The pictures this shot sends for one card: its picks still on the card, else the main one.
export function pickedPictures(shot, cardId, images) {
  const chosen = (shot?.cardImages?.[cardId] || []).filter((id) => images.includes(id));
  return chosen.length ? chosen : images.slice(0, 1);
}

// The new cardImages after tapping one picture. Tapping a picked one drops it (never the last);
// tapping another adds it, and when two are already picked it replaces the older pick.
export function togglePicture(shot, cardId, images, imageId) {
  const current = pickedPictures(shot, cardId, images);
  let next;
  if (current.includes(imageId))
    next = current.length > 1 ? current.filter((id) => id !== imageId) : current;
  else next = [...current, imageId].slice(-MAX_CARD_PICTURES);
  const all = { ...(shot?.cardImages || {}) };
  // Only the main picture is the default: store nothing for it.
  if (next.length === 1 && next[0] === images[0]) delete all[cardId];
  else all[cardId] = next;
  return all;
}
