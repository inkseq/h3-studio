import test from 'node:test';
import assert from 'node:assert/strict';
import { flowChecklist, needsBody, profileUsed, storyFlow } from './story-flow.js';
import { bibleOf } from './story-bible.js';

const scene = (id, beats = [], script = '') => ({
  id,
  title: id,
  type: 'scene',
  data: { beats, script },
});
const beat = { id: 'beat-1', kind: 'line', text: '走', speaker: 'c1' };
const plan = {
  purpose: '老大要追',
  beats: ['beat-1'],
  size: 'close',
  emotionFrom: '愣',
  emotionTo: '怒',
  timeline: [{ from: 0, to: 1, text: '皱眉' }],
  entry: '左',
  exit: '右',
};
const shot = (id, parentId, extra = {}) => ({
  id,
  title: 'Shot ' + id,
  prompt: '',
  cast: [],
  place: null,
  props: [],
  entity: { parentId, data: {} },
  start: null,
  chosen: '',
  ...extra,
});

test('an empty story starts with the script', () => {
  const flow = storyFlow({ scenes: [], profiles: [], shots: [] });
  assert.deepEqual(flow.next, { stage: 'script', key: 'writeScript' });
  assert.deepEqual(flow.stages.script, { done: 0, total: 0 });
});

test('the next step walks script → world → storyboard → takes → edit', () => {
  const state = {
    scenes: [scene('s1', [beat]), scene('s2')],
    profiles: [],
    shots: [shot('k1', 's1')],
  };
  let flow = storyFlow(state);
  // s1 has a beat but no shot films it yet: not done.
  assert.deepEqual(flow.stages.script, { done: 0, total: 2 });
  assert.deepEqual(flow.next, { stage: 'script', key: 'claimSceneBeats', id: 's1', title: 's1' });

  state.shots[0].entity.data.plan = { beats: ['beat-1'] };
  flow = storyFlow(state);
  assert.deepEqual(flow.stages.script, { done: 1, total: 2 });
  assert.equal(flow.next.key, 'splitBeats');
  assert.equal(flow.next.id, 's2');

  state.scenes[1] = scene('s2', [{ ...beat, id: 'beat-2' }]);
  state.shots.push(
    shot('k2', 's2', { entity: { parentId: 's2', data: { plan: { beats: ['beat-2'] } } } }),
  );
  flow = storyFlow(state);
  assert.deepEqual(flow.stages.script, { done: 2, total: 2 });
  assert.equal(flow.next.stage, 'storyboard');
  assert.equal(flow.next.key, 'planShot');

  state.shots = [shot('k1', 's1', { entity: { parentId: 's1', data: { plan } } })];
  state.scenes = [scene('s1', [beat])];
  flow = storyFlow(state);
  assert.deepEqual(flow.stages.storyboard, { done: 1, total: 1 });
  assert.deepEqual(flow.next, {
    stage: 'storyboard',
    key: 'chooseTake',
    id: 'k1',
    title: 'Shot k1',
  });

  state.shots[0].chosen = 'asset-1';
  assert.equal(storyFlow(state).next.key, 'reviewTakes');
  flow = storyFlow(state, { reviews: { k1: { verdict: 'keep' } } });
  assert.deepEqual(flow.next, { stage: 'edit', key: 'cutFilm' });
});

test('a scene whose script is in its shots asks for beats from the shots', () => {
  const flow = storyFlow({
    scenes: [scene('s1')],
    profiles: [],
    shots: [shot('k1', 's1', { prompt: '他喊道(S1) <d>走。</d>' })],
  });
  assert.deepEqual(flow.next, { stage: 'script', key: 'beatsFromShots', id: 's1', title: 's1' });
});

test('an empty beat is not a finished script', () => {
  const flow = storyFlow({
    scenes: [scene('s1', [{ id: 'beat-x', kind: 'action', text: '' }])],
    profiles: [],
    shots: [],
  });
  assert.deepEqual(flow.stages.script, { done: 0, total: 1 });
});

test('once takes are chosen the next step is review, not the script', () => {
  const shots = [
    shot('k1', 's1', { chosen: 'a1' }),
    shot('k2', 's1', { chosen: 'a2' }),
    shot('k3', 's1'),
  ];
  const state = { scenes: [scene('s1')], profiles: [], shots };
  let flow = storyFlow(state);
  assert.deepEqual(flow.next, {
    stage: 'storyboard',
    key: 'reviewTakes',
    id: 'k1',
    title: 'Shot k1',
    count: 2,
  });
  // With review data: reshoot first, then unreviewed takes, then missing takes, then cut.
  const reviews = new Map([
    ['k1', { verdict: 'keep' }],
    ['k2', { verdict: 'redo' }],
  ]);
  flow = storyFlow(state, { reviews });
  assert.deepEqual(flow.next, {
    stage: 'storyboard',
    key: 'reshoot',
    id: 'k2',
    title: 'Shot k2',
    count: 1,
  });
  reviews.set('k2', { verdict: null });
  assert.equal(storyFlow(state, { reviews }).next.key, 'reviewTakes');
  reviews.set('k2', { verdict: 'keep', reshoot: true });
  assert.equal(storyFlow(state, { reviews }).next.key, 'reshoot');
  reviews.set('k2', { verdict: 'keep' });
  assert.deepEqual(storyFlow(state, { reviews }).next.id, 'k3');
  shots[2].chosen = 'a3';
  reviews.set('k3', { verdict: 'keep' });
  assert.deepEqual(storyFlow(state, { reviews }).next, { stage: 'edit', key: 'cutFilm' });
  // Without review data nothing says the takes were looked at: still review.
  assert.equal(storyFlow(state).next.key, 'reviewTakes');
});

const character = (id, desc = '', data = {}) => ({
  id,
  type: 'character',
  name: id,
  desc,
  variants: [{ id: 'main', desc: 'coat', images: ['img'] }],
  voice: 'voice',
  bible: bibleOf({ data }),
});

test('an ordinary body needs no counts; a multi-body character does', () => {
  const plainMan = character('a', '三个独立的年轻男子，各有自己的身体');
  assert.equal(needsBody(plainMan), false);
  assert.equal(needsBody(character('b', '一个长着三个头的巨人')), true);
  assert.equal(needsBody(character('c', 'A conjoined pair')), true);
  assert.equal(needsBody(character('d', '连体双胞胎')), true);
  // A group whose other member has three heads marks the whole group.
  const member = character('e', '', { groupId: 'g1' }),
    giant = character('f', '', { groupId: 'g1', body: { heads: 3, arms: 6, legs: 6 } });
  assert.equal(needsBody(member, [member, giant]), true);
  assert.equal(needsBody({ ...plainMan, type: 'prop' }), false);
  const body = flowChecklist(plainMan).find((item) => item.key === 'body');
  assert.deepEqual([body.done, body.ordinary], [true, true]);
});

test('"used" counts from the story, not the selection', () => {
  const p = { id: 'loc', type: 'location' };
  assert.equal(profileUsed(p, [shot('k1', 's1')]), false);
  assert.equal(profileUsed(p, [shot('k1', 's1', { place: { id: 'loc' } })]), true);
  assert.equal(profileUsed({ id: 'c' }, [shot('k1', 's1', { cast: [{ id: 'c' }] })]), true);
  assert.equal(profileUsed({ id: 'pr' }, [shot('k1', 's1', { props: ['pr'] })]), true);
  const hero = character('hero');
  const used = (shots) =>
    flowChecklist(hero, { shots, profiles: [hero] }).find((item) => item.key === 'used').done;
  assert.equal(used([]), false);
  assert.equal(used([shot('k1', 's1', { cast: [{ id: 'hero' }] })]), true);
});
