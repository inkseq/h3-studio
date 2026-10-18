import test from 'node:test';
import assert from 'node:assert/strict';
import { freestyleProject, mergeJobCandidate } from './cloud-model.js';

function fixture() {
  const project = freestyleProject('Agent results'),
    shot = project.entities.find((e) => e.type === 'shot');
  const video = {
    id: 'agent-video',
    type: 'video',
    title: 'Keep my custom title',
    data: { cloudArtifactId: 'video-1', sourceJobId: 'job-1' },
  };
  const audio = {
    id: 'agent-sound',
    type: 'audio',
    data: { cloudArtifactId: 'audio-1', sourceJobId: 'job-1', mime: 'audio/flac' },
  };
  project.entities.push(video, audio);
  shot.data.selectedAssetId = video.id;
  const job = {
    id: 'job-1',
    status: 'succeeded',
    client_ref: { project_id: project.id, shot_id: shot.id, shot_version: shot.version },
    artifacts: [
      { id: 'video-1', kind: 'video', mime: 'video/mp4' },
      { id: 'audio-1', kind: 'audio', mime: 'audio/flac' },
    ],
  };
  return { project, shot, video, audio, job };
}
test('polling reuses API-adopted custom entity identities and preserves the selected result', () => {
  const { project, shot, video, job } = fixture(),
    before = structuredClone(video);
  const result = mergeJobCandidate(project, job);
  assert.equal(result.changed, true);
  assert.deepEqual(result.added, []);
  assert.equal(project.entities.filter((e) => e.data.cloudArtifactId === 'video-1').length, 1);
  assert.equal(project.entities.filter((e) => e.data.cloudArtifactId === 'audio-1').length, 1);
  assert.deepEqual(shot.data.candidateIds, ['agent-video']);
  assert.equal(shot.data.selectedAssetId, 'agent-video');
  assert.deepEqual(video, before);
  assert.deepEqual(project.jobs[0].clientImportedAudioIds, ['audio-1']);
  assert.equal(mergeJobCandidate(project, job).changed, false);
});
test('candidate removal and an independently selected take survive unchanged repeated job polling', () => {
  const { project, shot, job } = fixture();
  mergeJobCandidate(project, job);
  shot.data.candidateIds = [];
  shot.data.selectedAssetId = 'another-take';
  assert.equal(mergeJobCandidate(project, job).changed, false);
  assert.deepEqual(shot.data.candidateIds, []);
  assert.equal(shot.data.selectedAssetId, 'another-take');
});
test('an API-adopted candidate without selection never replaces the users previous take', () => {
  const { project, shot, job } = fixture();
  shot.data.selectedAssetId = 'previous-take';
  mergeJobCandidate(project, job);
  assert.equal(shot.data.selectedAssetId, 'previous-take');
  assert.deepEqual(shot.data.candidateIds, ['agent-video']);
});
test('foreign project results cannot attach to the quick project even when artifact identities match', () => {
  const { project, job } = fixture(),
    before = structuredClone(project);
  job.client_ref.project_id = 'other';
  assert.equal(mergeJobCandidate(project, job).changed, false);
  assert.deepEqual(project, before);
});
