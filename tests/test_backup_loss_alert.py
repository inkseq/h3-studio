"""Original coverage, unknown evidence and deduplicated delivery; no AWS writes."""

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from deploy.platform import alert_monitor as monitor
from studio_platform.alert_conditions import evaluate


class BackupLossTests(unittest.TestCase):
    def coverage(self, unexpected=22, expected=2):
        missing = unexpected + expected
        return {
            'status': 'incomplete' if missing else 'complete',
            'referenced_objects': 2420 + missing,
            'available_objects': 2420,
            'missing_objects': missing,
            'referenced_bytes': 10000000 + missing * 100,
            'available_bytes': 10000000,
            'missing_bytes': missing * 100,
            'expected_missing_objects': expected,
            'expected_missing_bytes': expected * 100,
            'unexpected_missing_objects': unexpected,
            'unexpected_missing_bytes': unexpected * 100,
            'loss_alert_required': unexpected > 0,
        }

    def evidence(self, version=3, coverage=None):
        receipt = {
            'phase': 'copied_not_restored',
            'format': 'sixnine-private-backup-copy-v' + str(version),
            'completion_sha256': 'a' * 64,
            'completion_version_id': 'original-version',
        }
        if version != 1:
            receipt['coverage'] = self.coverage() if coverage is None else coverage
            if version == 2:
                receipt['coverage'] = {
                    key: value
                    for key, value in receipt['coverage'].items()
                    if not key.startswith(('expected_', 'unexpected_'))
                    and key != 'loss_alert_required'
                }
        completed = {
            'version': version,
            'day': '2026-10-28',
            'completion_sha256': receipt['completion_sha256'],
            'completion_version_id': receipt['completion_version_id'],
        }
        if version != 1:
            completed['coverage'] = copy.deepcopy(receipt['coverage'])
        return completed, receipt

    def observe(self, completed, receipt, *, tamper_hash=False, day=28):
        raw = json.dumps(receipt).encode()
        completed['receipt_sha256'] = (
            'b' * 64 if tamper_hash else hashlib.sha256(raw).hexdigest()
        )
        with (
            patch.object(monitor, 'protected_directory'),
            patch.object(monitor, 'read_protected', return_value=completed),
            patch.object(monitor, 'read_protected_bytes', return_value=raw),
        ):
            return monitor.backup_copy_observation(
                now=datetime(2026, 10, day, 19, tzinfo=timezone.utc).timestamp(),
                root=Path('/synthetic-backup'),
            )

    def test_successful_copy_with_unexpected_loss_is_not_missing_or_healthy(self):
        self.assertEqual(
            self.observe(*self.evidence()), {'missing': False, 'loss': True}
        )

    def test_expected_only_v3_and_complete_legacy_copy_do_not_raise_loss(self):
        for version, coverage in (
            (3, self.coverage(0)),
            (3, self.coverage(0, 0)),
            (1, None),
        ):
            with self.subTest(version=version, coverage=coverage):
                self.assertEqual(
                    self.observe(*self.evidence(version, coverage)),
                    {'missing': False, 'loss': False},
                )

    def test_older_v2_missing_bytes_are_actionable_without_invented_classification(
        self,
    ):
        self.assertTrue(self.observe(*self.evidence(2))['loss'])
        self.assertFalse(self.observe(*self.evidence(2, self.coverage(0, 0)))['loss'])

    def test_hash_mismatch_or_corrupt_coverage_is_unknown_not_recovery(self):
        for changed in (
            'hash',
            'summary',
            'receipt_boolean',
            'negative',
            'boolean',
            'false_flag',
            'total',
            'extra',
            'missing',
        ):
            completed, receipt = self.evidence()
            if changed == 'summary':
                completed['coverage'] = self.coverage(0)
            elif changed == 'receipt_boolean':
                receipt['coverage']['available_objects'] = True
            elif changed == 'missing':
                receipt.pop('coverage')
                completed.pop('coverage')
            elif changed != 'hash':
                key, value = {
                    'negative': ('missing_bytes', -1),
                    'boolean': ('unexpected_missing_objects', True),
                    'false_flag': ('loss_alert_required', False),
                    'total': ('referenced_objects', 1),
                    'extra': ('unreviewed', 1),
                }[changed]
                receipt['coverage'][key] = value
                completed['coverage'][key] = value
            with self.subTest(changed=changed):
                self.assertEqual(
                    self.observe(completed, receipt, tamper_hash=changed == 'hash'),
                    {'missing': None, 'loss': None},
                )

    def test_missing_copy_keeps_loss_unknown(self):
        with (
            patch.object(monitor, 'protected_directory'),
            patch.object(monitor, 'read_protected', return_value=None),
        ):
            self.assertEqual(
                monitor.backup_copy_observation(now=1800000000),
                {'missing': True, 'loss': None},
            )

    def test_new_daily_receipts_do_not_repeat_the_same_active_loss_incident(self):
        state = None
        notifications = []
        for day in (28, 29, 30):
            completed, receipt = self.evidence()
            completed['day'] = f'2026-10-{day}'
            completed['completion_version_id'] = f'synthetic-day-{day}'
            receipt['completion_version_id'] = completed['completion_version_id']
            observed = self.observe(completed, receipt, day=day)
            self.assertEqual(observed, {'missing': False, 'loss': True})
            result = evaluate(
                None,
                state,
                environment='production',
                now=datetime(2026, 10, day, 19, tzinfo=timezone.utc).timestamp(),
                host={'business_backup_loss': observed['loss']},
            )
            notifications.extend(result['notifications'])
            state = json.loads(json.dumps(result['state']))
        self.assertEqual(
            [(item['code'], item['transition']) for item in notifications],
            [('business_backup_loss', 'firing')],
        )

    def test_unknown_does_not_clear_and_restart_does_not_duplicate_loss(self):
        sns, cloud = Mock(), Mock()
        sns.publish.return_value = {'MessageId': 'synthetic-ack'}
        state = monitor.initial_state('staging')
        settings = {
            'environment': 'staging',
            'instance_id': 'i-07805df7bd6984bd9',
            'topic_arn': 'arn:aws:sns:ap-southeast-1:829135631045:sixnine-staging-operator-alerts',
            'thresholds': {},
        }
        for now, loss in ((1000, True), (1060, True), (1120, None), (1180, False)):
            state = monitor.tick(
                settings,
                json.loads(json.dumps(state)),
                None,
                {'business_backup_loss': loss},
                None,
                evaluator=evaluate,
                sns=sns,
                cloudwatch=cloud,
                save=lambda value: None,
                now=now,
            )
            self.assertEqual(sns.publish.call_count, 2 if now == 1180 else 1)
        messages = [
            json.loads(call.kwargs['Message']) for call in sns.publish.call_args_list
        ]
        self.assertEqual(
            [(item['code'], item['transition']) for item in messages],
            [('business_backup_loss', 'firing'), ('business_backup_loss', 'resolved')],
        )
        self.assertNotIn('object', json.dumps(messages))


if __name__ == '__main__':
    unittest.main()
