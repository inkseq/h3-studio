import test from 'node:test';
import assert from 'node:assert/strict';
import { speechEdit, speechParts, speechShown, speechStored } from './speech-edit.js';

const STORED = '左头呵斥 (S1) <d>[Chinese] 今天吃牛。</d>，中头回应(S2)<d>那是阿花。</d>';
const type = (stored, edit) => {
  const parts = speechParts(stored);
  const shown = speechShown(parts);
  const [after, caret] = edit(shown);
  const next = speechEdit(parts, after, caret);
  assert.equal(speechShown(next), after, 'the box keeps exactly what was typed');
  return speechStored(next);
};

test('the box shows the words in quotes; saving unchanged text changes nothing (#1113)', () => {
  const parts = speechParts(STORED);
  assert.equal(speechShown(parts), '左头呵斥 「今天吃牛。」，中头回应「那是阿花。」');
  assert.equal(speechStored(parts), STORED);
  assert.equal(speechShown(speechParts('She says (S1) <d>[English] Hi.</d>')), 'She says “Hi.”');
  assert.equal(speechShown(speechParts('No lines.')), 'No lines.');
  assert.equal(speechStored(speechParts('')), '');
});

test('typing inside the quotes edits the line and keeps its tag', () => {
  // Replace 牛 with 鱼.
  assert.equal(
    type(STORED, (s) => [s.replace('牛', '鱼'), s.indexOf('牛') + 1]),
    STORED.replace('牛', '鱼'),
  );
  // Type at the very start and end of a line's words.
  assert.equal(
    type(STORED, (s) => [s.replace('「今', '「好，今'), s.indexOf('今') + 2]),
    STORED.replace('[Chinese] 今', '[Chinese] 好，今'),
  );
  assert.equal(
    type(STORED, (s) => [s.replace('阿花。」', '阿花。啊」'), s.indexOf('阿花。」') + 4]),
    STORED.replace('那是阿花。</d>', '那是阿花。啊</d>'),
  );
  // Clearing a line's words keeps an empty line.
  assert.equal(
    type('(S1) <d>[English] Hi.</d>', () => ['“”', 1]),
    '(S1) <d>[English] </d>',
  );
});

test('typing outside the quotes edits the story text, quotes typed there stay text', () => {
  assert.equal(
    type(STORED, (s) => [s.replace('左头呵斥', '左头大声呵斥'), 4]),
    STORED.replace('左头呵斥', '左头大声呵斥'),
  );
  // Right after a line's closing quote is story text.
  const after = '左头呵斥 「今天吃牛。」！，中头回应「那是阿花。」';
  assert.equal(
    type(STORED, () => [after, 13]),
    STORED.replace('</d>，', '</d>！，'),
  );
  // A new quote typed in the story text is not a speech line.
  assert.equal(
    type('她看着招牌', (s) => [s + '「牛肉面」', 10]),
    '她看着招牌「牛肉面」',
  );
});

test('deleting a quote mark turns that line into text; other lines keep their tags', () => {
  const stored = type(STORED, (s) => [s.replace('「今天', '今天'), 5]);
  assert.equal(stored, '左头呵斥 今天吃牛。」，中头回应(S2)<d>那是阿花。</d>');
  // Select everything and paste: no tags left unless pasted.
  assert.equal(
    type(STORED, () => ['全新的文字', 5]),
    '全新的文字',
  );
  assert.equal(
    type(STORED, () => ['他说(S1) <d>[Chinese] 好。</d>', 22]),
    '他说(S1) <d>[Chinese] 好。</d>',
  );
});

test('a typed character equal to its neighbour lands where the caret says', () => {
  // 「哈」 → typing 哈 inside gives 「哈哈」 either way; typing 」 after the line is text.
  assert.equal(
    type('(S1) <d>[Chinese] 哈</d>', () => ['「哈哈」', 2]),
    '(S1) <d>[Chinese] 哈哈</d>',
  );
  assert.equal(
    type('(S1) <d>[Chinese] 哈</d>', () => ['「哈」」', 4]),
    '(S1) <d>[Chinese] 哈</d>」',
  );
});

test('a line written in another script changes its language tag', () => {
  assert.equal(
    type('(S1) <d>[Chinese] 你好</d>', () => ['「Hello」', 6]),
    '(S1) <d>[English] Hello</d>',
  );
  assert.equal(
    type('(S1) <d>[English] Hi</d>', () => ['“你好”', 3]),
    '(S1) <d>[Chinese] 你好</d>',
  );
});
