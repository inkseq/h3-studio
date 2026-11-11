import test from 'node:test';
import assert from 'node:assert/strict';
import { readFeedbackSettings, saveFeedbackSettings, sendFeedback } from './api/feedback.js';

// The wire contract of the feedback area: what each function asks the transport for.
function recorder(answer) {
  const calls = [];
  return {
    calls,
    request: async (path, options) => {
      calls.push([path, options]);
      return answer;
    },
  };
}

test('a problem report is one keyed POST and returns the server receipt', async () => {
  const client = recorder({ report_id: 'r-1' });
  const body = { category: 'page', summary: 'text', source: 'web' };
  assert.deepEqual(await sendFeedback(client, body, 'key-1'), { report_id: 'r-1' });
  assert.deepEqual(client.calls, [['/v1/feedback', { method: 'POST', key: 'key-1', body }]]);
});

test('feedback settings are read with a plain GET', async () => {
  const client = recorder({ revision: 3, enabled: true });
  assert.deepEqual(await readFeedbackSettings(client), { revision: 3, enabled: true });
  assert.deepEqual(client.calls, [['/v1/feedback/settings', undefined]]);
});

test('saving feedback settings sends the revision it was read at', async () => {
  const client = recorder({ revision: 4, enabled: false });
  await saveFeedbackSettings(client, { enabled: false, revision: 3 }, 'key-2');
  assert.deepEqual(client.calls, [
    [
      '/v1/feedback/settings',
      { method: 'PATCH', body: { expected_revision: 3, enabled: false }, key: 'key-2' },
    ],
  ]);
});
