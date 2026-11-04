import test from 'node:test';
import assert from 'node:assert/strict';
import { rememberStoryPreview, storyPreview } from './story-preview.js';

test('a preview shows the copy already read for that account, under its own id', () => {
  rememberStoryPreview('a', 'p1', { id: 'p1', title: 'Rain', entities: [{ id: 's' }] });
  const seen = storyPreview('a', 'p1', 'ignored');
  assert.equal(seen.id, 'story-opening');
  assert.equal(seen.title, 'Rain');
  assert.equal(seen.entities.length, 1);
  assert.equal(seen.placeholder, undefined);
  assert.equal(storyPreview('b', 'p1', 'Other').title, 'Other');
  assert.equal(storyPreview('b', 'p1', 'Other').placeholder, true);
});

test('only the most recent stories are kept', () => {
  for (let i = 0; i < 40; i++) rememberStoryPreview('c', 'p' + i, { id: 'p' + i, entities: [] });
  assert.equal(storyPreview('c', 'p0').placeholder, true);
  assert.equal(storyPreview('c', 'p39').placeholder, undefined);
});
