import test from 'node:test';
import assert from 'node:assert/strict';
import { pickedPictures, togglePicture } from './shot-card-pictures.js';

const images = ['a', 'b', 'c'];

test('no pick sends the main picture; stale picks are ignored', () => {
  assert.deepEqual(pickedPictures({}, 'hero', images), ['a']);
  assert.deepEqual(pickedPictures({ cardImages: { hero: ['c', 'x'] } }, 'hero', images), ['c']);
  assert.deepEqual(pickedPictures({ cardImages: { hero: ['x'] } }, 'hero', images), ['a']);
  assert.deepEqual(pickedPictures({}, 'hero', []), []);
});

test('tapping adds up to two, replaces the older, and never empties the pick', () => {
  let shot = { cardImages: { other: ['z'] } };
  shot = { cardImages: togglePicture(shot, 'hero', images, 'b') };
  assert.deepEqual(shot.cardImages, { other: ['z'], hero: ['a', 'b'] });
  shot = { cardImages: togglePicture(shot, 'hero', images, 'c') };
  assert.deepEqual(shot.cardImages.hero, ['b', 'c']);
  shot = { cardImages: togglePicture(shot, 'hero', images, 'c') };
  assert.deepEqual(shot.cardImages.hero, ['b']);
  // The last pick stays picked.
  assert.deepEqual(togglePicture(shot, 'hero', images, 'b').hero, ['b']);
  // Back to only the main picture stores nothing.
  shot = { cardImages: togglePicture({ cardImages: { hero: ['a', 'b'] } }, 'hero', images, 'b') };
  assert.deepEqual(shot.cardImages, {});
});
