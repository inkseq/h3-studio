import test from 'node:test';
import assert from 'node:assert/strict';
import {
  alignVideoRange,
  applyVideoRange,
  boundVideoRange,
  videoBinding,
  videoCut,
} from './video-cut-model.js';
import { createStore, validateProject } from './store.js';
import { captionSignature, captionStatus } from './caption-model.js';
import { roughCutSnapshot } from './roughcut-model.js';
import { copyForCloud, stableJSON } from './cloud-model.js';
import { chapterHandover, shotListCsv } from './handover-model.js';
import { exportBundle, readBundle } from './media.js';

function fixture() {
  const data = new Map(),
    store = createStore({
      storage: { getItem: (k) => data.get(k) || null, setItem: (k, v) => data.set(k, v) },
    });
  store.newProject('剪辑测试');
  const chapter = store.addEntity('chapter'),
    scene = store.addEntity('scene', chapter),
    shot = store.addEntity('shot', scene, { data: { seconds: 4 } }),
    video = store.addEntity('video', scene, {
      data: {
        fileId: 'cloud_artifact_fixture',
        cloudArtifactId: 'fixture',
        metadata: { duration: 10 },
        mime: 'video/mp4',
      },
    });
  store.updateEntity(shot, {
    data: { selectedAssetId: video, candidateIds: [video], uxReview: { note: '原返修说明' } },
  });
  const asset = store.exportProject().entities.find((e) => e.id === video);
  return {
    store,
    chapter,
    scene,
    shot,
    video,
    range: { ...videoBinding(asset), start: 2, end: 8 },
  };
}
test('24fps trimming rounds inwards, preserves exact boundaries and never includes a frame outside the selected interval', () => {
  const a = alignVideoRange(0.01, 5.99);
  assert.equal(a.startFrame, 1);
  assert.equal(a.endFrame, 143);
  assert.equal(a.frames, 142);
  assert.ok(a.start >= 0.01);
  assert.ok(a.end <= 5.99);
  for (const frame of [1, 7, 11, 25, 143, 777]) {
    const r = alignVideoRange(frame / 24, (frame + 24) / 24);
    assert.equal(r.startFrame, frame);
    assert.equal(r.frames, 24);
  }
  assert.equal(alignVideoRange(-1, 2), null);
  assert.equal(alignVideoRange(1, 1), null);
  assert.ok(alignVideoRange(0.01, 0.02).frames < 1);
});
test('saving an in/out range keeps plan duration unless explicitly requested, keeps source files and is one undo step', () => {
  const h = fixture(),
    before = h.store.exportProject(),
    count = h.store.getState().undoCount;
  assert.equal(
    h.store.editProject((p) => applyVideoRange(p, h.shot, h.range, { duration: 10 }) && undefined),
    true,
  );
  let p = h.store.exportProject(),
    shot = p.entities.find((e) => e.id === h.shot),
    asset = p.entities.find((e) => e.id === h.video),
    cut = videoCut(shot, asset);
  assert.equal(shot.data.seconds, 4);
  assert.equal(cut.sourceStart, 2);
  assert.equal(cut.sourceEnd, 6);
  assert.equal(cut.unusedTailFrames, 48);
  assert.equal(h.store.getState().undoCount, count + 1);
  assert.deepEqual(
    asset,
    before.entities.find((e) => e.id === h.video),
  );
  assert.equal(shot.data.uxReview.note, '原返修说明');
  h.store.undo();
  assert.deepEqual(h.store.exportProject(), before);
  h.store.editProject((p) => {
    applyVideoRange(p, h.shot, h.range, { duration: 10, useRangeDuration: true });
  });
  p = h.store.exportProject();
  assert.equal(p.entities.find((e) => e.id === h.shot).data.seconds, 6);
  h.store.undo();
  assert.deepEqual(h.store.exportProject(), before);
});
test('short, zero-frame, out-of-file and changed-identity ranges fail atomically without replacing the chosen video', () => {
  const h = fixture(),
    before = h.store.exportProject();
  for (const range of [
    { ...h.range, end: 4 },
    { ...h.range, start: 0.01, end: 0.02 },
    { ...h.range, end: 11 },
    { ...h.range, fileId: 'changed' },
    { ...h.range, cloudArtifactId: 'another' },
  ]) {
    assert.equal(
      h.store.editProject((p) => {
        applyVideoRange(p, h.shot, range, { duration: 10 });
      }),
      false,
    );
    assert.deepEqual(h.store.exportProject(), before);
  }
  const p = h.store.exportProject(),
    shot = p.entities.find((e) => e.id === h.shot),
    asset = p.entities.find((e) => e.id === h.video);
  assert.equal(videoCut(shot, asset).sourceStart, 0);
  assert.equal(videoCut(shot, asset).sourceEnd, 4);
});
test('source changes leave a repairable draft and explicit clear, delete/undo and duplicate keep reference meanings', () => {
  const h = fixture();
  h.store.editProject((p) => {
    applyVideoRange(p, h.shot, h.range, { duration: 10 });
  });
  const original = h.store.exportProject();
  h.store.updateEntity(h.video, { data: { fileId: 'replacement' } });
  let p = h.store.exportProject(),
    shot = p.entities.find((e) => e.id === h.shot),
    asset = p.entities.find((e) => e.id === h.video);
  assert.match(videoCut(shot, asset).issue, /已改变/);
  assert.equal(validateProject(p).ok, true);
  assert.deepEqual(shot.data.selectedVideoRange, h.range);
  h.store.undo();
  h.store.deleteEntity(h.video);
  assert.equal(
    h.store.exportProject().entities.find((e) => e.id === h.shot).data.selectedVideoRange.assetId,
    h.video,
  );
  assert.equal(validateProject(h.store.exportProject()).ok, true);
  h.store.undo();
  assert.deepEqual(h.store.exportProject(), original);
  const copied = h.store.duplicateEntity(h.scene);
  p = h.store.exportProject();
  shot = p.entities.find((e) => e.parentId === copied && e.type === 'shot');
  asset = p.entities.find((e) => e.id === shot.data.selectedAssetId);
  assert.notEqual(asset.id, h.video);
  assert.equal(boundVideoRange(shot.data.selectedVideoRange, asset), true);
});
test('range edits invalidate render and subtitle confirmations while preserving cue text; cloud copy rebinding is explicit', () => {
  const h = fixture();
  h.store.editProject((p) => {
    p.journey = {
      captionTracks: {
        [h.chapter]: { cues: [{ id: 'cue', start: 0, end: 1, text: '不要丢掉的字幕稿' }] },
      },
    };
    p.journey.captionTracks[h.chapter].confirmedSnapshot = captionSignature(p, h.chapter);
  });
  const before = h.store.exportProject(),
    signature = stableJSON(roughCutSnapshot(before, h.chapter));
  assert.equal(captionStatus(before, h.chapter).confirmed, true);
  h.store.editProject((p) => {
    applyVideoRange(p, h.shot, h.range, { duration: 10 });
  });
  const p = h.store.exportProject();
  assert.equal(captionStatus(p, h.chapter).confirmed, false);
  assert.deepEqual(
    p.journey.captionTracks[h.chapter].cues,
    before.journey.captionTracks[h.chapter].cues,
  );
  assert.notEqual(stableJSON(roughCutSnapshot(p, h.chapter)), signature);
  const copied = copyForCloud(p),
    shot = copied.entities.find((e) => e.id === h.shot),
    asset = copied.entities.find((e) => e.id === h.video);
  assert.equal(boundVideoRange(shot.data.selectedVideoRange, asset), true);
  assert.equal(shot.data.selectedVideoRange.cloudArtifactId, null);
  assert.equal(shot.data.selectedVideoRange.fileId, 'cloud_artifact_fixture');
  h.store.undo();
  assert.equal(captionStatus(h.store.exportProject(), h.chapter).confirmed, true);
});
test('handover includes actual in/out without paths and a complete media backup remaps selected source bindings', async () => {
  const h = fixture();
  h.store.editProject((p) => {
    applyVideoRange(p, h.shot, h.range, { duration: 10 });
  });
  const p = h.store.exportProject(),
    handover = chapterHandover(p, h.chapter);
  assert.equal(handover.shots[0].source_in, 2);
  assert.equal(handover.shots[0].source_out, 6);
  assert.equal(handover.shots[0].selected_range.end, 8);
  assert.match(shotListCsv(handover), /实际源入点秒/);
  assert.equal(JSON.stringify(handover).includes('cloud_artifact_fixture'), false);
  const { blob } = await exportBundle(p, { readFile: () => new Blob(['video fixture']) }),
    restored = await readBundle(new File([blob], 'fixture.zip')),
    shot = restored.project.entities.find((e) => e.id === h.shot),
    asset = restored.project.entities.find((e) => e.id === h.video);
  assert.equal(boundVideoRange(shot.data.selectedVideoRange, asset), true);
  assert.notEqual(asset.data.fileId, h.range.fileId);
  assert.equal(shot.data.selectedVideoRange.start, 2);
});

test('using a short edit range preserves inherited or explicitly chosen generation duration', () => {
  const h = fixture();
  h.store.editProject((p) => {
    applyVideoRange(
      p,
      h.shot,
      { ...h.range, start: 1, end: 3 },
      { duration: 10, useRangeDuration: true },
    );
  });
  let shot = h.store.exportProject().entities.find((e) => e.id === h.shot);
  assert.equal(shot.data.seconds, 2);
  assert.equal(shot.data.h3.controls.duration, 4);
  h.store.updateEntity(h.shot, { data: { h3: { controls: { duration: 8 } } } });
  h.store.editProject((p) => {
    applyVideoRange(
      p,
      h.shot,
      { ...h.range, start: 1, end: 2 },
      { duration: 10, useRangeDuration: true },
    );
  });
  shot = h.store.exportProject().entities.find((e) => e.id === h.shot);
  assert.equal(shot.data.seconds, 1);
  assert.equal(shot.data.h3.controls.duration, 8);
});
