import test from 'node:test';
import assert from 'node:assert/strict';
import { nextCard, openKey } from './card-grid.js';

// Two rows of three 100×80 cards, then one card on a third row.
const rects = [0, 1, 2, 3, 4, 5, 6].map((i) => ({
  left: (i % 3) * 110,
  top: Math.floor(i / 3) * 90,
  width: 100,
  height: 80,
}));

test('arrow keys move by reading order sideways and by row up and down', () => {
  assert.equal(nextCard(rects, 0, 'ArrowRight'), 1);
  assert.equal(nextCard(rects, 2, 'ArrowRight'), 3);
  assert.equal(nextCard(rects, 0, 'ArrowLeft'), 0);
  assert.equal(nextCard(rects, 1, 'ArrowDown'), 4);
  assert.equal(nextCard(rects, 5, 'ArrowDown'), 6);
  assert.equal(nextCard(rects, 6, 'ArrowDown'), 6);
  assert.equal(nextCard(rects, 4, 'ArrowUp'), 1);
  assert.equal(nextCard(rects, 6, 'ArrowUp'), 3);
  assert.equal(nextCard(rects, 4, 'Home'), 0);
  assert.equal(nextCard(rects, 0, 'End'), 6);
  assert.equal(nextCard([], 0, 'End'), -1);
});

test('Enter or Space on the card itself opens it; keys on its buttons do not', () => {
  let opened = 0,
    prevented = 0;
  const card = {};
  const event = (key, target = card) => ({
    key,
    target,
    currentTarget: card,
    preventDefault: () => prevented++,
  });
  openKey(event('Enter'), () => opened++);
  openKey(event(' '), () => opened++);
  openKey(event('Enter', {}), () => opened++);
  openKey(event('a'), () => opened++);
  assert.equal(opened, 2);
  assert.equal(prevented, 2);
});
