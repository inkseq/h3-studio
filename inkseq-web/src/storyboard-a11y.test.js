import test from 'node:test';
import assert from 'node:assert/strict';
import {
  assetLabel,
  defaultAssetName,
  dragAnnouncements,
  dragItemName,
  inOutRange,
  shotName,
  timeBarKey,
} from './storyboard-a11y.js';
import { translate } from './i18n/locale.js';

const zh = (key, values) => translate('zh', key, values);
const en = (key, values) => translate('en', key, values);

test('an empty shot title reads "Shot N", never empty quotes', () => {
  assert.equal(shotName({ title: '  ' }, 4, zh), '镜头 5');
  assert.equal(shotName({ title: '' }, 0, en), 'Shot 1');
  assert.equal(shotName({ title: '海滩' }, 2, zh), '海滩');
});

test('library tiles name generated files by shot, take and length', () => {
  const take = {
    kind: 'video',
    name: 'Generated result',
    seconds: 6,
    use: { shot: 5, take: 2, role: 'take' },
  };
  assert.equal(assetLabel(take, zh, { full: true }), '镜头 5 · 第 2 条 · 6.0 秒');
  assert.equal(assetLabel(take, en), 'Shot 5 · Take 2');
  const line = {
    kind: 'audio',
    name: '台词音轨 · 镜头1',
    seconds: 6.1,
    use: { shot: 1, role: 'dialogue' },
  };
  assert.equal(assetLabel(line, zh, { full: true }), '镜头 1 · 台词 · 6.1 秒');
  // A name the person typed stays; an unknown length adds nothing (no bare "s").
  const typed = { kind: 'video', name: '我的片段', use: { shot: 1, take: 1, role: 'take' } };
  assert.equal(assetLabel(typed, zh, { full: true }), '我的片段');
  assert.equal(assetLabel({ kind: 'video', name: 'Generated result' }, zh), '生成的视频');
  assert.ok(defaultAssetName('生成结果'));
  assert.ok(!defaultAssetName('阿妈'));
});

test('drag announcements are in the reader language and never say an internal id', () => {
  const story = {
    shots: [
      { id: 'shot-9f3', title: '' },
      { id: 'shot-a71', title: '海滩' },
    ],
    assets: [
      {
        id: 'asset-1',
        kind: 'video',
        name: 'Generated result',
        seconds: 6,
        use: { shot: 2, take: 1, role: 'take' },
      },
    ],
    profiles: [{ id: 'char-1', name: '阿妈', variants: [] }],
    scenes: [{ id: 'scene-1', title: '海边' }],
    sceneName: (scene) => scene.title,
  };
  const say = dragAnnouncements(zh, (id) => dragItemName(id, story, zh));
  const lines = [
    say.onDragStart({ active: { id: 'shot:shot-9f3' } }),
    say.onDragOver({ active: { id: 'shot:shot-9f3' }, over: { id: 'shot:shot-a71' } }),
    say.onDragEnd({ active: { id: 'asset:asset-1' }, over: { id: 'slot:start:shot-a71' } }),
    say.onDragOver({ active: { id: 'profile:char-1' }, over: { id: 'sceneto:scene-1' } }),
    say.onDragCancel({ active: { id: 'profile:char-1' }, over: null }),
  ];
  assert.deepEqual(lines, [
    '已拿起镜头 1。',
    '镜头 1在镜头 2「海滩」上方。',
    '镜头 2 · 第 1 条已放到镜头 2「海滩」的首帧。',
    '阿妈在空场景海边上方。',
    '已取消移动阿妈。',
  ]);
  for (const line of lines) assert.doesNotMatch(line, /shot-|asset-|char-|scene-/);
  assert.equal(
    say.onDragOver({ active: { id: 'shot:shot-9f3' }, over: { id: 'shot:shot-9f3' } }),
    undefined,
  );
  assert.equal(
    say.onDragEnd({ active: { id: 'shot:shot-9f3' }, over: { id: 'shot:shot-9f3' } }),
    '镜头 1回到了原来的位置。',
  );
  const english = dragAnnouncements(en, (id) => dragItemName(id, story, en));
  assert.equal(english.onDragStart({ active: { id: 'shot:shot-a71' } }), 'Picked up shot 2, 海滩.');
  assert.equal(english.onDragStart({ active: { id: 'unknown:x' } }), 'Picked up the item.');
});

test('review keys: I/O, Enter, 1–4, frame steps and J/K/L', () => {
  const key = (k, extra = {}) => timeBarKey({ key: k, ...extra });
  assert.deepEqual(key('i'), { type: 'in' });
  assert.deepEqual(key('O'), { type: 'out' });
  assert.deepEqual(key('Enter'), { type: 'enter' });
  assert.deepEqual(key('3'), { type: 'choice', index: 2 });
  assert.equal(key('5'), null);
  assert.equal(key('ArrowRight').by, 1 / 24);
  assert.equal(key('ArrowLeft', { shiftKey: true }).by, -1);
  assert.deepEqual(key('k'), { type: 'toggle' });
  assert.deepEqual(key('l'), { type: 'play' });
  assert.equal(key('j').by, -1);
  assert.equal(key('i', { ctrlKey: true }), null);
  assert.deepEqual(inOutRange(4, 2, 9), { start: 2, end: 4 });
  assert.deepEqual(inOutRange(1, null, 3), { start: 1, end: 3 });
  assert.equal(inOutRange(null, 2, 3), null);
});
