import test from 'node:test';
import assert from 'node:assert/strict';
import { reportCatalogs } from './i18n/reportCatalogs.js';
import {
  REPORT_CATEGORIES,
  reportBody,
  reportPage,
  reportProblemKey,
  reportShortId,
} from './report-problem.js';

test('a report carries the path and view, never the rest of the address', () => {
  assert.equal(
    reportPage({ pathname: '/', search: '?view=workspace&project=p1&token=secret' }),
    '/?view=workspace',
  );
  assert.equal(reportPage({ pathname: '/trash', search: '' }), '/trash');
});

test('a shot is sent only with its story', () => {
  const base = { category: 'page', text: ' it broke ', page: '/', client: 'x' };
  assert.equal(reportBody(base).summary, 'it broke');
  assert.equal(reportBody(base).source, 'web');
  assert.equal(reportBody({ ...base, shotId: 's' }).shot_id, undefined);
  const inStory = reportBody({ ...base, projectId: 'p', shotId: 's' });
  assert.equal(inStory.project_id, 'p');
  assert.equal(inStory.shot_id, 's');
});

test('every category and both languages have words; refusals have their own message', () => {
  for (const name of REPORT_CATEGORIES)
    for (const locale of ['en', 'zh']) assert.ok(reportCatalogs[locale]['report.category.' + name]);
  assert.deepEqual(Object.keys(reportCatalogs.en).sort(), Object.keys(reportCatalogs.zh).sort());
  assert.equal(reportProblemKey({ status: 429 }), 'report.tooMany');
  assert.equal(reportProblemKey({ status: 422 }), 'report.privateText');
  assert.equal(reportProblemKey({ status: 500 }), 'report.failed');
  assert.equal(reportShortId('abcdef1234'), 'ABCDEF12');
});
