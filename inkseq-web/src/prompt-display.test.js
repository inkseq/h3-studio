import test from 'node:test';
import assert from 'node:assert/strict';
import {
  characterAliases,
  hasModelLines,
  inferSpeakers,
  promptParts,
  readablePrompt,
  speakerNames,
} from './prompt-display.js';

const profiles = [
  { id: 'c1', type: 'character', name: '老大', bible: { speaker: 'S1' } },
  { id: 'c2', type: 'character', name: '阿妈', bible: { speaker: 's4' } },
  { id: 'p1', type: 'prop', name: '面包', bible: { speaker: 'S9' } },
];

test('the text splits into plain text and spoken lines', () => {
  assert.deepEqual(promptParts('他喊道(S1) <d>[Chinese] 今天吃牛。</d>，走了。'), [
    { kind: 'text', text: '他喊道' },
    { kind: 'line', code: 'S1', text: '今天吃牛。' },
    { kind: 'text', text: '，走了。' },
  ]);
  assert.deepEqual(promptParts('<d>Hi.</d>'), [{ kind: 'line', code: '', text: 'Hi.' }]);
  assert.equal(hasModelLines('no lines'), false);
  assert.equal(hasModelLines('(S1) <d>x</d>'), true);
});

test('names come from the speaker map only, for characters', () => {
  const names = speakerNames(profiles);
  assert.deepEqual(
    [...names],
    [
      ['S1', '老大'],
      ['S4', '阿妈'],
    ],
  );
});

test('a line on its own reads "名字：台词"; inside a sentence it is quoted', () => {
  const names = speakerNames(profiles);
  assert.equal(readablePrompt('(S1) <d>[Chinese] 今天吃牛。</d>', names), '老大：今天吃牛。');
  assert.equal(
    readablePrompt('(S1) <d>[Chinese] 追！</d>\n(S4) <d>[Chinese] 别去。</d>', names),
    '老大：追！\n阿妈：别去。',
  );
  // The sentence already names the speaker: no name added.
  assert.equal(
    readablePrompt('左边老大粗暴瞪牛喊道(S1) <d>[Chinese] 今天吃牛。</d>。', names),
    '左边老大粗暴瞪牛喊道：“今天吃牛。”。',
  );
  // It does not: the name is added in brackets.
  assert.equal(
    readablePrompt('她颤抖着说道(S4) <d>[Chinese] 别去。</d>', names),
    '她颤抖着说道（阿妈）：“别去。”',
  );
  // Unknown speaker numbers are never shown.
  assert.equal(
    readablePrompt('老三嘟囔(S3) <d>[Chinese] 有面包。</d>', names),
    '老三嘟囔：“有面包。”',
  );
  assert.equal(readablePrompt('(S3) <d>[Chinese] 有面包。</d>', names), '“有面包。”');
  assert.equal(readablePrompt('他说：<d>好。</d>', names), '他说：“好。”');
  assert.equal(
    readablePrompt('John turns (S1) <d>[English] Get it back.</d>', new Map([['S1', 'John']])),
    'John turns: “Get it back.”',
  );
  assert.equal(readablePrompt('plain text', names), 'plain text');
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
