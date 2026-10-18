import test from 'node:test';
import assert from 'node:assert/strict';
import { createStore, validateProject } from './store.js';
import { mergeJobCandidate, copyForCloud, stableJSON } from './cloud-model.js';
import {
  generatedAudioOption,
  generatedTrackState,
  upsertGeneratedSound,
  unlinkGeneratedSound,
} from './generated-sound-model.js';
import { videoBinding, applyVideoRange } from './video-cut-model.js';
import { chapterTiming, trackPlacement, trackAtTime } from './media-timing.js';
import { roughCutSnapshot } from './roughcut-model.js';
import { captionSignature, captionStatus } from './caption-model.js';
import { chapterHandover } from './handover-model.js';

function fixture() {
  const data = new Map(),
    store = createStore({
      storage: { getItem: (k) => data.get(k) || null, setItem: (k, v) => data.set(k, v) },
    });
  store.newProject('生成音轨测试');
  const chapter = store.addEntity('chapter'),
    scene = store.addEntity('scene', chapter),
    shot = store.addEntity('shot', scene, { data: { seconds: 4 } });
  const job = {
    id: 'job-a',
    status: 'succeeded',
    simulation: true,
    recipe_id: 'h3-base-fl2va-v1',
    client_ref: { project_id: store.getState().project.id, shot_id: shot, shot_version: 1 },
    artifacts: [
      {
        id: 'video-a',
        kind: 'video',
        mime: 'video/mp4',
        metadata: { duration: 6 },
        content_url: '/v1/artifacts/video-a/content',
      },
      {
        id: 'audio-a',
        kind: 'audio',
        mime: 'audio/flac',
        metadata: { duration: 6, sample_rate: 32000 },
        content_url: '/v1/artifacts/audio-a/content',
      },
    ],
  };
  store.editProject((p) => {
    mergeJobCandidate(p, job);
    p.entities.find((e) => e.id === shot).data.selectedAssetId = 'result-video-a';
    p.journey = {
      sound: { mode: 'silent' },
      soundTracks: {
        [chapter]: [
          {
            id: 'manual',
            assetId: 'manual-music',
            fileId: 'manual-file',
            shotId: '',
            role: 'music',
            offset: 0,
            start: 0,
            end: 2,
            gain: 0.4,
            muted: false,
          },
        ],
      },
    };
  });
  assert.ok(store.exportProject().jobs.length, store.getState().notice);
  return { store, chapter, scene, shot, job };
}
test('same succeeded historical job backfills FLAC once outside candidates, preserves manual deletion and never imports roughcut outputs', () => {
  const h = fixture(),
    p = h.store.exportProject(),
    video = p.entities.find((e) => e.id === 'result-video-a');
  p.entities = p.entities.filter((e) => e.type !== 'audio');
  delete p.jobs[0].clientImportedAudioIds;
  const r = mergeJobCandidate(p, h.job);
  assert.deepEqual(r.added, ['result-audio-a']);
  assert.deepEqual(p.entities.find((e) => e.id === h.shot).data.candidateIds, ['result-video-a']);
  assert.deepEqual(
    p.entities.find((e) => e.id === video.id),
    video,
  );
  assert.equal(mergeJobCandidate(p, h.job).changed, false);
  p.entities = p.entities.filter((e) => e.id !== 'result-audio-a');
  assert.equal(mergeJobCandidate(p, h.job).changed, false);
  assert.equal(
    p.entities.some((e) => e.id === 'result-audio-a'),
    false,
  );
  mergeJobCandidate(p, {
    ...h.job,
    id: 'cut',
    recipe_id: 'chapter-roughcut-v1',
    artifacts: [{ id: 'rough-audio', kind: 'audio', mime: 'audio/flac' }],
  });
  assert.equal(
    p.entities.some((e) => e.data.cloudArtifactId === 'rough-audio'),
    false,
  );
});
test('one explicit action adds or replaces a single same-shot track and mixed mode, retaining manual tracks and undoing together', () => {
  const h = fixture(),
    before = h.store.exportProject();
  assert.equal(generatedAudioOption(before, h.shot).issue, '');
  h.store.editProject((p) => {
    upsertGeneratedSound(p, h.shot);
  });
  let p = h.store.exportProject(),
    tracks = p.journey.soundTracks[h.chapter];
  assert.equal(tracks.length, 2);
  assert.equal(p.journey.sound.mode, 'mixed');
  assert.deepEqual(tracks[0], before.journey.soundTracks[h.chapter][0]);
  const id = tracks[1].id;
  assert.deepEqual(tracks[1].generatedFrom, {
    jobId: 'job-a',
    videoEntityId: 'result-video-a',
    videoArtifactId: 'video-a',
    audioArtifactId: 'audio-a',
  });
  assert.equal(tracks[1].gain, 0.7);
  assert.equal(tracks[1].muted, false);
  assert.equal(tracks[1].offset, 0);
  h.store.editProject((p) => {
    upsertGeneratedSound(p, h.shot);
  });
  assert.equal(h.store.exportProject().journey.soundTracks[h.chapter].length, 2);
  assert.equal(h.store.exportProject().journey.soundTracks[h.chapter][1].id, id);
  h.store.undo();
  assert.deepEqual(h.store.exportProject(), before);
});
test('same source trim/duration automatically retimes sound and invalidates caption/roughcut confirmations without replacing the files', () => {
  const h = fixture();
  h.store.editProject((p) => {
    upsertGeneratedSound(p, h.shot);
    p.journey.captionTracks = {
      [h.chapter]: { cues: [{ id: 'cue', start: 0, end: 1, text: '保留对白' }] },
    };
    p.journey.captionTracks[h.chapter].confirmedSnapshot = captionSignature(p, h.chapter);
  });
  const before = h.store.exportProject(),
    snapshot = stableJSON(roughCutSnapshot(before, h.chapter));
  assert.equal(captionStatus(before, h.chapter).confirmed, true);
  h.store.editProject((p) => {
    applyVideoRange(
      p,
      h.shot,
      {
        ...videoBinding(p.entities.find((e) => e.id === 'result-video-a')),
        start: 1.01,
        end: 5.99,
      },
      { duration: 6, useRangeDuration: true },
    );
  });
  const p = h.store.exportProject(),
    t = p.journey.soundTracks[h.chapter][1];
  assert.equal(t.start, 25 / 24);
  assert.equal(t.end, 143 / 24);
  assert.equal(t.needsReview, false);
  assert.equal(t.fileId, before.journey.soundTracks[h.chapter][1].fileId);
  assert.equal(captionStatus(p, h.chapter).confirmed, false);
  assert.equal(p.journey.captionTracks[h.chapter].cues[0].text, '保留对白');
  assert.notEqual(stableJSON(roughCutSnapshot(p, h.chapter)), snapshot);
  const handover = chapterHandover(p, h.chapter);
  assert.equal(handover.audio_tracks[1].source_start, 25 / 24);
  assert.deepEqual(handover.audio_tracks[1].generated_from, t.generatedFrom);
  h.store.undo();
  assert.deepEqual(h.store.exportProject(), before);
});
test('replacing or deleting adopted video keeps old sound as needs-review; explicit new selection upserts and unlink preserves manual timing', () => {
  const h = fixture();
  h.store.editProject((p) => {
    upsertGeneratedSound(p, h.shot);
  });
  const original = h.store.exportProject(),
    second = {
      ...h.job,
      id: 'job-b',
      artifacts: [
        { ...h.job.artifacts[0], id: 'video-b' },
        { ...h.job.artifacts[1], id: 'audio-b' },
      ],
    };
  h.store.editProject((p) => {
    mergeJobCandidate(p, second);
    p.entities.find((e) => e.id === h.shot).data.selectedAssetId = 'result-video-b';
  });
  let p = h.store.exportProject(),
    t = p.journey.soundTracks[h.chapter][1];
  assert.equal(t.needsReview, true);
  assert.match(generatedTrackState(p, t).issue, /更换/);
  assert.equal(validateProject(p).ok, true);
  h.store.editProject((p) => {
    upsertGeneratedSound(p, h.shot);
  });
  p = h.store.exportProject();
  t = p.journey.soundTracks[h.chapter][1];
  assert.equal(p.journey.soundTracks[h.chapter].length, 2);
  assert.equal(t.generatedFrom.jobId, 'job-b');
  assert.equal(t.needsReview, false);
  const id = t.id;
  h.store.deleteEntity('result-video-b');
  p = h.store.exportProject();
  assert.equal(p.journey.soundTracks[h.chapter][1].needsReview, true);
  h.store.undo();
  p = h.store.exportProject();
  assert.equal(p.journey.soundTracks[h.chapter][1].needsReview, false);
  h.store.editProject((p) => {
    unlinkGeneratedSound(p, h.chapter, id);
  });
  p = h.store.exportProject();
  t = p.journey.soundTracks[h.chapter][1];
  assert.equal(t.generatedFrom, undefined);
  assert.equal(t.fileId, 'cloud_artifact_audio-b');
  assert.equal(t.start, 0);
  assert.equal(t.end, 4);
  assert.equal(t.role, 'dialogue');
  h.store.undo();
  assert.equal(
    h.store.exportProject().journey.soundTracks[h.chapter][1].generatedFrom.jobId,
    'job-b',
  );
  assert.equal(
    original.entities.find((e) => e.id === 'result-video-a').data.fileId,
    'cloud_artifact_video-a',
  );
});
test('missing or ambiguous FLAC, short audio, malformed binding and project copies cannot silently claim same-job audio', () => {
  const h = fixture(),
    p = h.store.exportProject();
  for (const mutate of [
    (q) => (q.entities = q.entities.filter((e) => e.type !== 'audio')),
    (q) => (q.entities.find((e) => e.type === 'audio').data.metadata.duration = 2),
    (q) => (q.entities.find((e) => e.type === 'audio').data.sourceJobId = 'another'),
    (q) =>
      q.entities.push({
        ...structuredClone(q.entities.find((e) => e.type === 'audio')),
        id: 'duplicate',
      }),
  ]) {
    const q = structuredClone(p);
    mutate(q);
    assert.ok(generatedAudioOption(q, h.shot).issue);
    assert.throws(() => upsertGeneratedSound(q, h.shot));
  }
  upsertGeneratedSound(p, h.shot);
  const copied = copyForCloud(p);
  assert.equal(copied.journey.soundTracks[h.chapter][1].needsReview, true);
  assert.ok(generatedAudioOption(copied, h.shot).issue);
  assert.ok(generatedTrackState(copied, copied.journey.soundTracks[h.chapter][1]).issue);
  p.journey.soundTracks[h.chapter][1].generatedFrom.secret = 'bad';
  assert.equal(validateProject(p).ok, false);
});
test('muting invalid linked sound permits roughcut and silent mode preserves all tracks without pretending to repair them', () => {
  const h = fixture();
  h.store.editProject((p) => {
    upsertGeneratedSound(p, h.shot);
    p.entities.find((e) => e.id === h.shot).data.selectedAssetId = '';
  });
  let p = h.store.exportProject();
  assert.ok(roughCutSnapshot(p, h.chapter).tracks.find((t) => t.generatedFrom).generatedIssue);
  h.store.editProject((p) => {
    p.journey.soundTracks[h.chapter][1].muted = true;
  });
  p = h.store.exportProject();
  assert.equal(
    roughCutSnapshot(p, h.chapter).tracks.some((t) => t.generatedFrom),
    false,
  );
  assert.equal(p.journey.soundTracks[h.chapter][1].needsReview, true);
  p.journey.sound.mode = 'silent';
  assert.deepEqual(roughCutSnapshot(p, h.chapter).tracks, []);
  assert.equal(p.journey.soundTracks[h.chapter].length, 2);
});
test('chapter timing accumulates rounded 24fps frames and generated track source seeks follow reordering without floating drift', () => {
  const h = fixture();
  h.store.editProject((p) => {
    upsertGeneratedSound(p, h.shot);
  });
  const p = h.store.exportProject(),
    shot = p.entities.find((e) => e.id === h.shot),
    track = p.journey.soundTracks[h.chapter][1];
  const previous = { id: 'previous', title: 'before', data: { seconds: 1.01 } },
    timeline = chapterTiming([previous, shot]);
  assert.equal(timeline[1].start, 24 / 24);
  assert.equal(trackPlacement(track, timeline).start, 1);
  assert.equal(trackAtTime(track, timeline, 1.5), 0.5);
  assert.equal(trackAtTime(track, chapterTiming([shot, previous]), 0.5), 0.5);
  const many = chapterTiming(
    Array.from({ length: 999 }, (_, i) => ({ id: String(i), data: { seconds: 0.07 } })),
  );
  assert.equal(many.at(-1).end, 1998 / 24);
  assert.equal(many[998].start, 1996 / 24);
});

test('unlinked manual sound is not silently duplicated by adding the same generated FLAC again', () => {
  const h = fixture();
  h.store.editProject((p) => {
    const t = upsertGeneratedSound(p, h.shot);
    unlinkGeneratedSound(p, h.chapter, t.id);
  });
  const before = h.store.exportProject();
  assert.match(generatedAudioOption(before, h.shot).issue, /手动音轨/);
  assert.throws(() => upsertGeneratedSound(before, h.shot), /重复叠音/);
  assert.equal(before.journey.soundTracks[h.chapter].length, 2);
  h.store.editProject((p) => {
    p.journey.soundTracks[h.chapter][1].muted = true;
    upsertGeneratedSound(p, h.shot);
  });
  const p = h.store.exportProject();
  assert.equal(p.journey.soundTracks[h.chapter].length, 3);
  assert.equal(p.journey.soundTracks[h.chapter][1].muted, true);
  assert.equal(p.journey.soundTracks[h.chapter][2].generatedFrom.jobId, 'job-a');
  h.store.undo();
  assert.deepEqual(h.store.exportProject(), before);
});
