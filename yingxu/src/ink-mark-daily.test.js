import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DAILY_INK_MARKS, dailyInkMark } from './ink-mark-daily.js';

test('every daily mark exists in InkMark and each appears once', () => {
  const source = readFileSync(new URL('./InkMark.jsx', import.meta.url), 'utf8');
  assert.equal(new Set(DAILY_INK_MARKS).size, DAILY_INK_MARKS.length);
  for (const name of DAILY_INK_MARKS) assert.match(source, new RegExp('\n  ' + name + ': '));
});

test('same mark all day, next mark the next day, full cycle repeats', () => {
  const morning = new Date(2026, 9, 21, 0, 5),
    night = new Date(2026, 9, 21, 23, 55),
    tomorrow = new Date(2026, 9, 22, 9, 0);
  assert.equal(dailyInkMark(morning), dailyInkMark(night));
  assert.notEqual(dailyInkMark(morning), dailyInkMark(tomorrow));
  const later = new Date(2026, 9, 21 + DAILY_INK_MARKS.length, 12);
  assert.equal(dailyInkMark(later), dailyInkMark(morning));
});
