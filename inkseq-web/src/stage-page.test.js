import test from 'node:test';
import assert from 'node:assert/strict';
import {
  cardFacts,
  coverId,
  duplicateFiles,
  fileOwners,
  libraryOrder,
  looksLikeFileName,
  memberCrop,
  sharedCovers,
} from './stage-page.js';
import { bibleOf } from './story-bible.js';

const person = (id, images = [], extra = {}) => ({
  id,
  type: 'character',
  name: id,
  def: 'look',
  variants: [{ id: 'look', desc: '', images }],
  voices: [],
  bible: bibleOf(null),
  ...extra,
});
const file = (id, sha, extra = {}) => ({
  id,
  kind: 'image',
  name: id + '.png',
  entity: { data: { metadata: sha ? { sha256: sha } : {} } },
  ...extra,
});

test('a card names what it lacks in plain words, voice only once the character speaks', () => {
  const bare = cardFacts(person('a'));
  assert.deepEqual(bare.gaps, ['picture']);
  assert.equal(bare.photos, 0);
  assert.equal(bare.looks, 1);
  const speaks = cardFacts(person('b', ['img']), { speaking: 2 });
  assert.deepEqual(speaks.gaps, ['speaker', 'voice']);
  const ready = cardFacts(
    person('c', ['img'], {
      voice: 'v',
      voices: [{ asset: 'v' }],
      bible: { ...bibleOf(null), speaker: 'S1' },
    }),
    { speaking: 2, used: true },
  );
  assert.deepEqual(ready.gaps, []);
  assert.equal(ready.voices, 1);
  // A voice chosen from our list alone sends no sample, so it is not a voice yet.
  const listed = cardFacts(
    person('d', ['img'], { voiceId: 'v-steady', bible: { ...bibleOf(null), speaker: 'S2' } }),
    { speaking: 1 },
  );
  assert.deepEqual(listed.gaps, ['voice']);
  assert.equal(listed.voices, 0);
  assert.equal(ready.total, 6);
  assert.ok(ready.done >= 3);
});

test('the cover is the default look first picture, else any picture', () => {
  assert.equal(coverId(person('a', ['x', 'y'])), 'x');
  const p = {
    ...person('b'),
    def: 'gone',
    variants: [
      { id: 'one', images: [] },
      { id: 'two', images: ['z'] },
    ],
  };
  assert.equal(coverId(p), 'z');
  assert.equal(coverId(person('c')), '');
});

test('cards that share one picture (same file or same content) know their order', () => {
  const assets = new Map([
    ['g1', file('g1', 'abc')],
    ['g2', file('g2', 'abc')],
    ['solo', file('solo', 'def')],
  ]);
  const profiles = [
    person('老大', ['g1']),
    person('老二', ['g2']),
    person('老三', ['g1']),
    person('阿妈', ['solo']),
    { ...person('place', ['g1']), type: 'location' },
  ];
  const shared = sharedCovers(profiles, (id) => assets.get(id));
  assert.deepEqual(shared.get('老大'), { n: 3, index: 0 });
  assert.deepEqual(shared.get('老二'), { n: 3, index: 1 });
  assert.deepEqual(shared.get('老三'), { n: 3, index: 2 });
  assert.equal(shared.has('阿妈'), false);
  // A place with the same picture is not one of the three people.
  assert.equal(shared.has('place'), false);
});

test('a group photo is cut into columns and each member shows its own, from the top', () => {
  // A square photo of three people in a portrait card.
  const left = memberCrop({ n: 3, index: 0, width: 1536, height: 1536, frame: 3 / 4 });
  assert.deepEqual(left, { left: -0, top: 0, width: 300, height: 225 });
  const middle = memberCrop({ n: 3, index: 1, width: 1536, height: 1536, frame: 3 / 4 });
  assert.equal(middle.left, -100);
  const right = memberCrop({ n: 3, index: 2, width: 1536, height: 1536, frame: 3 / 4 });
  assert.equal(right.left, -200);
  // The picture keeps its own shape: box width / height = picture aspect × frame aspect⁻¹.
  assert.equal(Math.round((((left.width / left.height) * 3) / 4) * 100) / 100, 1);
  // A wide, low picture: one column is taller than the picture, so the window takes the full
  // height, narrows to the frame's shape and stays inside the picture.
  const wide = memberCrop({ n: 2, index: 1, width: 3000, height: 600, frame: 3 / 4 });
  assert.equal(wide.height, 100);
  assert.ok(wide.left <= 0 && wide.left >= -(wide.width - 100));
  assert.equal(memberCrop({ n: 3, index: 0, width: 0, height: 10 }), null);
  assert.equal(memberCrop({ n: 1, index: 0, width: 10, height: 10 }), null);
});

test('files are named after the cards that use them when their title is only a file name', () => {
  const owners = fileOwners([
    person('老大', ['g1']),
    person('老三', ['g1']),
    { ...person('海滩', ['beach']), type: 'location', ambient: 'waves' },
  ]);
  assert.deepEqual(
    owners.get('g1').map((o) => o.name),
    ['老大', '老三'],
  );
  assert.deepEqual(owners.get('waves'), [{ name: '海滩', type: 'location' }]);
  assert.equal(looksLikeFileName('front-full.v3.png'), true);
  assert.equal(looksLikeFileName('Take.MOV'), true);
  assert.equal(looksLikeFileName('阿妈'), false);
  assert.equal(looksLikeFileName('v3.final'), false);
});

test('copies of one file are found by content and placed side by side', () => {
  const assets = [file('a', 'x'), file('b', 'y'), file('c', 'x'), file('d', '')];
  const dupes = duplicateFiles(assets);
  assert.deepEqual([...dupes.keys()], ['a', 'c']);
  assert.equal(dupes.get('c').n, 2);
  assert.deepEqual(
    libraryOrder(assets, dupes).map((a) => a.id),
    ['a', 'c', 'b', 'd'],
  );
  // Files a shot uses still come first, in shot and take order.
  const used = [file('p', 'x'), { ...file('q', 'z'), use: { shot: 2, take: 1 } }, file('r', 'x')];
  assert.deepEqual(
    libraryOrder(used, duplicateFiles(used)).map((a) => a.id),
    ['q', 'p', 'r'],
  );
});
