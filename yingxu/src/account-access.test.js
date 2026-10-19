import test from 'node:test';
import assert from 'node:assert/strict';
import { accessFrom, readAccess, resetAccess } from './account-access.js';

test('anonymous or failed reads expose nothing privileged', () => {
  assert.deepEqual(accessFrom(null), { signedIn: false, gpus: false, admin: false });
  assert.deepEqual(accessFrom({}), { signedIn: false, gpus: false, admin: false });
});

test('GPUs follow operator_capacity.view and admin follows operator_capacity.admin', () => {
  assert.deepEqual(
    accessFrom({ username: 'supervan', operator_capacity: { view: true, admin: false } }),
    {
      signedIn: true,
      gpus: true,
      admin: false,
    },
  );
  assert.deepEqual(accessFrom({ username: 'mia', operator_capacity: { view: false } }), {
    signedIn: true,
    gpus: false,
    admin: false,
  });
});

test('machine principals (agent keys) never get GPU or admin navigation', () => {
  assert.deepEqual(
    accessFrom({
      username: 'superdan',
      machine: true,
      operator_capacity: { view: true, admin: true },
    }),
    {
      signedIn: true,
      gpus: false,
      admin: false,
    },
  );
});

test('one cached read per page load; errors fall back to no access', async () => {
  resetAccess();
  let calls = 0;
  const failing = () => {
    calls++;
    return Promise.reject(new Error('offline'));
  };
  assert.deepEqual(await readAccess(failing), { signedIn: false, gpus: false, admin: false });
  await readAccess(failing);
  assert.equal(calls, 1);
  resetAccess();
  const ok = () =>
    Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ username: 'a', operator_capacity: { view: true } }),
    });
  assert.equal((await readAccess(ok)).gpus, true);
  resetAccess();
});
