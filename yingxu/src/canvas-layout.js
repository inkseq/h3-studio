// Node cards are 258px wide; reserve room for their longest two-line title.
export const CARD_BOUNDS = Object.freeze({ width: 258, height: 240, gap: 20 });
const valid = (p) => p && Number.isFinite(p.x) && Number.isFinite(p.y);
const ordered = (a, b) => (a.order ?? 0) - (b.order ?? 0) || a.id.localeCompare(b.id);
export function positionsOverlap(a, b, { width, height, gap } = CARD_BOUNDS) {
  return Math.abs(a.x - b.x) < width + gap && Math.abs(a.y - b.y) < height + gap;
}
export function freePosition(preferred, occupied) {
  const point = { ...preferred };
  // Jump past colliding rectangles; never move an existing user-placed card.
  for (;;) {
    const collisions = occupied.filter((other) => positionsOverlap(point, other));
    if (!collisions.length) return point;
    point.y =
      Math.max(...collisions.map((other) => other.y)) + CARD_BOUNDS.height + CARD_BOUNDS.gap;
  }
}
function preferredPositions(entities) {
  const positions = {};
  let y = 40;
  for (const chapter of entities.filter((e) => e.type === 'chapter').sort(ordered)) {
    positions[chapter.id] = { x: 80, y };
    const scenes = entities
      .filter((e) => e.type === 'scene' && e.parentId === chapter.id)
      .sort(ordered);
    for (const scene of scenes) {
      positions[scene.id] = { x: 420, y };
      const shots = entities
        .filter((e) => e.type === 'shot' && e.parentId === scene.id)
        .sort(ordered);
      for (const shot of shots) {
        positions[shot.id] = { x: 770, y };
        y += 260;
      }
      if (!shots.length) y += 260;
      y += 35;
    }
    if (!scenes.length) y += 260;
    y += 90;
  }
  let assetY = 40;
  for (const entity of entities.filter((e) => !positions[e.id]).sort(ordered)) {
    positions[entity.id] = { x: -280, y: assetY };
    assetY += 260;
  }
  return positions;
}
export function resolveCanvasPositions(entities, saved = {}) {
  const result = {},
    preferred = preferredPositions(entities);
  for (const entity of entities)
    if (valid(saved[entity.id])) result[entity.id] = { ...saved[entity.id] };
  // Include hidden chapters/scenes too, so expanding a scope cannot create collisions.
  const occupied = Object.values(result);
  for (const [id, point] of Object.entries(preferred))
    if (!result[id]) {
      result[id] = freePosition(point, occupied);
      occupied.push(result[id]);
    }
  return result;
}
