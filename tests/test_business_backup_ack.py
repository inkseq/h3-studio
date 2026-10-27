"""Isolated root-attestation protocol tests; no Docker, AWS, or production calls."""

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from test_business_backup_schedule import load

h = load('business_backup')
spec = importlib.util.spec_from_file_location(
    'business_backup_ack', Path(h.__file__).with_name('business_backup_ack.py')
)
ack = importlib.util.module_from_spec(spec)
with mock.patch.dict(
    sys.modules,
    {
        'business_backup': h,
        **({'fcntl': mock.Mock()} if sys.platform == 'win32' else {}),
    },
):
    spec.loader.exec_module(ack)


@unittest.skipIf(
    sys.platform == 'win32', 'Unix ownership/open flags; exercised by Linux CI'
)
class GenericCaptureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.prior = self.root / '2030-01-02'
        self.prior.mkdir(mode=0o700)
        self.cfg = {
            'environment': 'production',
            'instance_id': h.HOSTS['production'],
            'target': {'bucket': 'sixnine-business-backup-829135631045-ap-southeast-1'},
        }
        self.expected = {
            'release': 'a' * 40,
            'image': 'sha256:' + 'b' * 64,
            **{
                k: 'c' * 64
                for k in (
                    'config_sha256',
                    'release_state_sha256',
                    'helper_sha256',
                    'operator_sha256',
                )
            },
        }
        self.intent = {
            'version': 3,
            'day': self.prior.name,
            'commit': 'd' * 40,
            'image': 'sha256:' + 'e' * 64,
            'operation_id': '8e51de50-8b9d-4338-beda-c02b94d06aca',
            'automatic_retry': False,
        }
        self.write('intent.json', self.intent)
        phase = {
            'version': 1,
            'phase': 'snapshot',
            'observed_at': '2030-01-02T16:30:00Z',
            'automatic_retry': False,
        }
        self.write('phase-snapshot-started.json', phase)
        self.write(
            'phase-snapshot-unknown.json',
            {
                **phase,
                'observed_at': '2030-01-02T16:32:00Z',
                'code': 'backup_docker_failed_or_unknown',
            },
        )
        self.listing = {
            'bucket': self.cfg['target']['bucket'],
            'prefix': 'business-backups/' + self.intent['operation_id'] + '/',
            'versions': 0,
            'delete_markers': 0,
            'listing_complete': True,
            'listing_started_at': '2030-01-02T17:00:00Z',
            'checked_at': '2030-01-02T17:00:10Z',
        }
        self.now = h.datetime.fromisoformat('2030-01-02T17:01:00+00:00')
        for patch in (
            mock.patch.object(h, 'protected', side_effect=self.safe_path),
            mock.patch.object(h, 'datetime', wraps=h.datetime),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        h.datetime.now.return_value = self.now

    def safe_path(self, path, directory=False):
        path = Path(path)
        if (
            path.is_symlink()
            or not (path.is_dir() if directory else path.is_file())
            or path.stat().st_mode & 0o022
        ):
            raise ValueError('backup_path_unprotected')
        return path  # Temp-root ownership only is abstracted; mode/link checks remain.

    def write(self, name, value):
        path = self.prior / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def bytes(self):
        return {p.name: p.read_bytes() for p in self.prior.iterdir() if p.is_file()}

    def value(self):
        _, evidence, _ = h.capture_evidence(self.prior)
        return {
            'version': 2,
            'state': 'failed_capture_reconciled_not_completed',
            'day': self.prior.name,
            'operation_id': self.intent['operation_id'],
            'evidence': evidence,
            **self.listing,
            **self.expected,
            'acknowledged_at': '2030-01-02T17:01:00Z',
            'zero_work': True,
            'no_writer_or_transport': True,
            'partials_preserved': True,
            'automatic_retry': False,
        }

    def install(self, value=None):
        self.write(h.FAILED_CAPTURE_ACK, self.value() if value is None else value)

    def check(self, day='2030-01-03', cfg=None):
        return h.check_history(self.root, day, self.cfg if cfg is None else cfg)

    def test_future_unregistered_day_is_read_only_and_never_completed(self):
        self.install()
        before = self.bytes()
        self.assertFalse(self.check())
        self.assertFalse(self.check('2030-01-04'))
        self.assertEqual(before, self.bytes())
        self.assertFalse((self.prior / 'completed.json').exists())
        self.assertFalse((self.root / '2030-01-03').exists())
        for day in ('2030-01-02', '2030-01-01', '2030-02-31', '2030-1-3'):
            with self.subTest(day=day), self.assertRaises(ValueError):
                self.check(day)

    def test_no_ack_cannot_implicitly_approve_failure(self):
        before = self.bytes()
        with self.assertRaisesRegex(ValueError, 'prior_outcome'):
            self.check()
        self.assertEqual(before, self.bytes())

    def test_remote_identity_time_and_boolean_attestations_fail_closed(self):
        good = self.value()
        for field, bad in [
            ('versions', 1),
            ('versions', False),
            ('delete_markers', 1),
            ('listing_complete', False),
            ('prefix', 'business-backups/other/'),
            ('operation_id', 'other'),
            ('bucket', 'other'),
            ('day', '2030-01-01'),
            ('evidence', {}),
            ('zero_work', False),
            ('no_writer_or_transport', False),
            ('partials_preserved', False),
            ('automatic_retry', True),
            ('image', 'latest'),
            ('release', 'main'),
            ('helper_sha256', 'bad'),
            ('listing_started_at', '2030-01-02T16:31:00Z'),
            ('checked_at', '2030-01-02T17:02:00Z'),
            ('acknowledged_at', '2030-01-02T17:06:00Z'),
            ('checked_at', '2030-01-02'),
            ('state', 'completed'),
        ]:
            with self.subTest(field=field), self.assertRaises((ValueError, TypeError)):
                self.install({**good, field: bad})
                self.check()
        self.install({**good, 'skip_history': True})
        with self.assertRaises(ValueError):
            self.check()

    def test_original_inventory_and_permissions_are_revalidated(self):
        self.install()
        before = self.bytes()
        for name in (
            'target.json',
            'receipt.json',
            'completed.json',
            'output',
            'phase-handoff-started.json',
            'phase-transfer-started.json',
            'recovery-intent.json',
            'phase-snapshot-returned.json',
        ):
            with self.subTest(name=name):
                self.write(name, {})
                with self.assertRaises(ValueError):
                    self.check()
                (self.prior / name).unlink()
        for name in (
            'intent.json',
            'phase-snapshot-started.json',
            'phase-snapshot-unknown.json',
        ):
            path = self.prior / name
            path.write_bytes(before[name] + b' ')
            with self.assertRaises(ValueError):
                self.check()
            path.unlink()
            with self.assertRaises(ValueError):
                self.check()
            path.write_bytes(before[name])
            path.chmod(0o600)
        path = self.prior / h.FAILED_CAPTURE_ACK
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'not_private'):
            self.check()
        path.chmod(0o600)
        original = self.prior / 'intent.json'
        original.unlink()
        original.symlink_to(path)
        with self.assertRaisesRegex(ValueError, 'unprotected'):
            self.check()

    def test_ack_cannot_move_to_different_host_or_day(self):
        self.install()
        for cfg in (
            {},
            {**self.cfg, 'environment': 'staging'},
            {**self.cfg, 'instance_id': h.HOSTS['staging']},
            {**self.cfg, 'target': {'bucket': 'other'}},
        ):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                self.check(cfg=cfg)
        self.prior.rename(self.root / '2030-01-01')
        with self.assertRaises(ValueError):
            self.check()

    def test_capture_phase_semantics_even_when_attested_hashes_match(self):
        bad = json.loads((self.prior / 'phase-snapshot-unknown.json').read_text())
        for update in (
            {'phase': 'transfer'},
            {'automatic_retry': True},
            {'code': 'success'},
            {'observed_at': '2030-01-02T16:29:00Z'},
        ):
            self.write('phase-snapshot-unknown.json', {**bad, **update})
            with self.assertRaises(ValueError):
                h.capture_evidence(self.prior)

    def test_plan_and_apply_repeat_guards_preserve_originals_and_refuse_replay(self):
        original = h.capture_evidence(self.prior)
        before = self.bytes()
        with (
            mock.patch.object(
                ack, 'qualify', return_value=(self.cfg, original)
            ) as qualify,
            mock.patch.object(
                ack, 'negative_listing', return_value=self.listing
            ) as listing,
        ):
            self.assertEqual(
                ack.adopt(self.prior, self.expected)['state'], 'qualified_only'
            )
            listing.assert_not_called()
            self.assertEqual(before, self.bytes())
            result = ack.adopt(self.prior, self.expected, execute=True)
            self.assertEqual(result['state'], 'failed_capture_reconciled_not_completed')
            self.assertFalse(result['completed'])
            self.assertEqual(qualify.call_count, 3)
            self.assertEqual(
                {k: v for k, v in self.bytes().items() if k != h.FAILED_CAPTURE_ACK},
                before,
            )
            with self.assertRaises(ValueError):
                ack.adopt(self.prior, self.expected, execute=True)
        self.assertFalse(self.check())

    def test_stale_listing_changed_cas_or_guard_failure_never_writes(self):
        original = h.capture_evidence(self.prior)
        for changed in (
            RuntimeError('active writer'),
            (self.cfg, (self.intent, {}, original[2])),
        ):
            with (
                mock.patch.object(
                    ack, 'qualify', side_effect=[(self.cfg, original), changed]
                ),
                mock.patch.object(ack, 'negative_listing', return_value=self.listing),
            ):
                with self.assertRaises((ValueError, RuntimeError)):
                    ack.adopt(self.prior, self.expected, execute=True)
                self.assertFalse((self.prior / h.FAILED_CAPTURE_ACK).exists())
        with (
            mock.patch.object(ack, 'qualify', return_value=(self.cfg, original)),
            mock.patch.object(
                ack,
                'negative_listing',
                return_value={
                    **self.listing,
                    'listing_started_at': '2030-01-02T16:50:00Z',
                },
            ),
        ):
            with self.assertRaisesRegex(ValueError, 'stale'):
                ack.adopt(self.prior, self.expected, execute=True)
            self.assertFalse((self.prior / h.FAILED_CAPTURE_ACK).exists())

    def test_live_guard_rejects_work_writer_transport_and_service(self):
        nonce = self.intent['operation_id']
        for work, process, names, service in [
            (1, '', '', 'failed'),
            (0, 'studio_platform.backup_cli', '', 'failed'),
            (0, '', 'inkseq-backup-transfer-' + nonce, 'failed'),
            (0, '', '', 'deactivating'),
        ]:
            with (
                mock.patch.object(
                    h,
                    'docker',
                    side_effect=[
                        json.dumps({'nonterminal_jobs': work}),
                        process,
                        names,
                    ],
                ),
                mock.patch.object(
                    ack.subprocess,
                    'run',
                    return_value=mock.Mock(returncode=0, stdout=service),
                ),
            ):
                with self.assertRaises(ValueError):
                    ack.quiet(nonce)

    def test_pin_schema_and_changed_files_refuse_before_live_validation(self):
        with self.assertRaisesRegex(ValueError, 'pin_fields'):
            ack.pins({**self.expected, 'zero_work': True})
        with mock.patch.object(ack, 'digest', return_value='f' * 64):
            with self.assertRaisesRegex(ValueError, 'pin_changed'):
                ack.pins(self.expected)

    def test_operator_requires_root_and_nonblocking_release_lock(self):
        with mock.patch.object(os, 'geteuid', return_value=10001):
            with self.assertRaisesRegex(ValueError, 'root_required'):
                ack.run(self.prior.name, self.expected)
        lock = self.root / 'release.lock'
        lock.write_text('')
        lock.chmod(0o600)
        fd = os.open(lock, os.O_RDWR)
        try:
            ack.fcntl.flock(fd, ack.fcntl.LOCK_EX | ack.fcntl.LOCK_NB)
            with (
                mock.patch.object(os, 'geteuid', return_value=0),
                mock.patch.object(h, 'ROOT', self.root),
                mock.patch.object(h, 'STATE', self.root),
                mock.patch.object(ack, 'adopt') as adopt,
            ):
                with self.assertRaises(BlockingIOError):
                    ack.run(self.prior.name, self.expected, execute=True)
                adopt.assert_not_called()
        finally:
            os.close(fd)

    def test_listing_error_and_exclusive_write_failure_preserve_original_tree(self):
        before = self.bytes()
        original = h.capture_evidence(self.prior)
        with (
            mock.patch.object(ack, 'qualify', return_value=(self.cfg, original)),
            mock.patch.object(
                ack,
                'negative_listing',
                side_effect=TimeoutError('unknown read outcome'),
            ),
        ):
            with self.assertRaises(TimeoutError):
                ack.adopt(self.prior, self.expected, execute=True)
        self.assertEqual(before, self.bytes())
        with (
            mock.patch.object(ack, 'qualify', return_value=(self.cfg, original)),
            mock.patch.object(ack, 'negative_listing', return_value=self.listing),
            mock.patch.object(
                h, 'record', side_effect=FileExistsError('already exists')
            ),
        ):
            with self.assertRaises(FileExistsError):
                ack.adopt(self.prior, self.expected, execute=True)
        self.assertEqual(before, self.bytes())

    def test_listing_consumes_every_page_and_refuses_versions_errors_or_incomplete(
        self,
    ):
        for pages, valid in [
            ([{'IsTruncated': True}, {'IsTruncated': False}], True),
            ([{'IsTruncated': False, 'Versions': [{}]}], False),
            (
                [{'IsTruncated': True}, {'IsTruncated': False, 'DeleteMarkers': [{}]}],
                False,
            ),
            ([{'IsTruncated': True}], False),
            ([], False),
            ([{}], False),
        ]:
            client = mock.Mock()
            client.get_paginator.return_value.paginate.return_value = iter(pages)
            target = {
                'account_id': '829135631045',
                'region': 'ap-southeast-1',
                'bucket': self.cfg['target']['bucket'],
                'prefix': 'business-backups/' + self.intent['operation_id'],
                'kms_key_arn': 'unused',
                'role_arn': 'unused',
            }
            # Execute exactly the listing program with only its imports stubbed.
            modules = {
                'studio_platform.backup_remote': mock.Mock(
                    BackupTarget=lambda **v: type('Target', (), v)()
                ),
                'studio_platform.backup_remote_cli': mock.Mock(
                    aws_client=mock.Mock(return_value=client)
                ),
            }
            with (
                mock.patch.dict(sys.modules, modules),
                mock.patch.object(sys, 'argv', ['read', json.dumps(target)]),
                mock.patch('builtins.print') as output,
            ):
                if valid:
                    exec(compile(ack.LIST, '<listing>', 'exec'), {})
                    self.assertTrue(
                        json.loads(output.call_args.args[0])['listing_complete']
                    )
                else:
                    with self.assertRaises(ValueError):
                        exec(compile(ack.LIST, '<listing>', 'exec'), {})
            client.get_paginator.assert_called_once_with('list_object_versions')
            client.put_object.assert_not_called()
            client.delete_object.assert_not_called()


if __name__ == '__main__':
    unittest.main()
