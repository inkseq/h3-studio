import test from 'node:test';
import assert from 'node:assert/strict';
import {
  draftFromRules,
  parseRules,
  refusalCode,
  ruleTargets,
  sameRules,
  targetLabels,
} from './start-rules-model.js';

test('one start-rule target per exact pool, configuration and mode', () => {
  const profile = (id, mode, configuration_id = 'cfg', pool = 'vast') => ({
    id,
    mode,
    configuration_id,
    pool,
    model_id: 'wan',
  });
  const targets = ruleTargets([
    profile('a', 'ref'),
    profile('b', 'ref'),
    profile('c', 'fl'),
    profile('d', 'ref', 'cfg2'),
    { id: 'x', mode: 'ref' },
  ]);
  assert.deepEqual(
    targets.map((item) => item.id),
    ['c', 'a', 'd'],
  );
});

test('start rules accept only whole numbers inside the server bounds', () => {
  const draft = draftFromRules({ job_seconds: 30, boot_seconds: 600, minimum_jobs: 20 });
  assert.deepEqual(draft, { job_seconds: '30', boot_seconds: '600', minimum_jobs: '20' });
  assert.deepEqual(parseRules(draft).value, {
    job_seconds: 30,
    boot_seconds: 600,
    minimum_jobs: 20,
  });
  for (const bad of ['0', '1.5', '-3', '', '86401', 'abc'])
    assert.equal(parseRules({ ...draft, job_seconds: bad }).error.id, 'job_seconds', bad);
  assert.equal(parseRules({ ...draft, minimum_jobs: '10001' }).error.id, 'minimum_jobs');
  assert.ok(
    sameRules(parseRules(draft).value, { job_seconds: 30, boot_seconds: 600, minimum_jobs: 20 }),
  );
  assert.ok(
    !sameRules(parseRules(draft).value, { job_seconds: 31, boot_seconds: 600, minimum_jobs: 20 }),
  );
});

test('start-rule refusal codes read both FastAPI shapes', () => {
  assert.equal(refusalCode({ detail: { detail: 'start_rules_conflict' } }), 'start_rules_conflict');
  assert.equal(refusalCode({ detail: { code: 'start_rules_invalid' } }), 'start_rules_invalid');
  assert.equal(refusalCode(new Error('x')), '');
});

test('start-rule targets are named by what differs', () => {
  const item = (id, pool, configuration_id) => ({
    id,
    pool,
    configuration_id,
    mode: 'ref',
    model_id: 'm',
    gpu_names: ['RTX5090'],
  });
  assert.deepEqual(targetLabels([item('a', 'vast', 'x'), item('b', 'lium', 'y')]), {
    shared: 'm · REF · RTX5090',
    labels: { a: 'vast', b: 'lium' },
  });
  assert.deepEqual(targetLabels([item('a', 'vast', 'x'), item('b', 'vast', 'y')]), {
    shared: 'm · REF · RTX5090 · vast',
    labels: { a: 'x', b: 'y' },
  });
  assert.deepEqual(targetLabels([item('a', 'vast', 'x')]), {
    shared: '',
    labels: { a: 'm · REF · RTX5090 · vast' },
  });
});
