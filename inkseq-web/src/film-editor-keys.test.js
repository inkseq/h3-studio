import test from 'node:test';
import assert from 'node:assert/strict';
import {
  clipName,
  cutPoints,
  markRange,
  shuttle,
  sourceTimeAt,
  stepToCut,
  transportCommand,
} from './film-editor-keys.js';

// A minimal element: `closest` matches when any of the selector's parts names one of `is`.
const el = (...is) => ({
  closest: (selector) =>
    is.some((part) => selector.split(',').some((s) => s.trim().startsWith(part))) ? {} : null,
});
const key = (k, extra = {}) => ({ key: k, code: '', target: el(), ...extra });

test('transport keys map like Kdenlive/Shotcut and stay out of fields and dialogs', () => {
  assert.equal(transportCommand(key(' ')), 'toggle');
  assert.equal(transportCommand(key('j')), 'reverse');
  assert.equal(transportCommand(key('K')), 'pause');
  assert.equal(transportCommand(key('l')), 'forward');
  assert.equal(transportCommand(key('ArrowLeft')), 'backFrame');
  assert.equal(transportCommand(key('ArrowRight', { shiftKey: true })), 'forward1s');
  assert.equal(transportCommand(key('ArrowUp')), 'previousCut');
  assert.equal(transportCommand(key('ArrowDown')), 'nextCut');
  assert.equal(transportCommand(key('Home')), 'start');
  assert.equal(transportCommand(key('End')), 'end');
  assert.equal(transportCommand(key('i')), 'markIn');
  assert.equal(transportCommand(key('o')), 'markOut');
  assert.equal(transportCommand(key('?')), 'help');
  // Typing, sliders, dialogs, other pages and modified keys are left alone.
  assert.equal(transportCommand(key(' ', { target: el('textarea') })), null);
  assert.equal(transportCommand(key('ArrowLeft', { target: el('[role="slider"]') })), null);
  assert.equal(transportCommand(key('?', { target: el('dialog') })), null);
  assert.equal(transportCommand(key(' '), { inEditor: false }), null);
  assert.equal(transportCommand(key('z', { ctrlKey: true })), null);
  assert.equal(transportCommand(key('ArrowLeft', { altKey: true })), null);
  assert.equal(transportCommand(key(' ', { defaultPrevented: true })), null);
  // A plain button keeps Space and arrows; a timeline item gives them to the transport.
  assert.equal(transportCommand(key(' ', { target: el('button') })), null);
  assert.equal(transportCommand(key('ArrowRight', { target: el('button') })), null);
  assert.equal(transportCommand(key('j', { target: el('button') })), 'reverse');
  assert.equal(
    transportCommand(key('ArrowRight', { target: el('[data-timeline-item]', '[role="button"]') })),
    'forwardFrame',
  );
});

test('J and L speed up in their direction and turn round at normal speed', () => {
  assert.equal(shuttle(1, false, 1), 1);
  assert.equal(shuttle(1, true, 1), 2);
  assert.equal(shuttle(2, true, 1), 4);
  assert.equal(shuttle(4, true, 1), 4);
  assert.equal(shuttle(2, true, -1), -1);
  assert.equal(shuttle(-1, true, -1), -2);
});

test('up and down step between cuts and stop at the ends', () => {
  const clips = [
    { from: 0, frames: 48 },
    { from: 48, frames: 24 },
  ];
  const points = cutPoints(clips);
  assert.deepEqual(points, [0, 48, 72]);
  assert.equal(stepToCut(10, points, 1), 48);
  assert.equal(stepToCut(48, points, 1), 72);
  assert.equal(stepToCut(72, points, 1), 72);
  assert.equal(stepToCut(50, points, -1), 48);
  assert.equal(stepToCut(48, points, -1), 0);
  assert.equal(stepToCut(0, points, -1), 0);
});

test('I and O trim the selected clip at the playhead, through its speed', () => {
  const clip = {
    included: true,
    from: 24,
    frames: 48,
    startFrame: 12,
    sourceFrames: 48,
    rate: 1,
  };
  assert.equal(sourceTimeAt(clip, 24, 24), 0.5);
  assert.equal(sourceTimeAt(clip, 23, 24), null);
  assert.deepEqual(markRange(clip, 36, 24, 'in'), { start: 1, end: 2.5 });
  assert.deepEqual(markRange(clip, 36, 24, 'out'), { start: 0.5, end: 1 + 1 / 24 });
  assert.deepEqual(markRange(clip, 80, 24, 'in'), { error: 'editor.playheadOff' });
  assert.deepEqual(markRange({ ...clip, parts: [{}] }, 36, 24, 'in'), {
    error: 'editor.keptPartsKey',
  });
  const fast = { ...clip, rate: 2, sourceFrames: 96 };
  assert.equal(sourceTimeAt(fast, 36, 24), (12 + 24) / 24);
});

test('a clip without a title is named by its first script line, without speech tags', () => {
  assert.equal(clipName({ title: '开场' }), '开场');
  assert.equal(
    clipName({ title: '', description: '老大喊道(S1) <d>[Chinese] 今天吃牛。</d>\n第二行' }),
    '老大喊道「今天吃牛。」',
  );
  assert.equal(clipName({ title: '', description: '' }), '');
  assert.equal(Array.from(clipName({ description: '长'.repeat(80) })).length, 40);
});
