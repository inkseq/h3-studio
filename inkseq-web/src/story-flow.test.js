import test from 'node:test';
import assert from 'node:assert/strict';
import { storyFlow } from './story-flow.js';

const scene = (id, beats = []) => ({ id, title: id, type: 'scene', data: { beats } });
const beat = { id: 'beat-1', kind: 'line', text: '走', speaker: 'c1' };

test('an empty story starts with the script', () => {
  const flow = storyFlow({ scenes: [], profiles: [], shots: [] });
  assert.deepEqual(flow.next, { stage: 'script', key: 'writeScript' });
  assert.deepEqual(flow.stages.script, { done: 0, total: 0 });
});

test('the next step walks script → world → storyboard → takes → edit', () => {
  const state = {
    scenes: [scene('s1', [beat]), scene('s2')],
    profiles: [],
    shots: [{ id: 'k1', title: 'Shot 1', entity: { data: {} }, start: null, chosen: '' }],
  };
  let flow = storyFlow(state);
  assert.deepEqual(flow.stages.script, { done: 1, total: 2 });
  assert.equal(flow.next.key, 'splitBeats');
  assert.equal(flow.next.id, 's2');

  state.scenes[1] = scene('s2', [beat]);
  flow = storyFlow(state);
  assert.equal(flow.next.stage, 'storyboard');
  assert.equal(flow.next.key, 'claimBeats');

  state.shots[0].entity.data.plan = {
    purpose: '老大要追',
    beats: ['beat-1'],
    size: 'close',
    emotionFrom: '愣',
    emotionTo: '怒',
    timeline: [{ from: 0, to: 1, text: '皱眉' }],
    entry: '左',
    exit: '右',
  };
  flow = storyFlow(state);
  assert.deepEqual(flow.stages.storyboard, { done: 1, total: 1 });
  assert.equal(flow.next.key, 'chooseTake');

  state.shots[0].chosen = 'asset-1';
  flow = storyFlow(state);
  assert.deepEqual(flow.next, { stage: 'edit', key: 'cutFilm' });
});
