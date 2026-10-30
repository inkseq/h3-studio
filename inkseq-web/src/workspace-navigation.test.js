import test from 'node:test';
import assert from 'node:assert/strict';

test('legacy Freestyle routes into the same v2 project/entity/activity without copying a draft', async () => {
  const { freestyleWorkspaceHref } = await import('./workspace-navigation.js');
  const state = {
    workspace: { mode: 'cloud', dirty: true },
    project: { id: 'kept-project', entities: [{ id: 'local-edit' }] },
    selectedId: 'local-edit',
  };
  const before = JSON.stringify(state);
  assert.equal(
    freestyleWorkspaceHref('?project=original&entity=shot&panel=activity&secret=discard', state),
    '/?project=original&entity=shot&panel=activity&view=workspace',
  );
  assert.equal(
    freestyleWorkspaceHref('', state),
    '/?project=kept-project&entity=local-edit&view=workspace',
  );
  assert.equal(freestyleWorkspaceHref('', { workspace: { mode: 'local' } }), '/?view=workspace');
  assert.equal(freestyleWorkspaceHref('?project=one&project=two', state), null);
  assert.equal(JSON.stringify(state), before);
});
