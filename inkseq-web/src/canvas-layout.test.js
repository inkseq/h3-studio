import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveCanvasPositions, positionsOverlap, freePosition } from './canvas-layout.js';
import { createStore } from './store.js';
const entities = [
  { id: 'chapter', type: 'chapter', order: 0 },
  { id: 'scene', type: 'scene', parentId: 'chapter', order: 0 },
  { id: 'shot', type: 'shot', parentId: 'scene', order: 0 },
  { id: 'image', type: 'image', order: 0 },
];
test('new and hidden nodes avoid existing cards without moving the user layout', () => {
  const saved = { image: { x: 420, y: 40 }, chapter: { x: 1500, y: -100 } };
  const result = resolveCanvasPositions(entities, saved);
  assert.deepEqual(result.image, saved.image);
  assert.deepEqual(result.chapter, saved.chapter);
  assert.equal(positionsOverlap(result.scene, result.image), false);
  for (const [id, a] of Object.entries(result))
    for (const [other, b] of Object.entries(result))
      if (id !== other) assert.equal(positionsOverlap(a, b), false, `${id} overlaps ${other}`);
  assert.deepEqual(resolveCanvasPositions(entities, saved), result);
  assert.deepEqual(saved, { image: { x: 420, y: 40 }, chapter: { x: 1500, y: -100 } });
});
test('saved overlapping positions stay untouched until an explicit tidy action', () => {
  const saved = { chapter: { x: 50, y: 50 }, scene: { x: 50, y: 50 } };
  const result = resolveCanvasPositions(entities, saved);
  assert.deepEqual(result.chapter, saved.chapter);
  assert.deepEqual(result.scene, saved.scene);
});
test('placement jumps past multiple rectangles and ignores other columns', () => {
  assert.deepEqual(
    freePosition({ x: 0, y: 0 }, [
      { x: 0, y: 0 },
      { x: 0, y: 260 },
      { x: 800, y: 900 },
    ]),
    { x: 0, y: 520 },
  );
});
test('sequentially adding chapters, scenes, shots and media never stacks siblings from different parents', () => {
  const map = new Map(),
    storage = { getItem: (k) => map.get(k) ?? null, setItem: (k, v) => map.set(k, v) },
    s = createStore({ storage });
  s.newProject('布局测试');
  for (let i = 0; i < 3; i++) {
    const c = s.addEntity('chapter'),
      scene = s.addEntity('scene', c);
    s.addEntity('shot', scene);
    s.addEntity('image');
  }
  const p = s.exportProject(),
    coords = Object.values(p.layout.positions);
  for (let i = 0; i < coords.length; i++)
    for (let j = i + 1; j < coords.length; j++)
      assert.equal(positionsOverlap(coords[i], coords[j]), false);
  const original = structuredClone(p.layout.positions);
  s.setPositions({ [p.entities[0].id]: { x: 2100, y: 300 } });
  s.addEntity('audio');
  assert.deepEqual(s.getState().project.layout.positions[p.entities[0].id], { x: 2100, y: 300 });
  for (const e of p.entities.slice(1))
    assert.deepEqual(s.getState().project.layout.positions[e.id], original[e.id]);
});
