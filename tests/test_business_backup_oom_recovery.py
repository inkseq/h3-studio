"""No live operations: failed-capture identity, fences and bounded source loading."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_business_backup_recovery as original

h = original.host
with patch.dict(
    sys.modules, {'business_backup': h, 'business_backup_recovery': original.recovery}
):
    recovery = original.schedule.load('business_backup_oom_recovery')


class OomRecoveryTests(unittest.TestCase):
    def setUp(self):
        # This exhausted one-off operation must keep its installed historical pin.
        self.assertEqual(
            recovery.RUNTIME_SHA,
            'fed5e0c78078d0c833f983871e5da01960071f756f9453a7552976bda93ba98a',
        )
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.operation = self.root / recovery.DAY
        self.operation.mkdir()
        intent = dict(
            version=3,
            day=recovery.DAY,
            commit=recovery.old.TRANSPORT_COMMIT,
            image=recovery.old.TRANSPORT_IMAGE,
            operation_id=recovery.NONCE,
            automatic_retry=False,
        )
        h.record(self.operation / 'intent.json', intent)
        for name in ('phase-snapshot-started.json', 'phase-snapshot-unknown.json'):
            h.record(self.operation / name, dict(phase='snapshot'))
        hashes = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.operation.iterdir()
        }
        runtime = self.root / 'runtime.py'
        source = (original.schedule.ROOT / 'studio_platform/backup.py').read_bytes()
        runtime.write_bytes(source)
        self.source = source
        self.docker = Mock(side_effect=self.fake_docker)
        for item in (
            patch.object(h, 'ROOT', self.root),
            patch.object(h, 'protected', side_effect=lambda p, *args: Path(p)),
            patch.object(recovery, 'RUNTIME', runtime),
            # Historical installed runtime remains pinned; mocks exercise changed bytes.
            patch.object(recovery, 'RUNTIME_SHA', hashlib.sha256(source).hexdigest()),
            patch.object(recovery, 'ORIGINAL', hashes),
            patch.object(recovery, 'partial_unchanged'),
            patch.object(h, 'docker', self.docker),
            patch.object(
                recovery.subprocess,
                'run',
                return_value=Mock(returncode=0, stdout='failed'),
            ),
        ):
            item.start()
            self.addCleanup(item.stop)

    def fake_docker(self, args, **kwargs):
        if args[:3] == ['exec', recovery.old.APP, 'sha256sum']:
            return recovery.SOURCE_SHA + ' /app/studio_platform/backup.py'
        if args[0] == 'exec':
            return json.dumps({'nonterminal_jobs': 0})
        if args[0] == 'top':
            return 'PID COMMAND\n123 python app'
        return ''

    def test_qualification_checks_exact_runtime_and_has_no_writes(self):
        before = {p.name: p.read_bytes() for p in self.operation.iterdir()}
        self.assertEqual(recovery.qualify(self.operation), self.source)
        self.assertEqual(
            before, {p.name: p.read_bytes() for p in self.operation.iterdir()}
        )
        self.assertEqual(hashlib.sha256(self.source).hexdigest(), recovery.RUNTIME_SHA)
        self.assertTrue(
            all(
                c.args[0][0] in ('exec', 'top', 'ps')
                for c in self.docker.call_args_list
            )
        )

    def test_existing_attempt_fence_refuses_before_commands(self):
        h.record(self.operation / 'recovery-intent.json', {})
        with self.assertRaisesRegex(ValueError, 'already_attempted'):
            recovery.qualify(self.operation)
        self.docker.assert_not_called()

    def test_modified_original_metadata_or_runtime_refuses(self):
        for path in (self.operation / 'intent.json', recovery.RUNTIME):
            previous = path.read_bytes()
            path.write_bytes(previous + b'\n')
            with self.assertRaises(ValueError):
                recovery.qualify(self.operation)
            self.docker.assert_not_called()
            path.write_bytes(previous)

    def test_busy_unknown_or_boolean_work_cannot_capture(self):
        for count in (1, None, False, True):

            def docker(args, **kwargs):
                if args[0] == 'exec' and 'sha256sum' not in args:
                    return json.dumps({'nonterminal_jobs': count})
                return self.fake_docker(args, **kwargs)

            with patch.object(h, 'docker', side_effect=docker):
                with self.assertRaisesRegex(ValueError, 'zero_work'):
                    recovery.qualify(self.operation)

    def test_live_writer_or_previous_transfer_refuses(self):
        for kind in ('top', 'ps'):

            def docker(args, **kwargs):
                if args[0] == kind:
                    return (
                        '<reviewed-backup-capture>'
                        if kind == 'top'
                        else 'inkseq-backup-transfer-' + recovery.NONCE
                    )
                return self.fake_docker(args, **kwargs)

            with patch.object(h, 'docker', side_effect=docker):
                with self.assertRaises(ValueError):
                    recovery.qualify(self.operation)

    def test_failed_capture_preserves_fence_and_never_starts_transfer(self):
        original_bytes = (self.operation / 'intent.json').read_bytes()
        with patch.object(
            recovery.subprocess,
            'run',
            side_effect=subprocess.TimeoutExpired('docker', 2400),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                recovery.recover(self.operation, {}, self.source)
        self.assertEqual((self.operation / 'intent.json').read_bytes(), original_bytes)
        self.assertTrue((self.operation / 'recovery-intent.json').exists())
        self.assertTrue(
            (self.operation / 'oom-recovery/phase-snapshot-unknown.json').exists()
        )
        self.assertFalse((self.operation / 'target.json').exists())
        self.assertFalse((self.operation / 'completed.json').exists())
        self.docker.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'already_attempted'):
            recovery.qualify(self.operation)

    def test_failed_transfer_keeps_original_nonce_and_no_completion(self):
        cfg = original.schedule.ScheduleTests().config()

        def docker(args, **kwargs):
            if args[0] == 'run':
                self.assertIn('--instance-role-only', args)
                self.assertIn('--allow-incomplete', args)
                self.assertEqual(
                    h.read(self.operation / 'target.json')['prefix'],
                    'business-backups/' + recovery.NONCE,
                )
                raise subprocess.TimeoutExpired('docker', 1800)
            return ''

        with (
            patch.object(
                recovery.subprocess,
                'run',
                return_value=Mock(
                    returncode=0, stdout=b'{"state":"fresh_capture_returned"}'
                ),
            ),
            patch.object(h, 'freeze_transport_snapshot'),
            patch.object(h.os, 'chown', create=True),
            patch.object(h, 'docker', side_effect=docker),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                recovery.recover(self.operation, cfg, self.source)
        self.assertTrue(
            (self.operation / 'oom-recovery/phase-transfer-unknown.json').exists()
        )
        self.assertFalse((self.operation / 'completed.json').exists())

    def test_success_keeps_failed_source_and_links_pinned_completion(self):
        cfg = original.schedule.ScheduleTests().config()
        old_bytes = (self.operation / 'intent.json').read_bytes()
        coverage = dict(
            status='complete',
            referenced_objects=1,
            available_objects=1,
            missing_objects=0,
            referenced_bytes=10,
            available_bytes=10,
            missing_bytes=0,
            expected_missing_objects=0,
            expected_missing_bytes=0,
            unexpected_missing_objects=0,
            unexpected_missing_bytes=0,
            loss_alert_required=False,
        )
        receipt = dict(
            format='sixnine-private-backup-copy-v3',
            coverage=coverage,
            completion_sha256='a' * 64,
            completion_version_id='version-one',
        )
        with (
            patch.object(
                recovery.subprocess,
                'run',
                return_value=Mock(
                    returncode=0, stdout=b'{"state":"fresh_capture_returned"}'
                ),
            ) as capture,
            patch.object(h, 'freeze_transport_snapshot'),
            patch.object(h.os, 'chown', create=True),
            patch.object(h, 'copy_receipt', return_value=receipt),
            patch.object(h, 'require_hosted_coverage') as hosted,
            patch.object(h, 'retain_backups') as retention,
        ):
            result = recovery.recover(self.operation, cfg, self.source)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(capture.call_args.kwargs['input'], self.source)
        self.assertEqual(capture.call_args.kwargs['timeout'], h.SNAPSHOT_TIMEOUT)
        self.assertEqual(result['state'], 'fresh_copy_completed_not_restored')
        self.assertEqual((self.operation / 'intent.json').read_bytes(), old_bytes)
        complete = h.read(self.operation / 'completed.json')
        self.assertEqual(complete['completion_version_id'], 'version-one')
        self.assertEqual(
            complete['receipt_sha256'],
            hashlib.sha256((self.operation / 'receipt.json').read_bytes()).hexdigest(),
        )
        self.assertFalse(complete['restore_verified'])
        self.assertEqual(
            h.read(self.operation / 'target.json')['prefix'],
            'business-backups/' + recovery.NONCE,
        )
        hosted.assert_called_once_with(self.operation)
        retention.assert_not_called()

    def test_changed_source_or_existing_capture_refuses(self):
        destination = (
            self.root
            / 'platform-data'
            / 'business-backups'
            / (recovery.NONCE + '-oom-recapture')
        )
        destination.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'capture_exists'):
            recovery.qualify(self.operation)
        self.docker.assert_not_called()
        destination.rmdir()
        with patch.object(h, 'docker', return_value='wrong-source-sha'):
            with self.assertRaisesRegex(ValueError, 'source_changed'):
                recovery.qualify(self.operation)


if __name__ == '__main__':
    unittest.main()
