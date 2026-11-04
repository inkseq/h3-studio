import assert from 'node:assert/strict';
import test from 'node:test';
import { stageFrom, withStage } from './story-stage.js';

test('an unknown or missing stage is the storyboard', () => {
  assert.equal(stageFrom(''), 'storyboard');
  assert.equal(stageFrom('?stage=other'), 'storyboard');
  assert.equal(stageFrom('?view=workspace&stage=library'), 'library');
  assert.equal(stageFrom('?stage=edit'), 'edit');
  assert.equal(stageFrom(undefined), 'storyboard');
});

test('the address keeps everything else, and the default stage is left out', () => {
  const base = 'https://x.test/?view=workspace&project=p1';
  assert.equal(withStage(base, 'library'), '/?view=workspace&project=p1&stage=library');
  assert.equal(withStage(base + '&stage=edit', 'storyboard'), '/?view=workspace&project=p1');
});
