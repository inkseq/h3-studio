import test from 'node:test';
import assert from 'node:assert/strict';
import { microsToDollars, dollarsToMicros, formatMoneyMicros } from './money.js';
test('exact accepted money round trips without floating point fractional arithmetic', () => {
  for (const n of [0, 1, 2345678, 8999999999999999, 9000000000000000])
    assert.equal(dollarsToMicros(microsToDollars(n)), n);
  assert.equal(microsToDollars(8999999999999999), '8999999999.999999');
  for (const v of ['-1', '0.0000001', '1e3', true, 1, null, '9000000000.000001'])
    assert.throws(() => dollarsToMicros(v));
});

test('display money rounds cents without changing exact accepted microdollars', () => {
  for (const [value, label] of [
    [0, '$0.00'],
    [1, '<$0.01'],
    [9999, '<$0.01'],
    [10000, '$0.01'],
    [14999, '$0.01'],
    [15000, '$0.02'],
    [198615783, '$198.62'],
    [199995000, '$200.00'],
    [9000000000000000, '$9000000000.00'],
    [-1, '>-$0.01'],
    [-15000, '-$0.02'],
  ])
    assert.equal(formatMoneyMicros(value), label);
  for (const value of [undefined, null, NaN, Infinity, '123', 0.1, 9000000000000001])
    assert.equal(formatMoneyMicros(value), '—');
  assert.equal(microsToDollars(198615783), '198.615783');
  assert.equal(dollarsToMicros('198.615783'), 198615783);
});
