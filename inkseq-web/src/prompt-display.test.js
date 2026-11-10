import test from 'node:test';
import assert from 'node:assert/strict';
import { characterAliases, inferSpeakers, promptParts } from './prompt-display.js';

test('the text splits into plain text and spoken lines', () => {
  assert.deepEqual(promptParts('他喊道(S1) <d>[Chinese] 今天吃牛。</d>，走了。'), [
    { kind: 'text', text: '他喊道' },
    { kind: 'line', code: 'S1', text: '今天吃牛。' },
    { kind: 'text', text: '，走了。' },
  ]);
  assert.deepEqual(promptParts('<d>Hi.</d>'), [{ kind: 'line', code: '', text: 'Hi.' }]);
});

test('speaker numbers are tied to a character only when every sentence agrees', () => {
  const characters = [
    { id: 'sons', name: '儿子（三兄弟）' },
    { id: 'mum', name: '阿妈' },
  ];
  assert.deepEqual(characterAliases('儿子（三兄弟）'), ['儿子（三兄弟）', '儿子', '三兄弟']);
  const ids = inferSpeakers(
    [
      '三兄弟赶着牛，左边老大喊道(S1) <d>今天吃牛。</d>，老二护道(S2) <d>那是阿花。</d>',
      '矮小的阿妈拽住老二，颤抖着说道(S4) <d>别去。</d>',
      '阿妈看着三兄弟(S3) <d>走吧。</d>',
    ],
    characters,
  );
  // S2's sentence names nobody; S3's names two.
  assert.deepEqual(
    [...ids],
    [
      ['S1', 'sons'],
      ['S4', 'mum'],
    ],
  );
  const clash = inferSpeakers(['阿妈说(S1) <d>a</d>', '三兄弟说(S1) <d>b</d>'], characters);
  assert.equal(clash.size, 0);
});
