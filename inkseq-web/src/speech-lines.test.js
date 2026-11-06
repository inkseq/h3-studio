import test from 'node:test';
import assert from 'node:assert/strict';
import { readableLines } from './speech-lines.js';

test('speech tags read as quoted lines; other text is unchanged', () => {
  assert.equal(
    readableLines(
      '左头呵斥 (S1) <d>[Chinese] 今天吃牛。</d>，中头回应 (S2) <d>[Chinese] 那是阿花。</d>',
    ),
    '左头呵斥 「今天吃牛。」，中头回应 「那是阿花。」',
  );
  assert.equal(readableLines('She says (S1) <d>[English] Hi there.</d>'), 'She says “Hi there.”');
  assert.equal(readableLines('(S3)<d>走吧</d>'), '「走吧」');
  assert.equal(readableLines('No lines here.'), 'No lines here.');
  assert.equal(readableLines(undefined), '');
});
