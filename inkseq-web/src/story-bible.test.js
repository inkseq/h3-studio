import test from 'node:test';
import assert from 'node:assert/strict';
import {
  applyBiblePatch,
  bibleOf,
  characterGroups,
  exportBible,
  importPlan,
  nextSpeaker,
  profileChecklist,
  validBiblePatch,
} from './story-bible.js';
import { applyStoryboardAction } from './storyboard-actions.js';

const story = () => ({
  entities: [
    { id: 'img-1', type: 'image', version: 1, data: {} },
    { id: 'a', type: 'character', title: '老大', version: 1, data: { speaker: 'S1', looks: [] } },
    { id: 'b', type: 'character', title: '老二', version: 1, data: { looks: [] } },
    { id: 'pen', type: 'location', title: '牛圈', version: 1, data: { versions: [] } },
    { id: 'bow', type: 'prop', title: '弓', version: 1, data: { imageIds: [] } },
  ],
  links: [],
});
const entity = (project, id) => project.entities.find((e) => e.id === id);

test('a speaker number belongs to one character only', () => {
  const project = story();
  assert.equal(validBiblePatch(project, entity(project, 'b'), { speaker: 'S1' }), false);
  assert.equal(validBiblePatch(project, entity(project, 'b'), { speaker: 'S2' }), true);
  assert.equal(validBiblePatch(project, entity(project, 'a'), { speaker: 'S1' }), true);
  assert.equal(validBiblePatch(project, entity(project, 'b'), { speaker: 'Narrator' }), false);
});

test('fields are checked per kind and pictures must be story images', () => {
  const project = story();
  const bow = entity(project, 'bow'),
    a = entity(project, 'a');
  assert.equal(validBiblePatch(project, bow, { body: { heads: 1 } }), false);
  assert.equal(validBiblePatch(project, a, { expressions: { pain: 'img-1' } }), true);
  assert.equal(validBiblePatch(project, a, { expressions: { pain: 'nope' } }), false);
  assert.equal(validBiblePatch(project, a, { expressions: { laugh: 'img-1' } }), false);
  assert.equal(validBiblePatch(project, bow, { carriedBy: 'a' }), true);
  assert.equal(validBiblePatch(project, bow, { carriedBy: 'pen' }), false);
  assert.equal(validBiblePatch(project, bow, { scale: 'huge' }), false);
  assert.equal(validBiblePatch(project, a, { body: { heads: 13 } }), false);
});

test('nested fields merge one level and the bible reads back with defaults', () => {
  const project = story();
  const a = entity(project, 'a');
  applyBiblePatch(a, { body: { heads: 1 } });
  applyBiblePatch(a, { body: { arms: 2 }, expressions: { pain: 'img-1' } });
  const b = bibleOf(a);
  assert.deepEqual([b.body.heads, b.body.arms, b.body.legs], [1, 2, null]);
  assert.equal(b.expressions.pain, 'img-1');
  assert.equal(b.expressions.neutral, '');
  assert.equal(bibleOf({ data: { scale: 'odd' } }).scale, '');
});

test('the bible action marks shots that use the profile for review', () => {
  const project = story();
  project.entities.push({
    id: 'shot-1',
    type: 'shot',
    version: 1,
    status: 'ready',
    data: { cast: [{ characterId: 'a' }], storyboard: {} },
  });
  const receipt = applyStoryboardAction(project, {
    type: 'bible',
    pid: 'a',
    patch: { anchors: ['thick black beard'] },
  });
  assert.deepEqual(receipt, { id: 'a' });
  assert.deepEqual(entity(project, 'a').data.anchors, ['thick black beard']);
  assert.equal(entity(project, 'shot-1').status, 'review');
  assert.equal(applyStoryboardAction(project, { type: 'bible', pid: 'a', patch: { x: 1 } }), false);
});

const view = (over) => ({
  id: 'p',
  type: 'character',
  name: 'x',
  desc: '',
  voice: '',
  voiceId: '',
  voices: [],
  variants: [{ id: 'main', name: '默认', desc: '', images: [] }],
  bible: bibleOf({ data: {} }),
  ...over,
});

test('the checklist follows the flow and counts a partial expression sheet', () => {
  const p = view({
    variants: [{ id: 'main', name: '默认', desc: '棕袍', images: ['img-1'] }],
    voiceId: 'v1',
    bible: bibleOf({ data: { body: { line: 'one man' }, expressions: { pain: 'img-1' } } }),
  });
  const items = profileChecklist(p, { used: true });
  assert.deepEqual(
    items.map((i) => [i.key, i.done]),
    [
      ['anchor', true],
      ['body', true],
      ['expressions', false],
      ['look', true],
      ['voice', true],
      ['used', true],
    ],
  );
  assert.equal(items[2].part, 1);
  assert.deepEqual(
    profileChecklist(view({ type: 'prop' })).map((i) => i.key),
    ['picture', 'scale', 'states', 'fixedLine'],
  );
});

test('groups keep one card per person and singles stay alone', () => {
  const g = (id, groupId) => view({ id, bible: { ...bibleOf({ data: {} }), groupId } });
  const out = characterGroups([g('1', '三兄弟'), g('2', ''), g('3', '三兄弟')]);
  assert.deepEqual(
    out.map((x) => [x.group, x.members.map((m) => m.id)]),
    [
      ['三兄弟', ['1', '3']],
      ['', ['2']],
    ],
  );
  assert.equal(nextSpeaker([view({ bible: { ...bibleOf({ data: {} }), speaker: 'S1' } })]), 'S2');
});

test('a cast/art design imports as candidates and exports back', () => {
  const json = {
    characters: [
      {
        name: '老大',
        persona: { identity: '长子', ageRange: '约 25 岁', personality: ['急', '护短'] },
        voice: { timbre: '低沉' },
      },
    ],
    scenes: [
      {
        name: '海边牛圈',
        summary: '黄昏的牛圈',
        anchors: [{ name: '石墙', desc: '北边低矮的干砌石墙' }],
        lighting: [{ state: '黄昏', prompt: 'dusk' }],
      },
    ],
    props: [{ name: '弓', scale: '手持级', anchors: ['短木弓'], states: [{ state: '拉满' }] }],
  };
  const profiles = [view({ id: 'a', name: '老大' })];
  const plan = importPlan(profiles, json);
  assert.deepEqual(
    plan.map((i) => [i.type, i.name, i.existing]),
    [
      ['character', '老大', 'a'],
      ['location', '海边牛圈', null],
      ['prop', '弓', null],
    ],
  );
  assert.equal(plan[0].patch.profile.personality, '急、护短');
  assert.deepEqual(plan[1].patch.anchors, ['石墙：北边低矮的干砌石墙']);
  assert.deepEqual(plan[1].lights, ['黄昏']);
  assert.equal(plan[2].patch.scale, 'hand');
  assert.deepEqual(plan[2].patch.states, [{ name: '拉满', imageId: '' }]);
  assert.deepEqual(importPlan(profiles, { nothing: true }), []);

  const out = exportBible([
    view({ id: 'a', name: '老大', bible: { ...bibleOf({ data: { speaker: 'S1' } }) } }),
  ]);
  assert.equal(out.format, 'inkseq.story-bible/1');
  assert.equal(out.characters[0].speaker, 'S1');
});
