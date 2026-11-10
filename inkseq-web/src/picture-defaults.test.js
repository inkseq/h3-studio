import test from 'node:test';
import assert from 'node:assert/strict';
import {
  applyPictureDefaults,
  defaultCopies,
  pictureDefaults,
  setPictureDefaults,
} from './picture-defaults.js';
import { applyStoryboardAction } from './storyboard-actions.js';

test('defaults read safely and save only known values (#891)', () => {
  assert.deepEqual(pictureDefaults({}), {
    aspect: '16:9',
    quality: 'draft',
    keepAudio: true,
    copies: 1,
    set: false,
  });
  const p = { journey: { brief: { aspect: '9:16' } } };
  setPictureDefaults(p, { aspect: '9:16', quality: 'final', keepAudio: false, copies: 9 });
  assert.deepEqual(p.journey.pictureDefaults, {
    aspect: '9:16',
    quality: 'final',
    keepAudio: false,
    copies: 1,
  });
  assert.equal(p.journey.brief.aspect, '9:16');
  assert.equal(pictureDefaults(p).set, true);
});

test('a shot takes the defaults', () => {
  const shot = { data: { storyboard: { quality: 'draft' }, h3: { controls: { seed: '7' } } } };
  applyPictureDefaults(shot, { aspect: '1:1', quality: 'final', keepAudio: false });
  assert.equal(shot.data.storyboard.quality, 'final');
  assert.deepEqual(shot.data.h3.controls, {
    seed: '7',
    aspect_ratio: '1:1',
    generate_audio: false,
  });
  applyPictureDefaults(shot, { aspect: '16:9', quality: 'draft', keepAudio: true });
  assert.equal(shot.data.h3.controls.generate_audio, undefined);
});

test('a new shot starts with the story default; without one it is unchanged', () => {
  const project = () => ({
    id: 'p',
    title: 'T',
    journey: {},
    entities: [{ id: 'c', type: 'chapter', parentId: null, title: 'C', data: {}, version: 1 }],
    links: [],
    jobs: [],
  });
  const plain = project();
  const id = applyStoryboardAction(plain, { type: 'add', title: 'S' }, { chapterId: 'c' }).id;
  assert.equal(plain.entities.find((e) => e.id === id).data.h3, undefined);
  const story = project();
  setPictureDefaults(story, { aspect: '9:16', quality: 'final', keepAudio: false, copies: 2 });
  const added = applyStoryboardAction(story, { type: 'add', title: 'S' }, { chapterId: 'c' }).id;
  const shot = story.entities.find((e) => e.id === added);
  assert.equal(shot.data.storyboard.quality, 'final');
  assert.equal(shot.data.h3.controls.aspect_ratio, '9:16');
  assert.equal(shot.data.h3.controls.generate_audio, false);
  // Takes per generation start at the default, within the account's limit.
  assert.equal(defaultCopies({ project: { pictureDefaults: { copies: 3 } }, copiesMax: 2 }), '2');
  assert.equal(defaultCopies({ project: {}, copiesMax: 4 }), '1');
});
