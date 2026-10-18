import test from 'node:test';
import assert from 'node:assert/strict';
import { blankCloudProject, stableJSON } from './cloud-model.js';
import { captionSignature, burnCaptionStatus, exportSrt } from './caption-model.js';
import { roughCutSnapshot } from './roughcut-model.js';

function fixture() {
  const p = blankCloudProject('字幕烧录独立测试');
  p.entities = [
    { id: 'c', type: 'chapter', title: '章', parentId: null, order: 0, data: {} },
    { id: 's', type: 'scene', title: '场', parentId: 'c', order: 0, data: {} },
    {
      id: 'shot',
      type: 'shot',
      title: '镜',
      parentId: 's',
      order: 0,
      data: { seconds: 4, selectedAssetId: 'v' },
    },
    {
      id: 'v',
      type: 'video',
      title: '片',
      parentId: null,
      order: 0,
      data: { fileId: 'f', metadata: { duration: 4 } },
    },
  ];
  p.journey = {
    sound: { mode: 'silent' },
    soundTracks: { c: [] },
    captionTracks: {
      c: { cues: [{ id: 'cue', start: 0.01, end: 1.99, text: '你好，明天。\n再见，昨天。' }] },
    },
  };
  p.journey.captionTracks.c.confirmedSnapshot = captionSignature(p, 'c');
  return p;
}

test('burn-in requires current human confirmation and aligns inward to 24fps without changing the SRT draft', () => {
  const p = fixture(),
    before = structuredClone(p),
    status = burnCaptionStatus(p, 'c');
  assert.deepEqual(status.issues, []);
  assert.equal(status.aligned[0].start_frame, 1);
  assert.equal(status.aligned[0].end_frame, 47);
  assert.equal(status.aligned[0].start, 1 / 24);
  assert.equal(status.aligned[0].end, 47 / 24);
  assert.deepEqual(p, before);
  assert.match(exportSrt(p, 'c'), /00:00:00,010 --> 00:00:01,990/);
  p.entities.find((e) => e.id === 'shot').data.seconds = 3;
  assert.ok(burnCaptionStatus(p, 'c').issues.some((t) => t.includes('确认')));
  assert.equal(p.journey.captionTracks.c.cues[0].text, before.journey.captionTracks.c.cues[0].text);
});

test('burn preset blocks oversized, unsafe or subframe text while leaving independently valid SRT available', () => {
  for (const text of [
    '字'.repeat(19),
    '一\n二\n三',
    '一\n',
    '你好{世界}',
    '路径\\new',
    '前\t后',
    '前\u200d后',
    '前\u2028后',
    '前\u2029后',
  ]) {
    const p = fixture();
    p.journey.captionTracks.c.cues[0].text = text;
    p.journey.captionTracks.c.confirmedSnapshot = captionSignature(p, 'c');
    assert.ok(burnCaptionStatus(p, 'c').issues.length, text);
    assert.equal(p.journey.captionTracks.c.cues[0].text, text);
    if (!text.includes('\t')) assert.ok(exportSrt(p, 'c').length);
  }
  const p = fixture();
  p.journey.captionTracks.c.cues[0] = { id: 'cue', start: 0.01, end: 0.03, text: '太短' };
  p.journey.captionTracks.c.confirmedSnapshot = captionSignature(p, 'c');
  assert.ok(burnCaptionStatus(p, 'c').issues.some((t) => t.includes('不足1帧')));
  assert.match(exportSrt(p, 'c'), /太短/);
});

test('burn text uses Unicode codepoint count and CRLF equivalence without removing raw draft characters', () => {
  const p = fixture();
  p.journey.captionTracks.c.cues[0].text = '😀'.repeat(18) + '\r\n中文';
  p.journey.captionTracks.c.confirmedSnapshot = captionSignature(p, 'c');
  assert.deepEqual(burnCaptionStatus(p, 'c').issues, []);
  assert.equal(burnCaptionStatus(p, 'c').aligned[0].text, p.journey.captionTracks.c.cues[0].text);
  p.journey.captionTracks.c.cues[0].text += '\r孤立';
  p.journey.captionTracks.c.confirmedSnapshot = captionSignature(p, 'c');
  assert.ok(burnCaptionStatus(p, 'c').issues.some((t) => t.includes('控制字符')));
});

test('only subtitle-enabled roughcut snapshots depend on caption text and confirmation; silent output still supports captions', () => {
  const p = fixture(),
    off = stableJSON(roughCutSnapshot(p, 'c', { resolution: '720P', burn_subtitles: false })),
    old = stableJSON(roughCutSnapshot(p, 'c', { resolution: '720P' })),
    on = stableJSON(roughCutSnapshot(p, 'c', { resolution: '720P', burn_subtitles: true }));
  p.journey.captionTracks.c.cues[0].text = '新的手动字幕';
  assert.equal(
    stableJSON(roughCutSnapshot(p, 'c', { resolution: '720P', burn_subtitles: false })),
    off,
  );
  assert.equal(stableJSON(roughCutSnapshot(p, 'c', { resolution: '720P' })), old);
  assert.notEqual(
    stableJSON(roughCutSnapshot(p, 'c', { resolution: '720P', burn_subtitles: true })),
    on,
  );
  assert.ok(burnCaptionStatus(p, 'c').issues.length);
  p.journey.captionTracks.c.confirmedSnapshot = captionSignature(p, 'c');
  assert.deepEqual(burnCaptionStatus(p, 'c').issues, []);
  assert.deepEqual(roughCutSnapshot(p, 'c', { burn_subtitles: true }).tracks, []);
});
