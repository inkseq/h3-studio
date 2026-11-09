import test from 'node:test';
import assert from 'node:assert/strict';
import {
  addCue,
  captionLines,
  confirmCaptions,
  cuesFromLines,
  fillCuesFromLines,
  removeCue,
  setCue,
  splitCaptionText,
} from './caption-edit.js';
import { captionProblems, captionStatus, captionTrack } from './caption-model.js';
import { blankCloudProject } from './cloud-model.js';

// One chapter, one 4 s shot with a video, and a caption whose first line is too long to burn in.
function burnProject() {
  const p = blankCloudProject('确认字幕');
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
    captionTracks: { c: { cues: [{ id: 'cue', start: 0, end: 2, text: '字'.repeat(20) }] } },
  };
  return p;
}

const project = () => ({
  entities: [
    {
      id: 'scene',
      type: 'scene',
      data: {
        beats: [
          { id: 'beat-a', kind: 'line', text: '你去哪', speaker: 'c1' },
          { id: 'beat-b', kind: 'action', text: '他转身' },
          { id: 'beat-c', kind: 'line', text: '我去追那个偷东西的人', speaker: 'c2' },
        ],
      },
    },
    { id: 's1', type: 'shot', parentId: 'scene', data: { plan: { beats: ['beat-a', 'beat-b'] } } },
    { id: 's2', type: 'shot', parentId: 'scene', data: { plan: { beats: ['beat-c'] } } },
  ],
  journey: {},
});

test('cues come from the lines each clip films, spread over that clip', () => {
  const p = project();
  const cues = cuesFromLines(
    p,
    [
      { id: 's1', from: 0, frames: 48 },
      { id: 's2', from: 48, frames: 96 },
    ],
    24,
  );
  assert.deepEqual(
    cues.map(({ start, end, text }) => [start, end, text]),
    [
      [0, 2, '你去哪'],
      [2, 6, '我去追那个偷东西的人'],
    ],
  );
  fillCuesFromLines(p, 'ch', [{ id: 's1', from: 0, frames: 48 }], 24);
  assert.equal(captionTrack(p, 'ch').cues.length, 1);
  assert.throws(() => fillCuesFromLines(p, 'ch', [], 24), /editor.noLines/);
});

test('cues can be added at the playhead, edited inside the cut and removed', () => {
  const p = project();
  const id = addCue(p, 'ch', 9.5, 10);
  let cue = captionTrack(p, 'ch').cues[0];
  assert.deepEqual([cue.start, cue.end, cue.text], [9.5, 10, '']);
  setCue(p, 'ch', id, { text: '好', start: -1, end: 0.1 }, 10);
  cue = captionTrack(p, 'ch').cues[0];
  assert.deepEqual([cue.start, cue.end, cue.text], [0, 0.3, '好']);
  assert.throws(() => setCue(p, 'ch', 'nope', { text: 'x' }, 10), /editor.cueMissing/);
  removeCue(p, 'ch', id);
  assert.deepEqual(captionTrack(p, 'ch').cues, []);
});

test('long script lines become cues of at most 2 x 18 characters, broken at punctuation', () => {
  assert.deepEqual(splitCaptionText('别去。'), ['别去。']);
  assert.deepEqual(splitCaptionText('别去，那个人杀过狮子，他一拳就能打死一头牛。'), [
    '别去，那个人杀过狮子，\n他一拳就能打死一头牛。',
  ]);
  const long =
    '我们三个今天一定要把阿花找回来，不然妈妈会伤心的，可是那个巨人实在太可怕了，谁也打不过他。';
  const parts = splitCaptionText(long);
  assert.ok(parts.length > 1);
  assert.equal(parts.join('').replace(/\n/g, ''), long);
  for (const part of parts) {
    const lines = part.split('\n');
    assert.ok(lines.length <= 2, part);
    for (const line of lines) assert.ok(Array.from(line).length <= 18, line);
  }
  // No break point at all: even pieces, never more than 18.
  assert.deepEqual(splitCaptionText('一'.repeat(20)), ['一'.repeat(10) + '\n' + '一'.repeat(10)]);
  assert.deepEqual(splitCaptionText('I will find the cow and bring it home'), [
    'I will find the\ncow and bring it',
    'home',
  ]);
});

test('a long line is split into cues timed by their share of the line; speech tags count as lines', () => {
  const p = project();
  p.entities.push(
    { id: 'scene2', type: 'scene', data: {} },
    {
      id: 's3',
      type: 'shot',
      parentId: 'scene2',
      description:
        '阿妈说(S4) <d>[Chinese] 别去。那个人杀过狮子，一拳就打死过一头牛，你们三个加起来也打不过他。</d>',
      data: {},
    },
  );
  const cues = cuesFromLines(p, [{ id: 's3', from: 0, frames: 240 }], 24);
  assert.ok(cues.length >= 2);
  assert.equal(cues[0].start, 0);
  assert.equal(cues.at(-1).end, 10);
  for (let i = 1; i < cues.length; i++) assert.equal(cues[i].start, cues[i - 1].end);
  for (const cue of cues)
    assert.ok(
      captionLines(cue.text).every((line) => line.ok),
      cue.text,
    );
});

test('captions are confirmed only without problems, and any change asks again', () => {
  const p = burnProject();
  assert.throws(() => confirmCaptions(p, 'c'), /editor.captionsHaveProblems/);
  assert.equal(captionProblems(p, 'c')[0].cueId, 'cue');
  setCue(p, 'c', 'cue', { text: '你好。' }, 4);
  assert.deepEqual(captionProblems(p, 'c'), []);
  assert.equal(captionStatus(p, 'c').confirmed, false);
  confirmCaptions(p, 'c');
  assert.equal(captionStatus(p, 'c').confirmed, true);
  setCue(p, 'c', 'cue', { text: '你好！' }, 4);
  const status = captionStatus(p, 'c');
  assert.equal(status.confirmed, false);
  assert.equal(status.previouslyConfirmed, true);
});

test('the line counter flags a third line, an empty line and a line over 18', () => {
  assert.deepEqual(
    captionLines('一二三\n' + '字'.repeat(19)).map(({ length, ok }) => [length, ok]),
    [
      [3, true],
      [19, false],
    ],
  );
  assert.deepEqual(
    captionLines('a\n\nb').map((line) => line.ok),
    [true, false, false],
  );
});
