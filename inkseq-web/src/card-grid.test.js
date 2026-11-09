import test from 'node:test';
import assert from 'node:assert/strict';
import { applyRovingStops, nextCard, openKey } from './card-grid.js';

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

// The World grid: two single cards, a group box of three members stacked in the third column,
// and a single card on the next row. Members are cards of the same grid, in reading order.
const world = [
  { left: 0, top: 0, width: 260, height: 80 }, // 阿妈
  { left: 270, top: 0, width: 260, height: 80 }, // 狮皮人
  { left: 548, top: 34, width: 244, height: 80 }, // 老大 (in the group box)
  { left: 548, top: 122, width: 244, height: 80 }, // 老二
  { left: 548, top: 210, width: 244, height: 80 }, // 老三
  { left: 0, top: 310, width: 260, height: 80 }, // next row
];

test('arrows reach the members of a group box and move down within the group', () => {
  assert.equal(nextCard(world, 1, 'ArrowRight'), 2);
  assert.equal(nextCard(world, 2, 'ArrowRight'), 3);
  assert.equal(nextCard(world, 2, 'ArrowDown'), 3);
  assert.equal(nextCard(world, 3, 'ArrowDown'), 4);
  assert.equal(nextCard(world, 4, 'ArrowUp'), 3);
  assert.equal(nextCard(world, 4, 'ArrowRight'), 5);
  // Down from a single card beside the group goes to the card under it, not into the group.
  assert.equal(nextCard(world, 0, 'ArrowDown'), 5);
  assert.equal(nextCard(world, 5, 'ArrowUp'), 0);
  assert.equal(nextCard(world, 3, 'End'), 5);
});

// A div as the browser has it: tabIndex reads -1 with no attribute, so a card that was never
// active is not focusable at all unless it gets an explicit tabindex="-1".
class FakeCard {
  constructor(id, inner = []) {
    this.dataset = { gridCard: id };
    this.attributes = new Map();
    this.inner = inner;
    this.classList = { contains: () => false };
  }
  get tabIndex() {
    return this.attributes.has('tabindex') ? Number(this.attributes.get('tabindex')) : -1;
  }
  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }
  hasAttribute(name) {
    return this.attributes.has(name);
  }
  removeAttribute(name) {
    this.attributes.delete(name);
  }
  querySelectorAll() {
    return this.inner;
  }
  get focusable() {
    return this.attributes.has('tabindex');
  }
}

test('every card in the grid, group members too, can take focus and so open with Enter', () => {
  const cards = ['ama', 'lion', 'laoda', 'laoer', 'laosan'].map((id) => new FakeCard(id));
  const root = { querySelectorAll: () => cards };
  applyRovingStops(root, '.pcard', { current: null });
  assert.deepEqual(
    cards.map((card) => card.getAttribute('tabindex')),
    ['0', '-1', '-1', '-1', '-1'],
  );
  assert.ok(cards.every((card) => card.focusable));
  // A member focused by a click becomes the one Tab stop.
  applyRovingStops(root, '.pcard', { current: 'laoer' });
  assert.deepEqual(
    cards.map((card) => card.getAttribute('tabindex')),
    ['-1', '-1', '-1', '0', '-1'],
  );
  let opened = '';
  const member = cards[4];
  for (const key of ['Enter', ' '])
    openKey({ key, target: member, currentTarget: member, preventDefault() {} }, () => {
      opened += member.dataset.gridCard + ';';
    });
  assert.equal(opened, 'laosan;laosan;');
});
