import test from 'node:test';
import assert from 'node:assert/strict';
import {
  beatClaims,
  beatSeconds,
  beatsSeconds,
  beatsFromShots,
  beatsText,
  insertBeat,
  moveBeat,
  parseScript,
  sceneBeats,
  splitBlocked,
  splitTagged,
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

test('English lines are timed by words, Chinese by characters, mixed by both', () => {
  // 6 words at 2.7 a second.
  assert.equal(beatSeconds({ kind: 'line', text: 'Get it back, we need it.' }), 2.2);
  assert.equal(beatSeconds({ kind: 'line', text: '' }), 0);
  // 4 characters + 1 word.
  assert.equal(beatSeconds({ kind: 'line', text: '我要 OK 了吗' }), 1.3);
});

test('text in the video model form splits into action, lines and delivery', () => {
  const beats = splitTagged(
    '黄昏逆光，三兄弟赶着红牛前行，左边老大粗暴瞪牛喊道(S1) <d>[Chinese] 今天吃牛。</d>，中间老二弱声护道(S2) <d>[Chinese] 那是阿花。</d>，老三闭嘴不语，无对白。',
    new Map([
      ['S1', 'c1'],
      ['S2', 'c2'],
    ]),
  );
  assert.deepEqual(
    beats.map((b) => [b.kind, b.speaker, b.delivery, b.text]),
    [
      ['action', '', '', '黄昏逆光，三兄弟赶着红牛前行'],
      ['line', 'c1', '左边老大粗暴瞪牛喊道', '今天吃牛。'],
      ['line', 'c2', '中间老二弱声护道', '那是阿花。'],
      ['action', '', '', '老三闭嘴不语。'],
    ],
  );
  // A clause that does not end in a speaking word stays action.
  assert.deepEqual(
    splitTagged('他推门进来(S1) <d>走。</d>').map((b) => [b.kind, b.delivery, b.text]),
    [
      ['action', '', '他推门进来'],
      ['line', '', '走。'],
    ],
  );
});

test('a scene written in its shots becomes beats in shot order, each from its own shot', () => {
  const characters = [
    { id: 'sons', name: '儿子（三兄弟）', bible: {} },
    { id: 'mum', name: '阿妈', bible: { speaker: 'S4' } },
  ];
  const { beats, claims } = beatsFromShots(
    [
      { id: 's1', prompt: '三兄弟赶着牛，左边老大喊道(S1) <d>[Chinese] 今天吃牛。</d>' },
      { id: 's2', prompt: '俯拍脚印延伸向海滩，无对白。' },
      { id: 's3', prompt: '她颤抖着说道(S4) <d>[Chinese] 别去。</d>，老二不语。' },
      { id: 's4', prompt: '' },
    ],
    characters,
  );
  assert.deepEqual(
    beats.map((b) => [b.kind, b.speaker, b.text]),
    [
      ['action', '', '三兄弟赶着牛'],
      ['line', 'sons', '今天吃牛。'],
      ['action', '', '俯拍脚印延伸向海滩。'],
      ['line', 'mum', '别去。'],
      ['action', '', '老二不语。'],
    ],
  );
  assert.deepEqual(
    claims.map(([, shot]) => shot),
    ['s1', 's1', 's2', 's3', 's3'],
  );
  assert.deepEqual(
    claims.map(([beat]) => beat),
    beats.map((b) => b.id),
  );
  // The beats pass the store's own check and every claim lands on its shot.
  const project = {
    entities: [
      { id: 'sons', type: 'character', title: '儿子（三兄弟）', version: 1, data: {} },
      { id: 'mum', type: 'character', title: '阿妈', version: 1, data: {} },
      { id: 'sc', type: 'scene', title: '场 1', version: 1, data: {} },
      ...['s1', 's2', 's3', 's4'].map((id) => ({
        id,
        type: 'shot',
        parentId: 'sc',
        version: 1,
        status: 'ready',
        data: {},
      })),
    ],
    links: [],
  };
  assert.ok(applyStoryboardAction(project, { type: 'sceneBeats', id: 'sc', beats }));
  for (const [beat, shot] of claims)
    assert.ok(applyStoryboardAction(project, { type: 'claimBeat', scene: 'sc', beat, shot }));
  const shotOf = (id) => project.entities.find((e) => e.id === id);
  assert.equal(shotOf('s1').data.plan.beats.length, 2);
  assert.equal(shotOf('s3').data.plan.beats.length, 2);
  assert.equal(shotOf('s4').data.plan, undefined);
});

test('splitting says why it cannot run', () => {
  assert.equal(splitBlocked({ script: '他走了。', shots: [] }), '');
  assert.equal(splitBlocked({ script: ' ', shots: [{ prompt: '他走了。' }] }), 'inShots');
  assert.equal(splitBlocked({ script: '', shots: [{ prompt: '' }] }), 'empty');
});

test('beats move one place and new ones go right below', () => {
  const beats = [beat('beat-a'), beat('beat-b'), beat('beat-c')];
  const ids = (list) => list.map((b) => b.id);
  assert.deepEqual(ids(moveBeat(beats, 'beat-b', -1)), ['beat-b', 'beat-a', 'beat-c']);
  assert.deepEqual(ids(moveBeat(beats, 'beat-b', 1)), ['beat-a', 'beat-c', 'beat-b']);
  assert.equal(moveBeat(beats, 'beat-a', -1), null);
  assert.equal(moveBeat(beats, 'beat-c', 1), null);
  assert.equal(moveBeat(beats, 'beat-x', 1), null);
  const made = insertBeat(beats, 'beat-a', 'line');
  assert.deepEqual(ids(made.beats), ['beat-a', made.beat.id, 'beat-b', 'beat-c']);
  assert.equal(made.beat.kind, 'line');
  assert.equal(insertBeat(beats, null, 'action').beats.at(-1).kind, 'action');
  assert.equal(validBeats(story(), made.beats), true);
});
