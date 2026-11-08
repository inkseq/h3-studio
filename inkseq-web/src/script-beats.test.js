import test from 'node:test';
import assert from 'node:assert/strict';
import {
  beatClaims,
  beatSeconds,
  beatsSeconds,
  beatsText,
  parseScript,
  sceneBeats,
  validBeats,
} from './script-beats.js';
import { applyStoryboardAction } from './storyboard-actions.js';

const characters = [
  { id: 'c1', name: '老大', bible: { speaker: 'S1' } },
  { id: 'c2', name: '老二', bible: { speaker: 'S2' } },
];

test('free text splits into action and line beats by rule only', () => {
  const beats = parseScript(
    [
      '三兄弟肩并肩赶着牛回圈。',
      '',
      '老二：（声音发紧）少了一头……阿花呢？',
      '老大: 追！把它牵回来。（压着火）',
      '(S2) <d>[Chinese] 要不……先问问？</d>',
      '路人：你好',
    ].join('\n'),
    characters,
  );
  assert.deepEqual(
    beats.map((b) => [b.kind, b.speaker, b.text, b.delivery]),
    [
      ['action', '', '三兄弟肩并肩赶着牛回圈。', ''],
      ['line', 'c2', '少了一头……阿花呢？', '声音发紧'],
      ['line', 'c1', '追！把它牵回来。', '压着火'],
      ['line', 'c2', '要不……先问问？', ''],
      // An unknown name stays an action: nothing is guessed.
      ['action', '', '路人：你好', ''],
    ],
  );
  assert.ok(beats.every((b) => /^beat-/.test(b.id)));
  assert.equal(beatsText(beats.slice(1, 2), characters), '老二：（声音发紧）少了一头……阿花呢？');
});

test('seconds follow the script defaults', () => {
  assert.equal(beatSeconds({ kind: 'action', text: 'x' }), 2.5);
  assert.equal(beatSeconds({ kind: 'line', text: '追！把它牵回来。' }), 1.8);
  assert.equal(
    beatsSeconds([
      { kind: 'action', text: '' },
      { kind: 'line', text: '追！把它牵回来。' },
    ]),
    4.3,
  );
});

const story = () => ({
  entities: [
    { id: 'c1', type: 'character', title: '老大', version: 1, data: {} },
    { id: 'sc', type: 'scene', title: '场 1', version: 1, data: {} },
    { id: 's1', type: 'shot', parentId: 'sc', version: 1, status: 'ready', data: {} },
    { id: 's2', type: 'shot', parentId: 'sc', version: 1, status: 'ready', data: {} },
  ],
  links: [],
});
const beat = (id, extra = {}) => ({
  id,
  kind: 'action',
  text: 'x',
  speaker: '',
  delivery: '',
  ...extra,
});

test('beats validate speakers and shapes', () => {
  const project = story();
  assert.equal(validBeats(project, [beat('beat-a')]), true);
  assert.equal(validBeats(project, [beat('beat-a'), beat('beat-a')]), false);
  assert.equal(validBeats(project, [beat('beat-a', { kind: 'line', speaker: 'c1' })]), true);
  assert.equal(validBeats(project, [beat('beat-a', { kind: 'line', speaker: 'nobody' })]), false);
  assert.equal(validBeats(project, [beat('beat-a', { speaker: 'c1' })]), false);
  assert.equal(validBeats(project, [{ ...beat('beat-a'), extra: 1 }]), false);
});

test('a beat is claimed by one shot; removing it drops the claim', () => {
  const project = story();
  const find = (id) => project.entities.find((e) => e.id === id);
  assert.ok(
    applyStoryboardAction(project, {
      type: 'sceneBeats',
      id: 'sc',
      beats: [beat('beat-a'), beat('beat-b')],
    }),
  );
  applyStoryboardAction(project, { type: 'claimBeat', scene: 'sc', beat: 'beat-b', shot: 's1' });
  applyStoryboardAction(project, { type: 'claimBeat', scene: 'sc', beat: 'beat-a', shot: 's1' });
  // Claimed beats stay in scene order.
  assert.deepEqual(find('s1').data.plan.beats, ['beat-a', 'beat-b']);
  assert.equal(find('s1').status, 'review');
  applyStoryboardAction(project, { type: 'claimBeat', scene: 'sc', beat: 'beat-a', shot: 's2' });
  assert.deepEqual(find('s1').data.plan.beats, ['beat-b']);
  assert.deepEqual(find('s2').data.plan.beats, ['beat-a']);
  const claims = beatClaims(
    ['s1', 's2'].map((id) => ({ id, entity: find(id) })),
    sceneBeats(find('sc')),
  );
  assert.deepEqual(
    [...claims.entries()],
    [
      ['beat-a', ['s2']],
      ['beat-b', ['s1']],
    ],
  );
  applyStoryboardAction(project, { type: 'sceneBeats', id: 'sc', beats: [beat('beat-a')] });
  assert.deepEqual(find('s1').data.plan.beats, []);
  assert.equal(
    applyStoryboardAction(project, { type: 'claimBeat', scene: 'sc', beat: 'beat-x', shot: 's1' }),
    false,
  );
  assert.ok(applyStoryboardAction(project, { type: 'sceneIntent', id: 'sc', intent: '先问问' }));
  assert.equal(find('sc').data.intent, '先问问');
});
