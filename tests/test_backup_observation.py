"""Offline receipt, failure privacy and operator-notification boundaries (#655)."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

from deploy.platform import alert_monitor as monitor
from studio_platform.alert_conditions import evaluate
from test_business_backup_schedule import host


class BackupObservationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path('/synthetic-backup')
        self.files = {}
        self.guard = patch.object(monitor, 'protected_directory')
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.reader = patch.object(
            monitor, 'read_protected_bytes', side_effect=self.read
        )
        self.read_mock = self.reader.start()
        self.addCleanup(self.reader.stop)

    def read(self, path, *, missing=False, private=True):
        if path in self.files:
            return self.files[path]
        if missing:
            return None
        raise FileNotFoundError

    def completed(self, day):
        receipt = dict(
            format='sixnine-private-backup-copy-v3',
            phase='copied_not_restored',
            completion_sha256='a' * 64,
            completion_version_id='synthetic-version',
            restore_verified=False,
        )
        raw = json.dumps(receipt).encode()
        done = dict(
            version=3,
            day=day,
            receipt_sha256=hashlib.sha256(raw).hexdigest(),
            completion_sha256='a' * 64,
            completion_version_id='synthetic-version',
            restore_verified=False,
        )
        self.files[self.root / day / 'receipt.json'] = raw
        self.files[self.root / day / 'completed.json'] = json.dumps(done).encode()

    def observe(self, clock):
        now = datetime.fromisoformat(clock).replace(tzinfo=timezone.utc).timestamp()
        return monitor.backup_copy_missing(now=now, root=self.root)

    def test_daily_deadline_and_midnight_do_not_require_premature_backup(self):
        self.completed('2026-10-25')
        self.assertFalse(self.observe('2026-10-26T00:01:00'))
        self.assertFalse(self.observe('2026-10-26T17:59:59'))
        self.assertTrue(self.observe('2026-10-26T18:00:00'))
        self.completed('2026-10-26')
        self.assertFalse(self.observe('2026-10-26T18:00:00'))
        self.assertFalse(self.observe('2026-10-27T00:00:00'))

    def test_no_copy_and_partial_intent_never_pass_and_future_day_is_ignored(self):
        self.files[self.root / '2026-10-26' / 'intent.json'] = b'{}'
        self.completed('2026-10-27')
        self.assertTrue(self.observe('2026-10-26T17:00:00'))
        self.assertEqual(self.read_mock.call_count, 2)
        self.assertTrue(
            all(
                call.args[0].name == 'completed.json'
                for call in self.read_mock.call_args_list
            )
        )

    def test_corrupt_hash_unavailable_or_mismatched_receipt_is_unknown(self):
        self.completed('2026-10-26')
        path = self.root / '2026-10-26' / 'completed.json'
        original = self.files[path]
        for delta in (
            {'receipt_sha256': 'b' * 64},
            {'completion_sha256': 'b' * 64},
            {'completion_version_id': 'other'},
            {'day': '2026-10-27'},
            {'version': True},
        ):
            with self.subTest(delta=delta):
                self.files[path] = json.dumps(
                    {**json.loads(original), **delta}
                ).encode()
                self.assertIsNone(self.observe('2026-10-26T18:00:00'))
        self.files[path] = original
        self.files.pop(self.root / '2026-10-26' / 'receipt.json')
        self.assertIsNone(self.observe('2026-10-26T18:00:00'))
        self.files[path] = b'PRIVATE_INVALID_JSON'
        self.assertIsNone(self.observe('2026-10-26T18:00:00'))
        with patch.object(monitor, 'protected_directory', side_effect=PermissionError):
            self.assertIsNone(self.observe('2026-10-26T18:00:00'))

    def test_observation_is_independent_of_app_database_and_never_runs_backup(self):
        runner = Mock(
            return_value=Mock(
                returncode=0,
                stdout=(
                    b'LoadState=loaded\nActiveState=failed\nSubState=failed\n'
                    b'Result=exit-code\nExecMainCode=1\nExecMainStatus=1\n'
                ),
            )
        )
        with patch.object(monitor, 'backup_copy_missing', return_value=None):
            result = monitor.backup_observation(now=1, runner=runner)
        self.assertEqual(
            result,
            dict(
                business_backup_failed=True,
                business_backup_missing=None,
                business_backup_unobservable=True,
            ),
        )
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(
            runner.call_args.args[0][:3],
            ['/usr/bin/systemctl', 'show', 'inkseq-business-backup.service'],
        )

    def test_backup_alerts_deduplicate_and_unknown_does_not_resolve(self):
        for code in (
            'business_backup_failed',
            'business_backup_missing',
            'business_backup_unobservable',
        ):
            first = evaluate(
                None, None, environment='production', now=1000, host={code: True}
            )
            self.assertEqual(len(first['notifications']), 1)
            message = first['notifications'][0]
            self.assertEqual((message['code'], message['transition']), (code, 'firing'))
            monitor.notification(message, 'production')
            state = json.loads(json.dumps(first['state']))
            for value in (True, None, 'PRIVATE_INVALID_VALUE'):
                observed = evaluate(
                    None, state, environment='production', now=1060, host={code: value}
                )
                self.assertEqual(observed['notifications'], [])
                state = observed['state']
            resolved = evaluate(
                None, state, environment='production', now=1120, host={code: False}
            )
            self.assertEqual(resolved['notifications'][0]['transition'], 'resolved')


class BackupDiagnosticsTests(unittest.TestCase):
    def test_unknown_phase_records_no_secrets_and_never_mints_completion(self):
        records = {}

        def record(path, value):
            if path.name in records:
                raise FileExistsError
            records[path.name] = value

        operation = Path('/synthetic-operation')
        with patch.object(host, 'record', side_effect=record):
            with self.assertRaises(subprocess.TimeoutExpired):
                with host.phase(operation, 'snapshot'):
                    raise subprocess.TimeoutExpired(
                        'PRIVATE_COMMAND', 900, output='SECRET'
                    )
            self.assertEqual(
                set(records),
                {'phase-snapshot-started.json', 'phase-snapshot-unknown.json'},
            )
            self.assertEqual(
                records['phase-snapshot-unknown.json']['code'],
                'backup_phase_timeout_outcome_unknown',
            )
            entered = False
            with self.assertRaises(FileExistsError):
                with host.phase(operation, 'snapshot'):
                    entered = True
            self.assertFalse(entered)
        encoded = json.dumps(records)
        self.assertNotIn('SECRET', encoded)
        self.assertNotIn('PRIVATE_COMMAND', encoded)
        self.assertFalse(any('completed' in name for name in records))

    def test_finite_error_codes_do_not_echo_arbitrary_exceptions(self):
        self.assertEqual(
            host.diagnostic_code(
                ValueError('backup_prior_outcome_requires_reconciliation')
            ),
            'backup_prior_outcome_requires_reconciliation',
        )
        for error in (
            ValueError('PRIVATE'),
            ValueError({'SECRET': 1}),
            OSError('PRIVATE'),
            RuntimeError('backup_disk_reserve_required'),
        ):
            self.assertEqual(host.diagnostic_code(error), 'backup_failed_or_unknown')

    def test_successful_phase_is_not_copy_or_restore_proof(self):
        record = Mock()
        with patch.object(host, 'record', record):
            with host.phase(Path('/synthetic'), 'handoff'):
                pass
        self.assertEqual(
            [call.args[0].name for call in record.call_args_list],
            ['phase-handoff-started.json', 'phase-handoff-returned.json'],
        )

    def test_hosted_coverage_uses_payload_reader_after_transport_freeze(self):
        with (
            patch.object(Path, 'exists', return_value=True),
            patch.object(
                host, 'read', side_effect=AssertionError('root metadata reader')
            ),
            patch.object(
                host,
                'retention_json',
                return_value={'hosted_users': {'status': 'complete'}},
            ) as reader,
        ):
            host.require_hosted_coverage(Path('/synthetic'))
            reader.assert_called_once_with(Path('/synthetic/snapshot/manifest.json'))
            reader.return_value = {'hosted_users': {'status': 'unavailable'}}
            with self.assertRaisesRegex(ValueError, 'hosted_users_unavailable'):
                host.require_hosted_coverage(Path('/synthetic'))


if __name__ == '__main__':
    unittest.main()
