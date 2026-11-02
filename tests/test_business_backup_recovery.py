"""Original-operation recovery is one shot, fenced, and never a fresh snapshot."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_business_backup_schedule as schedule

host = schedule.load('business_backup')
with patch.dict(sys.modules, {'business_backup': host}):
    recovery = schedule.load('business_backup_recovery')


@unittest.skipUnless(
    sys.platform == 'linux', 'Linux protected-host filesystem semantics'
)
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.operation = Path(self.temp.name) / recovery.DAY
        self.operation.mkdir(mode=0o700)
        self.intent = {
            'version': 3,
            'day': recovery.DAY,
            'commit': recovery.ORIGINAL_COMMIT,
            'image': recovery.ORIGINAL_IMAGE,
            'operation_id': recovery.NONCE,
            'automatic_retry': False,
        }
        host.record(self.operation / 'intent.json', self.intent)
        self.cfg = schedule.ScheduleTests().config()
        self.coverage = {
            'status': 'incomplete',
            'referenced_objects': 1937,
            'available_objects': 1935,
            'missing_objects': 2,
            'referenced_bytes': 1332124577,
            'available_bytes': 1330138098,
            'missing_bytes': 1986479,
            'expected_missing_objects': 2,
            'expected_missing_bytes': 1986479,
            'unexpected_missing_objects': 0,
            'unexpected_missing_bytes': 0,
            'loss_alert_required': False,
        }
        self.evidence = {
            'version': 3,
            'files': 1937,
            'bytes': recovery.TOTAL_BYTES,
            'manifest_sha256': recovery.MANIFEST_SHA,
            'coverage': self.coverage,
        }
        self.receipt = {
            'format': 'sixnine-private-backup-copy-v3',
            'target': {
                **self.cfg['target'],
                'prefix': 'business-backups/' + recovery.NONCE,
            },
            'phase': 'copied_not_restored',
            'completion_version_id': 'original-version',
            'completion_sha256': 'a' * 64,
            'completion_bytes': 12345,
            'snapshot_manifest_sha256': recovery.MANIFEST_SHA,
            'objects': 1935,
            'bytes': recovery.TOTAL_BYTES,
            'restore_verified': False,
            'coverage': self.coverage,
        }
        for p in (
            patch.object(host, 'protected', side_effect=lambda p, *a: Path(p)),
            patch.object(host.os, 'chown', create=True),
            patch.object(host, 'freeze_transport_snapshot'),
            patch.object(host, 'copy_receipt', side_effect=lambda *a: self.receipt),
            patch.object(recovery, 'quiet'),
            patch.object(recovery, 'verify', return_value=self.evidence),
        ):
            p.start()
            self.addCleanup(p.stop)

    def fake_docker(self, args, **kwargs):
        if args[0] == 'run':
            self.assertIn('--instance-role-only', args)
            self.assertIn('--allow-incomplete', args)
            self.assertIn('copy', args)
            self.assertNotIn('configured', args)
            p = self.operation / 'output' / 'transfer'
            p.mkdir(mode=0o700)
            host.record(p / 'completion.json', self.receipt)
        return ''

    def test_success_retains_snapshot_date_commit_prefix_and_never_deletes(self):
        original = (self.operation / 'intent.json').read_bytes()
        with (
            patch.object(host, 'docker', side_effect=self.fake_docker) as docker,
            patch.object(host, 'retain_backups') as retention,
        ):
            result = recovery.transfer_original(self.operation, self.cfg, self.evidence)
        self.assertTrue(result['new_daily_copy_required'])
        self.assertEqual((self.operation / 'intent.json').read_bytes(), original)
        complete = host.read(self.operation / 'completed.json')
        self.assertEqual(complete['day'], recovery.DAY)
        self.assertEqual(complete['commit'], recovery.ORIGINAL_COMMIT)
        self.assertFalse(complete['restore_verified'])
        self.assertEqual(
            complete['receipt_sha256'],
            hashlib.sha256((self.operation / 'receipt.json').read_bytes()).hexdigest(),
        )
        self.assertEqual(docker.call_count, 2)  # Handoff and original-prefix copy only.
        retention.assert_not_called()
        self.assertEqual(
            host.read(self.operation / 'target.json')['prefix'],
            'business-backups/' + recovery.NONCE,
        )

    def test_existing_partial_or_completed_state_refuses_before_any_command(self):
        for name in (
            'recovery-intent.json',
            'output',
            'snapshot',
            'completed.json',
            'target.json',
        ):
            p = self.operation / name
            p.write_text('{}')
            with patch.object(host, 'docker') as docker:
                with self.assertRaisesRegex(ValueError, 'prior_outcome'):
                    recovery.transfer_original(self.operation, self.cfg, self.evidence)
                docker.assert_not_called()
            p.unlink()

    def test_unknown_handoff_or_transfer_never_replays(self):
        for fail_at in ('cp', 'run'):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as d:
                operation = Path(d) / recovery.DAY
                operation.mkdir(mode=0o700)
                host.record(operation / 'intent.json', self.intent)

                def fail(args, **kwargs):
                    if args[0] == fail_at:
                        raise subprocess.TimeoutExpired('private-arguments', 900)
                    return ''

                with patch.object(host, 'docker', side_effect=fail):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        recovery.transfer_original(operation, self.cfg, self.evidence)
                self.assertTrue((operation / 'recovery-intent.json').exists())
                self.assertFalse((operation / 'completed.json').exists())
                with patch.object(host, 'docker') as docker:
                    with self.assertRaisesRegex(ValueError, 'prior_outcome'):
                        recovery.transfer_original(operation, self.cfg, self.evidence)
                    docker.assert_not_called()

    def test_changed_receipt_cannot_complete_original(self):
        self.receipt['snapshot_manifest_sha256'] = 'b' * 64
        with patch.object(host, 'docker', side_effect=self.fake_docker):
            with self.assertRaisesRegex(ValueError, 'receipt_changed'):
                recovery.transfer_original(self.operation, self.cfg, self.evidence)
        self.assertFalse((self.operation / 'completed.json').exists())
        self.assertTrue(
            (self.operation / 'output' / 'transfer' / 'completion.json').exists()
        )

    def test_changed_original_identity_cannot_start(self):
        p = self.operation / 'intent.json'
        p.write_text(json.dumps({**self.intent, 'commit': 'c' * 40}))
        with patch.object(host, 'docker') as docker:
            with self.assertRaisesRegex(ValueError, 'identity_changed'):
                recovery.transfer_original(self.operation, self.cfg, self.evidence)
            docker.assert_not_called()

    def test_preserve_daily_skips_retention_but_checks_hosted_coverage(self):
        with (
            patch.object(host, 'retention_after_success') as retention,
            patch.object(host, 'require_hosted_coverage') as hosted,
        ):
            result = host.finish_daily(
                self.operation,
                {'state': 'copied_not_restored'},
                self.cfg,
                'image',
                preserve=True,
            )
        retention.assert_not_called()
        hosted.assert_called_once_with(self.operation)
        self.assertEqual(result['retention']['state'], 'preserved_by_operator')


class RecoveryGateTests(unittest.TestCase):
    def test_quiet_process_query_includes_docker_required_pid_column(self):
        def docker(args, **kwargs):
            if args[0] == 'exec':
                return json.dumps({'nonterminal_jobs': 0})
            if args[0] == 'top':
                self.assertEqual(args, ['top', recovery.APP, '-eo', 'pid,args'])
                return 'PID COMMAND\n123 python app'
            return 'sixnine-platform-app-1'

        with patch.object(host, 'docker', side_effect=docker):
            recovery.quiet()

    def test_busy_or_unknown_work_rejects_without_stop_or_cancel(self):
        for value in (1, True, None):
            with patch.object(
                host, 'docker', return_value=json.dumps({'nonterminal_jobs': value})
            ) as docker:
                with self.assertRaisesRegex(ValueError, 'zero_work'):
                    recovery.quiet()
                self.assertEqual(docker.call_count, 1)

    def test_active_snapshot_or_retained_transport_blocks_replay(self):
        for replies in (
            (json.dumps({'nonterminal_jobs': 0}), 'studio_platform.backup_cli'),
            (
                json.dumps({'nonterminal_jobs': 0}),
                'python app',
                'inkseq-backup-transfer-' + recovery.NONCE,
            ),
        ):
            with patch.object(host, 'docker', side_effect=replies):
                with self.assertRaises(ValueError):
                    recovery.quiet()

    def test_transport_verifies_offline_and_rejects_changed_inventory(self):
        evidence = {'version': 3, 'files': 0, 'bytes': 0, 'manifest_sha256': 'f' * 64}
        with patch.object(host, 'docker', return_value=json.dumps(evidence)) as docker:
            with self.assertRaisesRegex(ValueError, 'snapshot_changed'):
                recovery.verify(Path('/synthetic'))
        args = docker.call_args.args[0]
        self.assertEqual(args[args.index('--network') + 1], 'none')
        self.assertIn('--read-only', args)
        self.assertEqual(docker.call_args.kwargs['timeout'], 600)

    def test_snapshot_wait_has_headroom_inside_whole_service_budget(self):
        self.assertGreaterEqual(host.SNAPSHOT_TIMEOUT, 2 * 1010)
        service = (
            schedule.ROOT / 'deploy/platform/inkseq-business-backup.service'
        ).read_text()
        self.assertIn('business_backup.py
', service)
        self.assertNotIn('--daily-preserve', service)
        limit = int(
            next(
                x.split('=')[1]
                for x in service.splitlines()
                if x.startswith('TimeoutStartSec=')
            )
        )
        self.assertGreaterEqual(limit, host.SNAPSHOT_TIMEOUT + 900 + 1800 + 600)
        self.assertLessEqual(limit, 7200)


@unittest.skipUnless(sys.platform == 'linux', 'Linux release-lock semantics')
class RecoveryEntryGuardTests(unittest.TestCase):
    """Exercise the public qualifier/execute entry, not just its transfer helper."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'release.lock').touch()
        self.state = dict(current=recovery.TRANSPORT_COMMIT, status='app_ready')
        self.identity = dict(
            image=recovery.TRANSPORT_IMAGE,
            running=True,
            mounts=[
                dict(
                    Destination='/data',
                    Source=str(self.root / 'platform-data'),
                    Type='bind',
                )
            ],
        )
        self.release = Mock()
        self.transfer = Mock(return_value={'state': 'synthetic_completed'})
        self.quiet = Mock()
        self.verify = Mock(return_value={'manifest_sha256': recovery.MANIFEST_SHA})
        self.docker = Mock(side_effect=lambda *a, **k: json.dumps(self.identity))
        self.approved_image = Mock()
        self.record = Mock(side_effect=AssertionError('unexpected operation write'))
        cfg = dict(version=3, environment='production')
        for item in (
            patch.object(host, 'ROOT', self.root),
            patch.object(host, 'STATE', self.root / 'operations'),
            patch.object(host, 'protected', side_effect=lambda p, *a: Path(p)),
            patch.object(host.os, 'geteuid', return_value=0),
            patch.object(
                host,
                'read',
                side_effect=lambda p: (
                    self.state if p.name == 'release-state.json' else cfg
                ),
            ),
            patch.object(host, 'validate_config', return_value=cfg),
            patch.object(Path, 'read_text', return_value='synthetic-host'),
            patch.object(host, 'approved_image', self.approved_image),
            patch.object(host, 'docker', self.docker),
            patch.object(host, 'record', self.record),
            patch.object(
                host, 'retention_json', return_value={'sha256': recovery.MANIFEST_SHA}
            ),
            patch.object(
                host.shutil, 'disk_usage', return_value=Mock(free=10 * 1024**3)
            ),
            patch.object(recovery, 'pristine'),
            patch.object(recovery, 'quiet', self.quiet),
            patch.object(recovery, 'verify', self.verify),
            patch.object(recovery, 'transfer_original', self.transfer),
            patch.dict(sys.modules, {'release': self.release}),
            patch.object(sys, 'path', list(sys.path)),
        ):
            item.start()
            self.addCleanup(item.stop)

    def assert_untouched(self):
        self.transfer.assert_not_called()
        self.record.assert_not_called()
        self.assertEqual([p.name for p in self.root.iterdir()], ['release.lock'])

    def test_release_status_or_pending_drift_refuses_both_modes_before_commands(self):
        for state in (
            dict(current='a' * 40, status='app_ready'),
            dict(current=recovery.TRANSPORT_COMMIT, status='app_failed'),
            dict(
                current=recovery.TRANSPORT_COMMIT, status='app_ready', pending='b' * 40
            ),
            {},
        ):
            self.state = state
            for execute in (False, True):
                with self.subTest(state=state, execute=execute):
                    with self.assertRaisesRegex(ValueError, 'release_changed'):
                        recovery.run(execute=execute)
                    self.docker.assert_not_called()
                    self.assert_untouched()

    def test_unapproved_manifest_or_image_refuses_before_app_or_operation(self):
        for gate in (self.release.approved_manifest, self.approved_image):
            gate.side_effect = ValueError('synthetic_approval_mismatch')
            for execute in (False, True):
                with self.assertRaisesRegex(ValueError, 'approval_mismatch'):
                    recovery.run(execute=execute)
                self.docker.assert_not_called()
                self.assert_untouched()
            gate.side_effect = None

    def test_app_image_running_or_mount_drift_refuses_both_modes(self):
        original = self.identity
        for change in (
            dict(image='sha256:' + 'a' * 64),
            dict(running=False),
            dict(mounts=[]),
            dict(mounts=[dict(Destination='/data', Source='/other-data', Type='bind')]),
        ):
            self.identity = {**original, **change}
            for execute in (False, True):
                with self.subTest(change=change, execute=execute):
                    with self.assertRaisesRegex(ValueError, 'app_image_mismatch'):
                        recovery.run(execute=execute)
                    self.quiet.assert_not_called()
                    self.assert_untouched()

    def test_zero_work_gate_failure_never_reaches_transfer(self):
        self.quiet.side_effect = ValueError('backup_recovery_zero_work_required')
        for execute in (False, True):
            with self.assertRaisesRegex(ValueError, 'zero_work'):
                recovery.run(execute=execute)
            self.verify.assert_not_called()
            self.assert_untouched()

    def test_default_qualification_binds_approved_image_and_does_not_write(self):
        result = recovery.run()
        self.assertEqual(result['state'], 'offline_recovery_qualified')
        self.assertEqual(result['writes'], 0)
        self.assertEqual(result['cloud_operations'], 0)
        self.assertEqual(
            self.approved_image.call_args.kwargs['pinned_image'],
            recovery.TRANSPORT_IMAGE,
        )
        self.release.approved_manifest.assert_called_once_with(
            self.root,
            self.root / 'releases' / recovery.TRANSPORT_COMMIT,
            recovery.TRANSPORT_COMMIT,
        )
        self.quiet.assert_called_once()
        self.verify.assert_called_once()
        self.assert_untouched()

    def test_prior_qualification_cannot_authorize_execution_after_promotion(self):
        recovery.run()
        self.state['current'] = 'a' * 40
        with self.assertRaisesRegex(ValueError, 'release_changed'):
            recovery.run(execute=True)
        self.assert_untouched()

    def test_lock_contention_refuses_and_execute_keeps_lock_through_write_boundary(
        self,
    ):
        import fcntl

        with (self.root / 'release.lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for execute in (False, True):
                with self.assertRaises(BlockingIOError):
                    recovery.run(execute=execute)
                self.assert_untouched()
            fcntl.flock(lock, fcntl.LOCK_UN)

            def transfer(*args):
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return {'state': 'synthetic_completed'}

            self.transfer.side_effect = transfer
            self.assertEqual(recovery.run(execute=True)['state'], 'synthetic_completed')
            self.transfer.assert_called_once()
            # The entry releases the lock even after returning from the writer.
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == '__main__':
    unittest.main()
