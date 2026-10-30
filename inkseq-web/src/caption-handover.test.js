import test from 'node:test';
import assert from 'node:assert/strict';
import { createStore, validateProject } from './store.js';
import {
  captionStatus,
  captionSignature,
  captionIssues,
  exportSrt,
  srtTime,
} from './caption-model.js';
import { chapterHandover, shotListCsv, csvCell } from './handover-model.js';
const storage = () => {
  const data = new Map();
  return {
    getItem: (k) => data.get(k) || null,
    setItem: (k, v) => data.set(k, v),
    removeItem: (k) => data.delete(k),
  };
};
function fixture() {
  const store = createStore({ storage: storage() });
  store.newProject('交接测试');
  const c = store.addEntity('chapter', null, { title: '第一章' }),
    scene = store.addEntity('scene', c, { title: '门口' }),
    a = store.addEntity('shot', scene, { title: 'A', data: { seconds: 6 } }),
    b = store.addEntity('shot', scene, { title: 'B', data: { seconds: 6 } }),
    video = store.addEntity('video', null, {
      title: '候选',
      data: {
        fileId: 'file-a',
        fileName: 'C:\\private\\clip.mp4',
        cloudArtifactId: 'artifact-a',
        cloudContentPath: 'https://example.test/?signature=do-not-export',
        metadata: { duration: 6, private_url: 'never-export' },
        simulation: true,
      },
    });
  store.updateEntity(a, { data: { selectedAssetId: video } });
  store.editProject((p) => {
    p.journey = {
      captionTracks: {
        [c]: {
          cues: [
            { id: 'c2', start: 7, end: 9, text: '收件人是谁？' },
            { id: 'c1', start: 0.125, end: 2.5, text: '你好\nHello' },
          ],
        },
      },
      soundTracks: { [c]: [] },
    };
  });
  return { store, c, scene, a, b, video };
}
test('SRT requires explicit confirmation, sorts cues and preserves Unicode/line breaks with millisecond timecodes', () => {
  const { store, c } = fixture(),
    p = store.exportProject();
  assert.equal(captionStatus(p, c).confirmed, false);
  assert.throws(() => exportSrt(p, c), /确认/);
  p.journey.captionTracks[c].confirmedSnapshot = captionSignature(p, c);
  const text = exportSrt(p, c);
  assert.equal(
    text,
    '1\r\n00:00:00,125 --> 00:00:02,500\r\n你好\r\nHello\r\n\r\n2\r\n00:00:07,000 --> 00:00:09,000\r\n收件人是谁？\r\n',
  );
  assert.equal(srtTime(59.9996), '00:01:00,000');
  assert.equal(validateProject(p).ok, true);
  const parsed = JSON.parse(JSON.stringify(p));
  const previous = parsed.journey.captionTracks[c].confirmedSnapshot;
  parsed.journey.captionTracks[c].confirmedSnapshot = {
    cues: previous.cues,
    audio: previous.audio,
    soundMode: previous.soundMode,
    basis: previous.basis,
  };
  assert.equal(captionStatus(parsed, c).confirmed, true);
});
test('caption constraints prevent empty, overlapping, reversed, out-of-chapter, markup and oversized cues', () => {
  const cue = { id: 'x', start: 0, end: 1, text: '你好' };
  for (const change of [
    { text: '' },
    { text: '<b>hello</b>' },
    { text: 'a'.repeat(2001) },
    { start: 1 },
    { end: 20 },
    { start: NaN },
    { end: 0.00001 },
  ])
    assert.ok(captionIssues([{ ...cue, ...change }], 12).length);
  assert.ok(
    captionIssues([cue, { ...cue, id: 'y', start: 0.5, end: 2 }], 12).some((e) =>
      e.includes('重叠'),
    ),
  );
  assert.deepEqual(captionIssues([cue, { ...cue, id: 'y', start: 1, end: 2 }], 12), []);
  assert.ok(
    captionIssues(
      Array.from({ length: 501 }, (_, i) => ({ ...cue, id: String(i) })),
      12,
    ).length,
  );
});
test('caption confirmation becomes stale on retime, reorder or source replacement, and undo restores draft with chapter', () => {
  const { store, c, a, b, video } = fixture();
  store.editProject((p) => {
    p.journey.captionTracks[c].confirmedSnapshot = captionSignature(p, c);
  });
  const confirmed = store.exportProject();
  assert.ok(captionStatus(confirmed, c).confirmed);
  const withAudio = structuredClone(confirmed);
  withAudio.journey.soundTracks[c] = [
    { id: 'audio-track', assetId: 'missing', start: 0, end: 2, offset: 1 },
  ];
  assert.equal(captionStatus(withAudio, c).confirmed, false);
  store.updateEntity(a, { data: { seconds: 7 } });
  assert.equal(captionStatus(store.exportProject(), c).confirmed, false);
  store.undo();
  assert.ok(captionStatus(store.exportProject(), c).confirmed);
  store.reorderEntity(b, -1);
  assert.equal(captionStatus(store.exportProject(), c).confirmed, false);
  store.undo();
  store.updateEntity(video, { data: { fileId: 'replaced' } });
  assert.equal(captionStatus(store.exportProject(), c).confirmed, false);
  store.undo();
  store.deleteEntity(c);
  assert.equal(store.exportProject().journey.captionTracks[c], undefined);
  store.undo();
  assert.deepEqual(
    store.exportProject().journey.captionTracks[c],
    confirmed.journey.captionTracks[c],
  );
  assert.ok(captionStatus(store.exportProject(), c).confirmed);
});
test('chapter handover is scoped, keeps review/selection/sound/reference evidence without signed URLs or local paths', () => {
  const { store, c, a, video } = fixture(),
    other = store.addEntity('chapter', null, { title: '不得导出的另一章' }),
    s = store.addEntity('scene', other),
    shot = store.addEntity('shot', s),
    ref = store.addEntity('image', null, {
      title: '人物参考',
      data: { fileId: 'local-image', fileName: '/private/source.png' },
    });
  store.addLink(ref, a, 'reference');
  store.updateEntity(a, { data: { uxReview: { note: '只改转身速度', issues: ['动作与表演'] } } });
  const p = store.exportProject(),
    before = structuredClone(p),
    m = chapterHandover(p, c);
  assert.equal(m.shots.length, 2);
  assert.equal(m.shots[0].selected_asset_id, video);
  assert.equal(m.shots[0].review_note, '只改转身速度');
  assert.equal(m.shots[0].selected_simulation, true);
  assert.ok(m.assets.some((asset) => asset.id === ref));
  assert.equal(m.assets.find((asset) => asset.id === video).file_name, 'clip.mp4');
  for (const forbidden of [
    'signature',
    'do-not-export',
    'C:\\private',
    '/private',
    'private_url',
    shot,
  ])
    assert.equal(JSON.stringify(m).includes(forbidden), false);
  assert.deepEqual(p, before);
});
test('CSV quotes commas, newlines and quotes and renders formula-looking source text as text', () => {
  for (const value of [
    '=SUM(A1:A2)',
    '+cmd',
    '-1',
    '@data',
    '\tanything',
    '\ranything',
    '\nanything',
    '  =1',
  ])
    assert.ok(csvCell(value).startsWith('"\''));
  assert.equal(csvCell('台词,"你好"\n下一行'), '"台词,""你好""\r\n下一行"');
  const { store, c, a } = fixture();
  store.updateEntity(a, { title: '=HYPERLINK("x")', data: { prompt: '雨,灯\n"回头"' } });
  const csv = shotListCsv(chapterHandover(store.exportProject(), c));
  assert.ok(csv.startsWith('\ufeff"章"'));
  assert.ok(csv.includes('"\'=HYPERLINK(""x"")"'));
  assert.ok(csv.includes('"雨,灯\r\n""回头"""'));
});
test('local demo selections remain visibly fictional in the handover', () => {
  const { store, c, b } = fixture();
  store.updateEntity(b, { data: { uxReview: { chosen: 'A' } } });
  const row = chapterHandover(store.exportProject(), c, { cloud: false }).shots[1];
  assert.equal(row.selection_type, 'demo');
  assert.equal(row.selected_asset_id, null);
  assert.equal(row.selected_simulation, true);
  assert.ok(row.selected_asset_title.includes('非实际素材'));
});
