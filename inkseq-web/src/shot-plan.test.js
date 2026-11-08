import test from 'node:test';
import assert from 'node:assert/strict';
import { planChecklist, planOf, previousExit, validPlanPatch } from './shot-plan.js';
import { applyStoryboardAction } from './storyboard-actions.js';

const story = () => ({
  entities: [
    { id: 'c1', type: 'character', title: '老大', version: 1, data: {} },
    { id: 's1', type: 'shot', version: 1, status: 'ready', data: {} },
  ],
  links: [],
});

test('plan patches are checked field by field', () => {
  const project = story();
  const shot = project.entities[1];
  assert.equal(validPlanPatch(project, shot, { size: 'close', purpose: '老大要去追' }), true);
  assert.equal(validPlanPatch(project, shot, { size: 'huge' }), false);
  assert.equal(validPlanPatch(project, shot, { continuity: 'master' }), true);
  assert.equal(validPlanPatch(project, shot, { silent: ['c1'] }), true);
  assert.equal(validPlanPatch(project, shot, { silent: ['nobody'] }), false);
  assert.equal(
    validPlanPatch(project, shot, { timeline: [{ from: 0, to: 1.5, text: '低头' }] }),
    true,
  );
  assert.equal(
    validPlanPatch(project, shot, { timeline: [{ from: -1, to: 1, text: 'x' }] }),
    false,
  );
  assert.equal(validPlanPatch(project, shot, { beats: ['beat-a'] }), false);
  assert.equal(validPlanPatch(project, project.entities[0], { size: 'close' }), false);
});

test('the shotPlan action keeps claimed beats and marks the shot for review', () => {
  const project = story();
  const shot = project.entities[1];
  shot.data.plan = { beats: ['beat-a'] };
  assert.deepEqual(
    applyStoryboardAction(project, { type: 'shotPlan', shot: 's1', patch: { size: 'medium' } }),
    { id: 's1' },
  );
  assert.deepEqual(planOf(shot).beats, ['beat-a']);
  assert.equal(planOf(shot).size, 'medium');
  assert.equal(shot.status, 'review');
});

test('the checklist follows the pre-generation rules and the next shot copies the exit', () => {
  const entity = {
    data: {
      plan: {
        purpose: '老大要去追',
        beats: ['b'],
        size: 'close',
        emotionFrom: '发愣',
        emotionTo: '压着火',
        timeline: [{ from: 0, to: 1, text: '眉头压低' }],
        entry: '老大在画面左',
        exit: '老大转身',
      },
    },
  };
  const characters = [
    {
      id: 'c1',
      bible: { expressions: { a: '1', b: '2', c: '3', d: '4', e: '5', f: '' } },
    },
  ];
  const items = planChecklist({ entity, start: null }, { speakers: ['c1'], characters });
  assert.deepEqual(
    items.filter((i) => !i.done).map((i) => i.key),
    ['expressions', 'firstFrame'],
  );
  assert.equal(
    planChecklist({ entity: { data: {} }, start: 'img' }).some((i) => i.key === 'expressions'),
    false,
  );
  assert.equal(
    previousExit(
      [
        { id: 'x', entity },
        { id: 'y', entity: { data: {} } },
      ],
      'y',
    ),
    '老大转身',
  );
  assert.equal(previousExit([{ id: 'x', entity }], 'x'), '');
});
