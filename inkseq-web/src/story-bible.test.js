import test from 'node:test';
import assert from 'node:assert/strict';
import {
  applyBiblePatch,
  bibleOf,
  characterGroups,
  exportBible,
  hasVoiceSample,
  importActions,
  importPlan,
  nextSpeaker,
  peopleInProfile,
  profileChecklist,
  speakingPlaces,
  splitActions,
  splitBase,
  validBiblePatch,
  voiceGaps,
} from './story-bible.js';
import { applyStoryboardAction } from './storyboard-actions.js';
import { deletionImpact } from './entity-references.js';

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
    voice: 'clip-1',
    voices: [{ asset: 'clip-1', start: 0 }],
    bible: bibleOf({ data: { body: { line: 'one man' }, expressions: { pain: 'img-1' } } }),
  });
  const items = profileChecklist(p, { used: true });
  // A voice chosen from our list alone is not a sample a shot can send (h3-studio#1222).
  const voiceDone = (over) => profileChecklist(view(over)).find((i) => i.key === 'voice').done;
  assert.equal(voiceDone({ voiceId: 'v1' }), false);
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
  assert.deepEqual(plan[1].lights, [{ name: '黄昏', desc: '' }]);
  assert.equal(plan[2].patch.scale, 'hand');
  assert.deepEqual(plan[2].patch.states, [{ name: '拉满', imageId: '' }]);
  assert.deepEqual(importPlan(profiles, { nothing: true }), []);

  const out = exportBible([
    view({ id: 'a', name: '老大', bible: { ...bibleOf({ data: { speaker: 'S1' } }) } }),
  ]);
  assert.equal(out.format, 'inkseq.story-bible/1');
  assert.equal(out.characters[0].speaker, 'S1');
});

// A profile view built from a stored entity, as storyboard-domain does.
const viewOf = (e) =>
  view({
    id: e.id,
    type: e.type,
    name: e.title,
    desc: e.description || '',
    voiceId: e.data.voiceId || '',
    variants: (e.data.looks || [{ id: 'main', name: '主图', imageIds: e.data.imageIds }]).map(
      (v) => ({ id: v.id, name: v.name, desc: v.description || '', images: v.imageIds || [] }),
    ),
    def: e.data.defaultLookId || 'main',
    bible: bibleOf(e),
  });

test('one card holding several people is recognised from body, name or description', () => {
  const people = (over, others = []) => peopleInProfile(view(over), others);
  assert.equal(people({ name: '儿子（三兄弟）' }), 3);
  assert.equal(people({ name: 'The twins' }), 2);
  assert.equal(people({ name: 'Giant', desc: 'a two-headed giant' }), 2);
  assert.equal(people({ name: '怪', desc: '一个身体三个头' }), 3);
  assert.equal(people({ bible: bibleOf({ data: { body: { heads: 3 } } }) }), 3);
  assert.equal(people({ name: '阿妈', desc: '她有两个儿子，个子矮小' }), 0);
  assert.equal(people({ name: '老大', desc: '他有两个弟弟' }), 0);
  // A member of a group that already has several cards is one person.
  const member = (id) => view({ id, bible: bibleOf({ data: { groupId: '三兄弟' } }) });
  assert.equal(peopleInProfile(member('1'), [member('1'), member('2')]), 0);
  assert.equal(peopleInProfile(member('1'), [member('1')]), 3);
  assert.equal(peopleInProfile(view({ type: 'prop', name: '三兄弟的弓' })), 0);
  assert.equal(splitBase('儿子（三兄弟）'), '儿子');
  assert.equal(splitBase('Ann'), 'Ann');
});

test('speaking places come from line beats, the speaker number and unclaimed lines in a shot', () => {
  const sons = view({ id: 'sons', name: '儿子（三兄弟）' });
  const mum = view({ id: 'mum', name: '阿妈', bible: bibleOf({ data: { speaker: 'S4' } }) });
  const lion = view({ id: 'lion', name: '狮皮人' });
  const profiles = [sons, mum, lion];
  const shots = [
    { prompt: '老大喊道(S1) <d>[Chinese] 今天吃牛。</d>', cast: [{ id: 'sons' }] },
    { prompt: '阿妈说(S4) <d>[Chinese] 别去。</d>', cast: [{ id: 'mum' }, { id: 'sons' }] },
    { prompt: '狮皮人射箭，无对白。', cast: [{ id: 'lion' }] },
  ];
  const scenes = [{ data: { beats: [{ id: 'beat-1', kind: 'line', speaker: 'lion' }] } }];
  const at = (p) => speakingPlaces(p, { scenes, shots, profiles });
  assert.equal(at(sons), 1);
  assert.equal(at(mum), 1);
  assert.equal(at(lion), 1);
  assert.deepEqual(voiceGaps(sons, at(sons)), ['speaker', 'voice']);
  assert.deepEqual(voiceGaps(mum, at(mum)), ['voice']);
  assert.deepEqual(voiceGaps(view({ voiceId: 'v1' }), 0), []);
});

test('a character has a voice only when a shot would send a sample for it', () => {
  const tagged = { bible: bibleOf({ data: { speaker: 'S1' } }) },
    voices = [{ asset: 'clip-1', start: 0 }];
  const gaps = (over) => voiceGaps(view({ ...tagged, ...over }), 1);
  // A voice from our list alone, or a sample that is neither the main voice nor a look's
  // voice, sends nothing.
  assert.deepEqual(gaps({ voiceId: 'v-steady' }), ['voice']);
  assert.deepEqual(gaps({ voices }), ['voice']);
  assert.deepEqual(gaps({ voice: 'gone', voices }), ['voice']);
  assert.deepEqual(gaps({ voiceId: 'v-steady', voice: 'clip-1', voices }), []);
  const look = { id: 'main', name: '默认', desc: '', images: [], voice: 'clip-1' };
  assert.deepEqual(gaps({ voices, variants: [look] }), []);
  assert.equal(hasVoiceSample(view({ voice: 'clip-1', voices })), true);
  assert.equal(hasVoiceSample(null), false);
});

test('splitting makes one card per person in a group, all at once, and keeps the old card', () => {
  const project = story();
  project.entities.push({
    id: 'sons',
    type: 'character',
    title: '儿子（三兄弟）',
    description: '三个独立的年轻男子',
    version: 1,
    data: {
      looks: [{ id: 'main', name: '默认造型', description: '棕袍', imageIds: [] }],
      defaultLookId: 'main',
      anchors: ['满脸黑胡'],
      body: { heads: 3, arms: 6, legs: 6, line: '三个人肩并肩' },
    },
  });
  const views = project.entities.filter((e) => e.type === 'character').map(viewOf);
  const sons = views.find((p) => p.id === 'sons');
  assert.equal(splitActions(sons, views, { names: ['大儿子', '大儿子'] }), null);
  assert.equal(splitActions(sons, views, { names: ['大儿子'] }), null);
  assert.equal(splitActions(sons, views, { names: ['大儿子', ' '] }), null);
  const before = project.entities.length;
  const actions = splitActions(sons, views, {
    names: ['大儿子', '二儿子', '小儿子'],
    lookName: 'Default look',
  });
  const receipt = applyStoryboardAction(project, { type: 'batch', actions });
  assert.equal(receipt.ok, true);
  assert.equal(project.entities.length, before + 3);
  const members = project.entities.slice(before);
  assert.deepEqual(
    members.map((e) => [e.type, e.title]),
    [
      ['character', '大儿子'],
      ['character', '二儿子'],
      ['character', '小儿子'],
    ],
  );
  // S1 is already 老大's (entity a), so the members get S2, S3, S4.
  assert.deepEqual(
    members.map((e) => e.data.speaker),
    ['S2', 'S3', 'S4'],
  );
  for (const e of members) {
    assert.equal(e.data.groupId, '儿子');
    assert.equal(e.description, '三个独立的年轻男子');
    assert.equal(e.data.looks[0].name, 'Default look');
    assert.equal(e.data.looks[0].description, '棕袍');
    assert.deepEqual(e.data.anchors, ['满脸黑胡']);
    assert.deepEqual(e.data.body, { heads: 1, arms: 2, legs: 2, line: '三个人肩并肩' });
  }
  assert.equal(entity(project, 'sons').title, '儿子（三兄弟）');
});

test('a split puts every member in the old card’s shots and copies its photos, in one edit', () => {
  const project = story();
  project.entities.push(
    { id: 'face', type: 'image', version: 1, data: {} },
    { id: 'side', type: 'image', version: 1, data: {} },
    {
      id: 'sons',
      type: 'character',
      title: '儿子（三兄弟）',
      version: 1,
      data: {
        looks: [
          {
            id: 'main',
            name: '默认造型',
            description: '',
            imageIds: ['face', 'side', 'gone'],
            gallery: { front: 'face', side: 'gone' },
          },
        ],
        defaultLookId: 'main',
      },
    },
    { id: 'scene', type: 'scene', version: 1, status: 'ready', data: { cast: ['sons'] } },
    {
      id: 'shot-1',
      type: 'shot',
      parentId: 'scene',
      version: 1,
      status: 'ready',
      data: {
        cast: [
          { characterId: 'a', lookId: '' },
          { characterId: 'sons', lookId: 'main' },
        ],
      },
    },
    {
      id: 'shot-2',
      type: 'shot',
      parentId: 'scene',
      version: 1,
      status: 'ready',
      data: { cast: [{ characterId: 'b', lookId: '' }] },
    },
  );
  const views = project.entities.filter((e) => e.type === 'character').map(viewOf);
  const sons = views.find((p) => p.id === 'sons');
  const actions = splitActions(sons, views, { names: ['老大', '老二', '老三'] });
  const draft = structuredClone(project);
  const receipt = applyStoryboardAction(draft, { type: 'batch', actions });
  assert.equal(receipt.ok, true);
  const members = draft.entities.filter((e) => e.type === 'character').slice(-3);
  const ids = members.map((e) => e.id);
  // The old card keeps its place in the order; members take it, with no look chosen.
  assert.deepEqual(entity(draft, 'shot-1').data.cast, [
    { characterId: 'a', lookId: '' },
    ...ids.map((id) => ({ characterId: id, lookId: '' })),
  ]);
  assert.deepEqual(
    entity(draft, 'scene').data.cast,
    ids.map((id) => ({ characterId: id, lookId: '' })),
  );
  assert.equal(entity(draft, 'shot-1').status, 'review');
  assert.deepEqual(entity(draft, 'shot-2').data.cast, [{ characterId: 'b', lookId: '' }]);
  assert.equal(entity(draft, 'shot-2').version, 1);
  for (const member of members) {
    assert.deepEqual(member.data.looks[0].imageIds, ['face', 'side']);
    assert.deepEqual(member.data.looks[0].gallery, { front: 'face' });
  }
  // The old card is untouched; trashing it afterwards leaves every shot with the members.
  assert.deepEqual(entity(draft, 'sons'), entity(project, 'sons'));
  assert.equal(deletionImpact(draft, 'sons').cast, 0);

  // Leaving the photos out keeps the new looks empty; the casts still change.
  const bare = structuredClone(project);
  applyStoryboardAction(bare, {
    type: 'batch',
    actions: splitActions(sons, views, { names: ['老大', '老二'], copyPhotos: false }),
  });
  const bareMembers = bare.entities.filter((e) => e.type === 'character').slice(-2);
  for (const member of bareMembers) assert.deepEqual(member.data.looks[0].imageIds, []);
  assert.equal(entity(bare, 'shot-1').data.cast.length, 3);
});

test('a recast is refused for unknown, repeated or non-person targets', () => {
  const project = story();
  const recast = (to) =>
    applyStoryboardAction(structuredClone(project), { type: 'recast', id: 'a', to });
  assert.equal(recast([]), false);
  assert.equal(recast(['b', 'b']), false);
  assert.equal(recast(['a']), false);
  assert.equal(recast(['pen']), false);
  assert.equal(recast(['nobody']), false);
  assert.deepEqual(recast(['b']), { id: 'a' });
});

test('a batch is refused as a whole when any step is refused', () => {
  const project = story();
  const before = project.entities.length;
  const draft = structuredClone(project);
  const receipt = applyStoryboardAction(draft, {
    type: 'batch',
    actions: [
      { type: 'newProfile', ptype: 'character', name: '新人' },
      { type: 'bible', pid: { $ref: 0 }, patch: { speaker: 'S1' } },
    ],
  });
  // The caller's edit runs on a copy (store.editProject) and drops it on false.
  assert.equal(receipt, false);
  assert.equal(project.entities.length, before);
  assert.equal(applyStoryboardAction(draft, { type: 'batch', actions: [] }), false);
  assert.equal(
    applyStoryboardAction(draft, { type: 'batch', actions: [{ type: 'batch', actions: [] }] }),
    false,
  );
  assert.equal(
    applyStoryboardAction(draft, {
      type: 'batch',
      actions: [{ type: 'bible', pid: { $ref: 3 }, patch: { anchors: [] } }],
    }),
    false,
  );
});

test('export keeps voice, anchors, body, expressions, prop states and speaker; import applies them', () => {
  const source = story();
  const a = entity(source, 'a');
  a.data.voiceId = 'v-warm';
  applyBiblePatch(a, {
    anchors: ['thick black beard'],
    body: { heads: 1, arms: 2, legs: 2, line: 'one man' },
    expressions: { pain: 'img-1' },
    voiceNote: 'low and slow',
    groupId: 'brothers',
  });
  const bow = entity(source, 'bow');
  applyBiblePatch(bow, {
    states: [{ name: 'drawn', imageId: 'img-1' }],
    fixedLine: 'a short bow in his left hand',
    carriedBy: 'a',
  });
  const json = JSON.parse(
    JSON.stringify(
      exportBible(
        source.entities.filter((e) => ['character', 'prop'].includes(e.type)).map(viewOf),
        {
          assetName: (id) => (id === 'img-1' ? 'face.png' : ''),
        },
      ),
    ),
  );
  assert.deepEqual(json.characters[0].voice, { id: 'v-warm', note: 'low and slow' });
  assert.deepEqual(json.characters[0].expressions, { pain: { id: 'img-1', name: 'face.png' } });
  assert.deepEqual(json.props[0].states, [
    { state: 'drawn', image: { id: 'img-1', name: 'face.png' } },
  ]);

  // Into a story with the same picture and no cards: everything comes back.
  const target = {
    entities: [{ id: 'img-1', type: 'image', version: 1, data: {} }],
    links: [],
  };
  const plan = importPlan([], json, { images: new Set(['img-1']) });
  const first = plan.find((item) => item.name === '老大');
  for (const key of ['anchors', 'body', 'expressions', 'speaker', 'groupId', 'voice'])
    assert.ok(first.changes.includes(key), key);
  const keep = new Set(plan.map((_, i) => i));
  const receipt = applyStoryboardAction(target, {
    type: 'batch',
    actions: importActions(plan, keep, { profiles: [], lookName: 'Default look' }),
  });
  assert.equal(receipt.ok, true);
  const hero = target.entities.find((e) => e.title === '老大');
  assert.equal(hero.data.speaker, 'S1');
  assert.equal(hero.data.voiceId, 'v-warm');
  assert.equal(hero.data.voiceNote, 'low and slow');
  assert.equal(hero.data.groupId, 'brothers');
  assert.deepEqual(hero.data.anchors, ['thick black beard']);
  assert.deepEqual(hero.data.body, { heads: 1, arms: 2, legs: 2, line: 'one man' });
  assert.equal(hero.data.expressions.pain, 'img-1');
  const prop = target.entities.find((e) => e.title === '弓');
  assert.deepEqual(prop.data.states, [{ name: 'drawn', imageId: 'img-1' }]);
  assert.equal(prop.data.carriedBy, hero.id);
  assert.equal(prop.data.fixedLine, 'a short bow in his left hand');

  // A picture that is not in the story is counted and left out; a taken number is left out.
  const elsewhere = importPlan(
    [view({ id: 'x', bible: bibleOf({ data: { speaker: 'S1' } }) })],
    json,
  );
  assert.equal(elsewhere[0].missing, 1);
  assert.equal(elsewhere[0].patch.expressions, undefined);
  assert.deepEqual(elsewhere[0].skipped, ['speaker']);
  // Importing the same file again into the filled story changes nothing.
  const again = importPlan(
    target.entities.filter((e) => ['character', 'prop'].includes(e.type)).map(viewOf),
    json,
    { images: new Set(['img-1']) },
  );
  assert.deepEqual(
    again.filter((item) => item.type === 'character').map((item) => item.changes),
    [[], []],
  );
});
