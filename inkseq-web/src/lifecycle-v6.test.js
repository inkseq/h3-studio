import test from 'node:test';
import assert from 'node:assert/strict';
import { createStore } from './store.js';
import { deliveryIssues } from './delivery-model.js';
import { readBundle } from './media.js';

function setup() {
  const store = createStore({ storage: null });
  store.newProject('生命周期独立复核');
  const chapter = store.addEntity('chapter', null, { title: '第一章' }),
    scene = store.addEntity('scene', chapter, {
      title: '站台',
      data: { script: '阿林寻找姐姐。' },
    });
  const shot = store.addEntity('shot', scene, {
    title: '第一镜',
    description: '阿林走进站台。',
    data: { seconds: 4, uxReview: { chosen: 'A', status: 'selected' } },
  });
  const second = store.addEntity('shot', scene, {
    title: '第二镜',
    description: '她听见广播。',
    data: { seconds: 6, uxReview: { chosen: 'B', status: 'selected' } },
  });
  store.updateProject({
    logline: '阿林想找到姐姐，但末班车即将开走。',
    journey: { stage: 7, sound: { mode: 'silent' } },
  });
  const get = () => store.getState().project,
    entity = (id) => get().entities.find((e) => e.id === id),
    issues = () => deliveryIssues(get(), chapter);
  return { store, chapter, scene, shot, second, get, entity, issues };
}
function audioFixture(ctx) {
  const audio = ctx.store.addEntity('audio', null, {
    title: '对白来源',
    data: {
      fileId: 'audio-original',
      fileName: 'dialogue.wav',
      mime: 'audio/wav',
      missingFile: false,
    },
  });
  const track = {
    id: 'track-1',
    assetId: audio,
    shotId: ctx.second,
    role: 'dialogue',
    offset: 0,
    start: 0,
    end: 3,
    duration: 5,
    fileId: 'audio-original',
    gain: 0.7,
    muted: false,
  };
  ctx.store.editProject((p) => {
    p.journey.sound = { mode: 'dialogue' };
    p.journey.soundTracks = { [ctx.chapter]: [track] };
  });
  return { audio, track };
}

test('direct shot content change requires review but title and revision note alone do not', () => {
  const c = setup();
  assert.deepEqual(c.issues(), []);
  c.store.updateEntity(c.shot, { title: '重命名镜头' });
  assert.equal(c.entity(c.shot).status, 'draft');
  c.store.updateEntity(c.shot, {
    data: { uxReview: { ...c.entity(c.shot).data.uxReview, note: '保留表情' } },
  });
  assert.equal(c.entity(c.shot).status, 'draft');
  c.store.updateEntity(c.shot, { description: '阿林转身离开站台。' });
  assert.equal(c.entity(c.shot).status, 'review');
  assert.ok(c.issues().some((i) => i.id === 'review-' + c.shot));
  assert.equal(c.entity(c.shot).data.uxReview.chosen, 'A');
  c.store.updateEntity(c.shot, {
    status: 'draft',
    data: { uxReview: { ...c.entity(c.shot).data.uxReview, status: 'selected' } },
  });
  assert.ok(!c.issues().some((i) => i.id === 'review-' + c.shot));
});

test('deleting selected image can become an explicitly accepted placeholder without orphan review', () => {
  const c = setup(),
    image = c.store.addEntity('image', null, {
      title: '候选图',
      data: { fileId: 'candidate-file' },
    });
  c.store.updateEntity(c.shot, {
    status: 'draft',
    data: { selectedAssetId: image, uxReview: { chosen: null, status: 'selected' } },
  });
  c.store.deleteEntity(image);
  assert.equal(c.entity(c.shot).status, 'review');
  assert.ok(c.issues().some((i) => i.id === 'review-' + c.shot));
  c.store.updateEntity(c.shot, {
    status: 'draft',
    data: {
      acceptPlaceholder: true,
      uxReview: { chosen: null, status: 'placeholder', allRejected: false },
    },
  });
  assert.equal(c.issues().find((i) => i.id === 'candidate-' + c.shot).accepted, true);
  assert.equal(c.issues().filter((i) => !i.accepted).length, 0);
  c.store.updateEntity(c.shot, { description: '这一版改拍空站台。' });
  assert.equal(c.entity(c.shot).data.acceptPlaceholder, false);
  assert.ok(c.issues().some((i) => i.id === 'candidate-' + c.shot && !i.accepted));
});

test('structure-only restore reports missing images of effectively used looks and permits unspecified optional angles', async () => {
  const c = setup(),
    image = c.store.addEntity('image', null, {
      title: '正面参考',
      data: { fileId: 'portrait-file', fileName: 'portrait.jpg', mime: 'image/jpeg', bytes: 2 },
    }),
    character = c.store.addEntity('character', null, {
      title: '阿林',
      data: {
        looks: [
          {
            id: 'rain',
            name: '雨夜外套',
            description: '',
            version: 1,
            gallery: { front: image, side: '' },
          },
          { id: 'office', name: '工作制服', description: '', version: 1, gallery: {} },
        ],
      },
    });
  c.store.editProject((p) => {
    p.entities.find((e) => e.id === c.scene).data.cast = [
      { characterId: character, lookId: 'rain' },
    ];
    p.entities.find((e) => e.id === c.second).data.cast = [
      { characterId: character, lookId: 'office' },
    ];
  });
  const text = JSON.stringify(c.get()),
    restored = await readBundle({
      name: 'project.json',
      size: new TextEncoder().encode(text).length,
      text: async () => text,
    });
  assert.equal(restored.project.entities.find((e) => e.id === image).data.missingFile, true);
  const issues = deliveryIssues(restored.project, c.chapter),
    gallery = issues.filter((i) => i.id.startsWith('gallery-'));
  assert.equal(gallery.length, 1);
  assert.equal(gallery[0].entityId, character);
  assert.equal(gallery[0].lookId, 'rain');
  assert.equal(gallery[0].stage, 3);
  const cleared = structuredClone(restored.project);
  cleared.entities.find((e) => e.id === character).data.looks[0].gallery.front = '';
  assert.equal(
    deliveryIssues(cleared, c.chapter).filter((i) => i.id.startsWith('gallery-')).length,
    0,
  );
});

test('unused look missing gallery does not create unrelated delivery blockers', () => {
  const c = setup(),
    image = c.store.addEntity('image', null, { title: '旧造型', data: { missingFile: true } }),
    character = c.store.addEntity('character', null, {
      title: '阿林',
      data: {
        looks: [
          { id: 'unused', name: '旧造型', description: '', version: 1, gallery: { front: image } },
          { id: 'used', name: '当前造型', description: '', version: 1, gallery: {} },
        ],
      },
    });
  c.store.editProject((p) => {
    p.entities.find((e) => e.id === c.scene).data.cast = [
      { characterId: character, lookId: 'used' },
    ];
  });
  assert.equal(c.issues().filter((i) => i.id.startsWith('gallery-')).length, 0);
});

test('replacing a linked reference requires range reconfirmation even after candidate review', () => {
  const c = setup(),
    video = c.store.addEntity('video', null, { title: '转身参考', data: { fileId: 'old-motion' } });
  c.store.addLink(video, c.shot, 'motion');
  const link = c.get().links.find((l) => l.source === video);
  c.store.editProject((p) => {
    const s = p.entities.find((e) => e.id === c.shot);
    s.status = 'draft';
    s.data.referenceRanges = { [link.id]: { start: 1, end: 3, duration: 4, fileId: 'old-motion' } };
  });
  assert.ok(!c.issues().some((i) => i.id === 'range-' + link.id));
  c.store.updateEntity(video, { data: { fileId: 'new-motion' } });
  c.store.updateEntity(c.shot, { status: 'draft' });
  assert.ok(c.issues().some((i) => i.id === 'range-' + link.id && i.stage === 4));
  c.store.editProject((p) => {
    p.entities.find((e) => e.id === c.shot).data.referenceRanges[link.id] = {
      start: 0,
      end: 1,
      duration: 2,
      fileId: 'new-motion',
    };
  });
  assert.ok(!c.issues().some((i) => i.id === 'range-' + link.id));
});

test('deleted audio origin blocks delivery until the track is re-anchored or muted', () => {
  const c = setup();
  audioFixture(c);
  assert.deepEqual(c.issues(), []);
  c.store.deleteEntity(c.second);
  assert.ok(c.issues().some((i) => i.id === 'sound-origin-track-1' && i.stage === 6));
  c.store.editProject((p) => {
    p.journey.soundTracks[c.chapter][0].muted = true;
  });
  assert.ok(!c.issues().some((i) => i.id.startsWith('sound-')));
  c.store.editProject((p) => {
    const t = p.journey.soundTracks[c.chapter][0];
    t.muted = false;
    t.shotId = c.shot;
  });
  assert.ok(!c.issues().some((i) => i.id === 'sound-origin-track-1'));
});

test('audio replacement and shortened origin are detected independently; silent work preserves tracks', () => {
  const c = setup(),
    { audio } = audioFixture(c);
  c.store.updateEntity(audio, { data: { fileId: 'audio-replaced' } });
  assert.ok(c.issues().some((i) => i.id === 'sound-range-track-1'));
  c.store.editProject((p) => {
    const t = p.journey.soundTracks[c.chapter][0];
    t.fileId = 'audio-replaced';
    t.offset = 3;
  });
  c.store.updateEntity(c.second, { data: { seconds: 2 } });
  assert.ok(c.issues().some((i) => i.id === 'sound-origin-track-1'));
  c.store.editProject((p) => {
    p.journey.sound.mode = 'silent';
  });
  assert.ok(!c.issues().some((i) => i.id.startsWith('sound-')));
  assert.equal(c.get().journey.soundTracks[c.chapter].length, 1);
});

test('out-of-bounds saved ranges are flagged without pretending to decode media', () => {
  const c = setup(),
    { track } = audioFixture(c);
  c.store.editProject((p) => {
    p.journey.soundTracks[c.chapter][0] = { ...track, end: 7, duration: 5 };
  });
  assert.ok(c.issues().some((i) => i.id === 'sound-range-track-1'));
});
