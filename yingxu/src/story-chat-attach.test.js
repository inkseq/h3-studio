import test from 'node:test';
import assert from 'node:assert/strict';
import {
  attachActions,
  attachmentNote,
  cardPicturePatch,
  cardTargets,
  fileKind,
  joinText,
  uploadReady,
} from './story-chat-attach.js';

test('file kinds: media by type, text by type or extension', () => {
  assert.equal(fileKind({ type: 'image/png' }), 'image');
  assert.equal(fileKind({ type: 'video/mp4' }), 'video');
  assert.equal(fileKind({ type: 'audio/mpeg' }), 'audio');
  assert.equal(fileKind({ type: '', name: 'Script.MD' }), 'text');
  assert.equal(fileKind({ type: 'application/pdf', name: 'a.pdf' }), null);
});

test('only photos can go on character, place and prop cards', () => {
  const cards = [
    { id: 'c', kind: 'character' },
    { id: 'p', kind: 'place' },
    { id: 's', kind: 'shot' },
  ];
  assert.deepEqual(
    cardTargets(cards, 'image').map((card) => card.id),
    ['c', 'p'],
  );
  assert.deepEqual(cardTargets(cards, 'video'), []);
});

test('picture patch matches the server: default look, default version, prop list', () => {
  const character = { type: 'character', data: {} };
  assert.deepEqual(cardPicturePatch(character, 'asset-1'), {
    looks: [
      {
        id: 'main',
        name: '默认造型',
        description: '',
        gallery: {},
        imageIds: ['asset-1'],
        version: 2,
      },
    ],
    defaultLookId: 'main',
  });
  const place = {
    type: 'location',
    data: {
      defaultVersionId: 'night',
      versions: [
        { id: 'day', imageIds: [] },
        { id: 'night', imageIds: ['old'], version: 3 },
      ],
    },
  };
  const patch = cardPicturePatch(place, 'asset-2');
  assert.deepEqual(patch.versions[1], { id: 'night', imageIds: ['asset-2', 'old'], version: 4 });
  assert.equal(place.data.versions[1].imageIds.length, 1, 'input untouched');
  assert.deepEqual(
    cardPicturePatch({ type: 'prop', data: { imageIds: ['x', 'asset-3'] } }, 'asset-3'),
    {
      imageIds: ['asset-3', 'x'],
    },
  );
});

test('actions attach to the story, and to the card when one is chosen', () => {
  const project = { entities: [{ id: 'char-1', type: 'character', data: {} }] };
  const library = attachActions(project, { assetId: 'a1', title: 'clip.mp4' });
  assert.deepEqual(library, [
    { op: 'asset.attach', asset_id: 'a1', entity_id: 'asset-a1', title: 'clip.mp4' },
  ]);
  const onCard = attachActions(project, { assetId: 'a2', title: 'me.jpg', cardId: 'char-1' });
  assert.equal(onCard[1].op, 'entity.update');
  assert.deepEqual(onCard[1].patch.data.looks[0].imageIds, ['asset-a2']);
});

test('note names what went where; text joins under the limit', () => {
  const t = (key, values = {}) =>
    ({
      'storyChat.note.onCard': `${values.file}→${values.card}`,
      'storyChat.note.video': `video ${values.file}`,
      'storyChat.note.separator': '; ',
      'storyChat.note.wrap': `[${values.list}]`,
    })[key];
  assert.equal(
    attachmentNote(
      [
        { name: 'a.jpg', cardName: 'Mei', kind: 'image' },
        { name: 'b.mp4', kind: 'video' },
      ],
      t,
    ),
    '[a.jpg→Mei; video b.mp4]',
  );
  assert.equal(attachmentNote([], t), '');
  assert.deepEqual(joinText('idea  ', 'script', 100), { text: 'idea\n\nscript', cut: false });
  assert.deepEqual(joinText('', 'abcdef', 4), { text: 'abcd', cut: true });
});

test('upload waits until the asset is ready, and gives up on failure', async () => {
  const states = ['processing', 'ready'];
  const api = {
    upload: async () => ({ asset_id: 'a1', status: 'processing' }),
    asset: async () => ({ asset_id: 'a1', status: states.shift() }),
  };
  assert.equal(await uploadReady(api, {}, 'p1', { sleep: async () => {} }), 'a1');
  const failing = {
    upload: async () => ({ asset_id: 'a2', status: 'processing' }),
    asset: async () => ({ asset_id: 'a2', status: 'failed' }),
  };
  await assert.rejects(
    uploadReady(failing, {}, 'p1', { sleep: async () => {} }),
    /asset_not_ready/,
  );
});
