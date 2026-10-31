import test from 'node:test';
import assert from 'node:assert/strict';
import { formatEnd, maintenanceRefusal, noticeState } from './maintenance-notice.js';

const now = Date.parse('2026-10-23T09:00:00Z');
const at = (iso) => Date.parse(iso) / 1000;

test('nothing shows unless the server says the notice is active', () => {
  assert.equal(noticeState(null, now), null);
  assert.equal(noticeState({}, now), null);
  assert.equal(noticeState({ maintenance_notice: { active: false } }, now), null);
  assert.equal(noticeState({ maintenance_notice: { active: 'yes' } }, now), null);
});

test('an active notice carries its end time and turns overdue once it passes', () => {
  const config = {
    maintenance_notice: {
      active: true,
      message: '  Database upgrade ',
      expected_end_at: at('2026-10-23T09:30:00Z'),
      severity: 'info',
    },
  };
  assert.deepEqual(noticeState(config, now), {
    message: 'Database upgrade',
    severity: 'info',
    endsAt: Date.parse('2026-10-23T09:30:00Z'),
    overdue: false,
  });
  assert.equal(noticeState(config, Date.parse('2026-10-23T09:31:00Z')).overdue, true);
  assert.equal(
    noticeState({ maintenance_notice: { active: true, expected_end_at: 'later' } }, now).endsAt,
    null,
  );
});

test('the end time is shown without a date on the same day', () => {
  const end = Date.parse('2026-10-23T09:30:00Z');
  assert.doesNotMatch(formatEnd(end, 'en', end - 60_000), /Oct/);
  assert.match(formatEnd(end + 3 * 86_400_000, 'en', end), /Oct/);
  assert.equal(formatEnd(null, 'en', now), null);
});

test('a refusal during the window is recognised, top-level or nested', () => {
  const end = at('2026-10-23T09:30:00Z');
  assert.deepEqual(maintenanceRefusal({ code: 'maintenance_in_progress', expected_end_at: end }), {
    endsAt: end * 1000,
  });
  assert.deepEqual(
    maintenanceRefusal({ detail: { code: 'maintenance_in_progress', expected_end_at: end } }),
    { endsAt: end * 1000 },
  );
  assert.equal(maintenanceRefusal({ code: 'insufficient_credits' }), null);
});
