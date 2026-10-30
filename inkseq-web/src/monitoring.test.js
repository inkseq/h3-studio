import test from 'node:test';
import assert from 'node:assert/strict';
import { monitoringEnvironment, scrubBreadcrumb } from './monitoring.js';

test('only the deployed hosts report errors', () => {
  assert.equal(monitoringEnvironment('inkseq.com'), 'production');
  assert.equal(monitoringEnvironment('staging.inkseq.com'), 'staging');
  for (const host of ['localhost', '127.0.0.1', 'preview.inkseq.com', undefined])
    assert.equal(monitoringEnvironment(host), null);
});

test('breadcrumbs never carry typed text or request bodies', () => {
  assert.equal(scrubBreadcrumb({ category: 'console', message: 'prompt: secret' }), null);
  assert.equal(scrubBreadcrumb({ category: 'ui.input', message: 'textarea' }), null);
  const fetched = scrubBreadcrumb({
    category: 'fetch',
    data: { method: 'POST', status_code: 500, url: '/v1/story-chats/x/turns?a=1', body: 'hi' },
  });
  assert.deepEqual(fetched.data, {
    method: 'POST',
    status_code: 500,
    url: '/v1/story-chats/x/turns',
  });
  const nav = { category: 'navigation', data: { from: '/', to: '/works' } };
  assert.equal(scrubBreadcrumb(nav), nav);
});
