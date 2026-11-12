import test from 'node:test';
import assert from 'node:assert/strict';
import { shownVoices, voiceFilters, voiceSpeaks } from './voice-catalog.js';

const voices = [
  { id: 'a', gender: 'female', age: 'adult', speaks: true },
  { id: 'b', gender: 'female', age: 'child', speaks: false },
  { id: 'c', gender: 'male', age: 'senior', speaks: false },
  { id: 'd', gender: 'male', age: 'adult' },
];

test('filters follow the tags the list has, in a fixed order', () => {
  assert.deepEqual(voiceFilters(voices), {
    genders: ['female', 'male'],
    ages: ['child', 'adult', 'senior'],
  });
  // One value only is no choice, and a list without age tags offers no age filter.
  const old = [
    { id: 'a', gender: 'female' },
    { id: 'b', gender: 'female' },
  ];
  assert.deepEqual(voiceFilters(old), { genders: [], ages: [] });
});

test('a filter narrows the list and the voice in use always shows', () => {
  const ids = (filter, chosen) => shownVoices(voices, filter, chosen).map((voice) => voice.id);
  assert.deepEqual(ids({}), ['a', 'b', 'c', 'd']);
  assert.deepEqual(ids({ gender: 'male' }), ['c', 'd']);
  assert.deepEqual(ids({ gender: 'female', age: 'child' }), ['b']);
  assert.deepEqual(ids({ age: 'teen' }), []);
  assert.deepEqual(ids({ gender: 'male', age: 'senior' }, 'a'), ['a', 'c']);
});

test('a designed voice cannot say a line', () => {
  assert.equal(voiceSpeaks(voices, 'a'), true);
  assert.equal(voiceSpeaks(voices, 'b'), false);
  assert.equal(voiceSpeaks(voices, 'd'), true);
  assert.equal(voiceSpeaks(voices, ''), false);
  assert.equal(voiceSpeaks(voices, 'gone'), true);
});
