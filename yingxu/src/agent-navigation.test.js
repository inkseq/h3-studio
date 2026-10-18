import test from 'node:test';
import assert from 'node:assert/strict';
import {
  readAgentLink,
  locateEntity,
  mergeActivity,
  freestyleEntityTarget,
  freestyleLinkState,
} from './agent-navigation.js';
import { createCloudClient } from './cloud-client.js';

test('shared links parse only bounded project/entity identifiers and no credentials', () => {
  assert.deepEqual(readAgentLink('?project=cloud-1&entity=shot-2&panel=activity&token=ignored'), {
    projectId: 'cloud-1',
    entityId: 'shot-2',
    activity: true,
  });
  for (const search of [
    '',
    '?project=',
    '?project=a&project=b',
    '?project=x&entity=%0aevil',
    '?project=' + 'a'.repeat(161),
    '?project=x&entity=a&entity=b',
  ])
    assert.equal(readAgentLink(search), null);
  assert.equal(readAgentLink('?project=cloud-1&panel=delete').activity, false);
});
test('quick links locate the requested second shot only after the correct account and cloud project open', () => {
  const project = {
    id: 'quick-1',
    entities: [
      { id: 's1', type: 'shot', data: {} },
      { id: 's2', type: 'shot', data: {} },
    ],
    links: [],
  };
  const state = { project, workspace: { mode: 'cloud', account: 'superdan' } },
    before = structuredClone(state),
    search = '?project=quick-1&entity=s2';
  assert.deepEqual(freestyleLinkState(state, search, 'superdan'), {
    activity: false,
    target: { shotId: 's2', entityId: 's2' },
  });
  assert.equal(freestyleLinkState(state, search, 'supervan'), null);
  assert.equal(freestyleLinkState(state, '?project=other&entity=s2', 'superdan'), null);
  assert.equal(
    freestyleLinkState({ ...state, workspace: { mode: 'local' } }, search, 'superdan'),
    null,
  );
  assert.equal(
    freestyleLinkState(state, '?project=quick-1&panel=activity', 'superdan').activity,
    true,
  );
  assert.deepEqual(
    freestyleLinkState(state, '?project=quick-1&entity=removed', 'superdan').target,
    null,
  );
  assert.deepEqual(state, before);
});
test('quick media navigation picks an unambiguous source shot and leaves shared or story entities for the workbench', () => {
  const project = {
    entities: [
      { id: 's1', type: 'shot', data: {} },
      { id: 's2', type: 'shot', data: { candidateIds: ['v'] } },
      { id: 'v', type: 'video', data: { sourceShotId: 's2' } },
      { id: 'ref', type: 'image', data: {} },
      { id: 'chapter', type: 'chapter', data: {} },
    ],
    links: [
      { source: 'ref', target: 's1' },
      { source: 'ref', target: 's2' },
    ],
  };
  assert.deepEqual(freestyleEntityTarget(project, 'v'), { shotId: 's2', entityId: 'v' });
  assert.deepEqual(freestyleEntityTarget(project, 'ref'), { shotId: null, entityId: 'ref' });
  assert.deepEqual(freestyleEntityTarget(project, 'chapter'), {
    shotId: null,
    entityId: 'chapter',
  });
  assert.equal(freestyleEntityTarget(project, 'missing'), null);
});
test('entity navigation finds chapter, handles removed targets and does not edit content', () => {
  const project = {
      entities: [
        { id: 'c', type: 'chapter', parentId: null },
        { id: 's', type: 'scene', parentId: 'c' },
        { id: 'shot', type: 'shot', parentId: 's' },
        { id: 'character', type: 'character' },
        { id: 'video', type: 'video' },
      ],
    },
    before = structuredClone(project);
  assert.deepEqual(locateEntity(project, 'shot'), {
    entityId: 'shot',
    chapterId: 'c',
    section: 'story',
  });
  assert.equal(locateEntity(project, 'character').section, 'characters');
  assert.equal(locateEntity(project, 'video').section, 'assets');
  assert.equal(locateEntity(project, 'removed'), null);
  assert.deepEqual(project, before);
  assert.equal(
    locateEntity(
      {
        entities: [
          { id: 'a', type: 'scene', parentId: 'b' },
          { id: 'b', type: 'scene', parentId: 'a' },
        ],
      },
      'a',
    ).chapterId,
    null,
  );
});
test('activity refresh merges overlapping version pages without duplicates', () => {
  const rows = mergeActivity(
    [
      { id: 'v3', project_version: 3 },
      { id: 'v1', project_version: 1 },
    ],
    [
      { id: 'v4', project_version: 4 },
      { id: 'v3', project_version: 3 },
    ],
  );
  assert.deepEqual(
    rows.map((e) => e.id),
    ['v4', 'v3', 'v1'],
  );
});
test('activity GET remains authenticated same-origin and uses version cursor', async () => {
  const calls = [];
  const client = createCloudClient({
    fetcher: async (...args) => {
      calls.push(args);
      return new Response('{"items":[]}');
    },
  });
  client.setAccount('superdan');
  await client.projectActivity('project/id', { beforeVersion: 15 });
  assert.equal(calls[0][0], '/v1/projects/project%2Fid/activity?limit=50&before_version=15');
  assert.equal(calls[0][1].headers['X-Expected-Account'], 'superdan');
  assert.equal(calls[0][1].method, 'GET');
});
