import test from 'node:test';
import assert from 'node:assert/strict';
import { castCanvasLinks } from './canvas-references.js';
test('canvas shows inherited look and only the effective gallery without mutating project links', () => {
  const p = {
    entities: [
      { id: 's', type: 'scene', data: { cast: [{ characterId: 'c', lookId: 'day' }] } },
      { id: 'a', type: 'shot', parentId: 's', data: {} },
      {
        id: 'b',
        type: 'shot',
        parentId: 's',
        data: { cast: [{ characterId: 'c', lookId: 'night' }] },
      },
      {
        id: 'c',
        type: 'character',
        data: {
          looks: [
            { id: 'day', name: '白天', gallery: { front: 'i1' } },
            { id: 'night', name: '夜晚', gallery: { front: 'i2' } },
          ],
        },
      },
      { id: 'i1', type: 'image' },
      { id: 'i2', type: 'image' },
    ],
    links: [],
  };
  const before = structuredClone(p),
    edges = castCanvasLinks(p);
  assert.equal(edges.length, 4);
  assert.ok(edges.some((e) => e.source === 'i1' && e.target === 'a'));
  assert.ok(edges.some((e) => e.source === 'i2' && e.target === 'b'));
  assert.ok(!edges.some((e) => e.source === 'i1' && e.target === 'b'));
  assert.match(edges.find((e) => e.source === 'c' && e.target === 'a').label, /继承/);
  assert.deepEqual(p, before);
  p.links.push({ source: 'i1', target: 'a', role: 'identity' });
  assert.equal(castCanvasLinks(p).filter((e) => e.source === 'i1' && e.target === 'a').length, 0);
  p.entities.push({ id: 'render', type: 'video' });
  p.entities.find((e) => e.id === 'a').data.selectedAssetId = 'render';
  assert.ok(
    castCanvasLinks(p).some(
      (e) => e.source === 'render' && e.target === 'a' && e.label === '已采用候选',
    ),
  );
  assert.equal(p.links.length, 1);
});
