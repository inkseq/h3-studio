import test from 'node:test';
import assert from 'node:assert/strict';
import { words } from './i18n/error-words.js';

const inPage = (locale, run) => {
  globalThis.document = {};
  globalThis.localStorage = { getItem: () => locale };
  try {
    run();
  } finally {
    delete globalThis.document;
    delete globalThis.localStorage;
  }
};

test('thrown Chinese errors read in the page language', () => {
  const text = '请填写故事名称。';
  assert.equal(words(text), text);
  inPage('en', () => assert.equal(words(text), 'Enter a story name.'));
  inPage('zh', () => assert.equal(words(text), text));
  inPage('en', () => assert.equal(words('没有收录的文字'), '没有收录的文字'));
});
