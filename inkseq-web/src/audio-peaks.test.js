import test from 'node:test';
import assert from 'node:assert/strict';
import { peaksOf, peaksPath } from './audio-peaks.js';

test('peaks are the loudest sample per slice across channels (#232 19–20)', () => {
  const left = Float32Array.from([0, 0.5, -0.25, 0, 0, -1, 0.1, 0]),
    right = Float32Array.from([0.75, 0, 0, 0, 0, 0, 0, 0.2]);
  // 8 samples at 4 Hz = 2 s; 1 peak per second → 2 slices of 4 samples.
  assert.deepEqual(peaksOf([left, right], 4, 1), [0.75, 1]);
  assert.deepEqual(peaksOf([], 4), []);
});

test('the path covers only the part of the file a block plays', () => {
  const peaks = [0, 1, 0.5, 0];
  // 4 s file, block plays 1–3 s → peaks 1 and 0.5, two bars across the box.
  assert.equal(peaksPath(peaks, 4, 1, 3), 'M25.00 0.00V100.00M75.00 25.00V75.00');
  assert.equal(peaksPath(peaks, 4, 3, 1), '');
  assert.equal(peaksPath(null, 4, 0, 1), '');
});
