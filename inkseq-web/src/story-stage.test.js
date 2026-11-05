import assert from 'node:assert/strict';
import test from 'node:test';
import { STAGES, chapterFrom, stageFrom, withStage } from './story-stage.js';

test('the script comes first, and the storyboard stays the default', () => {
  assert.deepEqual(STAGES, ['script', 'world', 'storyboard', 'library', 'edit']);
  assert.equal(withStage('https://x.test/?project=p1', 'script'), '/?project=p1&stage=script');
});

test('an unknown or missing stage is the storyboard', () => {
  assert.equal(stageFrom(''), 'storyboard');
  assert.equal(stageFrom('?stage=other'), 'storyboard');
  assert.equal(stageFrom('?view=workspace&stage=library'), 'library');
  assert.equal(stageFrom('?stage=edit'), 'edit');
  assert.equal(stageFrom('?stage=world'), 'world');
  assert.equal(stageFrom('?stage=script'), 'script');
  assert.equal(stageFrom(undefined), 'storyboard');
});

test('the address keeps everything else, and the default stage is left out', () => {
  const base = 'https://x.test/?view=workspace&project=p1';
  assert.equal(withStage(base, 'library'), '/?view=workspace&project=p1&stage=library');
  assert.equal(withStage(base + '&stage=edit', 'storyboard'), '/?view=workspace&project=p1');
});

test('only the edit step keeps a chapter in the address', () => {
  const base = 'https://x.test/?project=p1';
  assert.equal(withStage(base, 'edit', 'c2'), '/?project=p1&stage=edit&chapter=c2');
  assert.equal(chapterFrom('?stage=edit&chapter=c2'), 'c2');
  assert.equal(
    withStage(base + '&stage=edit&chapter=c2', 'world', 'c2'),
    '/?project=p1&stage=world',
  );
  assert.equal(withStage(base + '&stage=edit&chapter=c2', 'storyboard'), '/?project=p1');
  assert.equal(chapterFrom('?stage=edit'), null);
});
