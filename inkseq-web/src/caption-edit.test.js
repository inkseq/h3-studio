import test from 'node:test';
import assert from 'node:assert/strict';
import { addCue, cuesFromLines, fillCuesFromLines, removeCue, setCue } from './caption-edit.js';
import { captionTrack } from './caption-model.js';

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
