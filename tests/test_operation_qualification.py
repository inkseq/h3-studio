"""Host qualifier refusal contracts. No live Docker, database or AWS calls."""

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_business_backup_schedule as schedule

q = schedule.load('operation_qualification')


class Fixture:
    def setUp(self):
        self.binding = {
            'version': 1,
            'environment': 'staging',
            'instance_id': q.HOSTS['staging'],
            'current': 'a' * 40,
            'previous': 'b' * 40,
            'helpers': {name: q.sha(b'helper') for name in q.REQUIRED_HELPERS},
            'files': {},
            'containers': {},
            'absent': ['new-operation'],
        }
        for index, service in enumerate(sorted(q.SERVICES)):
            self.binding['containers'][service] = {
                'id': str(index) * 64,
                'image': 'sha256:' + 'c' * 64,
                'running': True,
                'started': '2026-10-27T00:00:00Z',
            }
        self.raw = {q.HELPERS / name: b'helper' for name in q.REQUIRED_HELPERS}
        self.raw[q.INSTANCE] = self.binding['instance_id'].encode()
        self.put(
            'release-state.json',
            {'current': 'a' * 40, 'previous': 'b' * 40, 'status': 'app_ready'},
        )
        self.put('migration/active.json', {'current': 'a' * 40})
        self.put(
            'deployment-target.json',
            {
                'version': 1,
                'target': 'staging',
                'instance_id': self.binding['instance_id'],
            },
        )
        self.put('migration/paths.json', {})
        self.put('site.env', {})
        for commit in ('a' * 40, 'b' * 40):
            for manifest, approval in (
                (
                    f'releases/{commit}/release-manifest.json',
                    f'approved-releases/{commit}.sha256',
                ),
                (
                    f'migration/releases/{commit}/artifact/migration-manifest.json',
                    f'approved-migrations/{commit}.sha256',
                ),
            ):
                self.put(manifest, {'commit': commit, 'image_id': 'sha256:' + 'c' * 64})
                self.put(approval, q.sha(self.raw[q.ROOT / manifest]).encode())

    def put(self, name, value):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.raw[q.ROOT / name] = raw
        self.binding['files'][name] = q.sha(raw)

    def check(self, archive_ids=None):
        with (
            patch.object(q, 'read', side_effect=lambda path: self.raw[path]),
            patch.object(q, 'runtime', return_value=self.binding['containers']),
            patch.object(
                q,
                'archive_image_ids',
                return_value=archive_ids or {'sha256:' + 'c' * 64},
            ),
            patch.object(q, 'directory'),
        ):
            q.check(q.validate(self.binding))


class QualificationTests(Fixture, unittest.TestCase):
    def test_valid_exact_binding(self):
        self.check()

    def rds(self, *, target_host=None):
        selected = {
            'mode': 'rds',
            'host': 'inkseq-stage.abc123.ap-southeast-1.rds.amazonaws.com',
        }
        self.binding['database'] = selected
        self.binding['containers'].pop('db')
        self.put(
            'deployment-target.json',
            {
                'version': 1,
                'target': 'staging',
                'instance_id': self.binding['instance_id'],
                'database': {**selected, 'host': target_host or selected['host']},
            },
        )

    def test_rds_qualification_requires_exact_protected_database_and_four_services(
        self,
    ):
        self.rds()
        self.check()
        with patch.object(
            q, 'runtime', return_value=self.binding['containers']
        ) as runtime:
            with (
                patch.object(q, 'read', side_effect=lambda path: self.raw[path]),
                patch.object(
                    q, 'archive_image_ids', return_value={'sha256:' + 'c' * 64}
                ),
                patch.object(q, 'directory'),
            ):
                q.check(q.validate(self.binding))
            self.assertEqual(
                runtime.call_args.kwargs['target']['database'], self.binding['database']
            )

    def test_database_mode_or_endpoint_cannot_be_omitted_or_rebound(self):
        self.rds(target_host='inkseq-other.abc123.ap-southeast-1.rds.amazonaws.com')
        with self.assertRaisesRegex(q.Refused, 'database_changed'):
            self.check()
        self.put(
            'deployment-target.json',
            {
                'version': 1,
                'target': 'staging',
                'instance_id': self.binding['instance_id'],
            },
        )
        with self.assertRaisesRegex(q.Refused, 'database_changed'):
            self.check()
        self.binding.pop('database')
        with self.assertRaisesRegex(q.Refused, 'runtime_pins_required'):
            q.validate(self.binding)

    def test_local_binding_cannot_qualify_a_protected_rds_host(self):
        self.put(
            'deployment-target.json',
            {
                'version': 1,
                'target': 'staging',
                'instance_id': self.binding['instance_id'],
                'database': {
                    'mode': 'rds',
                    'host': 'inkseq-stage.abc123.ap-southeast-1.rds.amazonaws.com',
                },
            },
        )
        with self.assertRaisesRegex(q.Refused, 'database_changed'):
            self.check()

    def test_rds_managed_secret_selection_remains_bound_by_protected_target_hash(self):
        self.rds()
        path = q.ROOT / 'deployment-target.json'
        target = json.loads(self.raw[path])
        target['database']['admin_secret_arn'] = (
            'arn:aws:secretsmanager:ap-southeast-1:829135631045:secret:rds!db-test-123'
        )
        self.put('deployment-target.json', target)
        self.check()
        self.assertEqual(set(self.binding['database']), {'mode', 'host'})
        target['database']['admin_secret_arn'] += '-changed'
        self.raw[path] = json.dumps(target).encode()
        with self.assertRaisesRegex(q.Refused, 'marker_changed'):
            self.check()

    def test_rds_binding_rejects_local_db_pin_and_invalid_database_selection(self):
        original = copy.deepcopy(self.binding)
        self.rds()
        self.binding['containers']['db'] = original['containers']['db']
        with self.assertRaisesRegex(q.Refused, 'runtime_pins_required'):
            q.validate(self.binding)
        self.binding['containers'].pop('db')
        for selected in (
            {},
            {'mode': 'local', 'host': 'db'},
            {'mode': 'rds', 'host': 'unrelated.example'},
            {
                'mode': 'rds',
                'host': 'inkseq-stage.abc123.ap-southeast-1.rds.amazonaws.com',
                'ca_file': 'other',
            },
        ):
            with self.subTest(selected=selected):
                self.binding['database'] = selected
                with self.assertRaisesRegex(q.Refused, 'database_binding_invalid'):
                    q.validate(self.binding)

    def test_incomplete_and_unsafe_pins_refused(self):
        original = copy.deepcopy(self.binding)
        mutations = [
            lambda b: b.update(version=True),
            lambda b: b.update(instance_id=q.HOSTS['production']),
            lambda b: b.update(current='main'),
            lambda b: b['helpers'].pop('release.py'),
            lambda b: b['files'].pop('site.env'),
            lambda b: b['files'].update({'../escape': 'd' * 64}),
            lambda b: b.update(absent=['/tmp/new']),
            lambda b: b['containers'].pop('db'),
            lambda b: b['containers']['app'].update(running=False),
        ]
        for change in mutations:
            with self.subTest(change=change):
                value = copy.deepcopy(original)
                change(value)
                with self.assertRaises(q.Refused):
                    q.validate(value)

    def test_mutable_markers_helpers_and_host_refuse(self):
        for path in [q.INSTANCE, q.HELPERS / 'release.py', q.ROOT / 'site.env']:
            with self.subTest(path=path), patch.dict(self.raw, {path: b'changed'}):
                with self.assertRaises(q.Refused):
                    self.check()

    def test_even_pinned_unready_or_wrong_tuple_refuses(self):
        changes = [
            (
                'release-state.json',
                {
                    'current': 'a' * 40,
                    'previous': 'b' * 40,
                    'status': 'app_ready',
                    'pending': 'd' * 40,
                },
            ),
            ('migration/active.json', {'current': 'b' * 40}),
            (
                'deployment-target.json',
                {'target': 'production', 'instance_id': q.HOSTS['production']},
            ),
            ('approved-releases/' + 'b' * 40 + '.sha256', b'd' * 64),
        ]
        for name, value in changes:
            with self.subTest(name=name):
                raw, pins = self.raw.copy(), self.binding['files'].copy()
                self.put(name, value)
                with self.assertRaises(q.Refused):
                    self.check()
                self.raw, self.binding['files'] = raw, pins

    def test_oci_manifest_and_config_can_differ_but_app_must_belong_to_archive(self):
        name = 'releases/' + 'a' * 40 + '/release-manifest.json'
        self.put(name, {'commit': 'a' * 40, 'image_id': 'sha256:' + 'd' * 64})
        self.put(
            'approved-releases/' + 'a' * 40 + '.sha256',
            q.sha(self.raw[q.ROOT / name]).encode(),
        )
        self.check(archive_ids={'sha256:' + 'd' * 64, 'sha256:' + 'c' * 64})
        with self.assertRaisesRegex(q.Refused, 'manifest_image_changed'):
            self.check(archive_ids={'sha256:' + 'd' * 64})

    def test_cli_does_not_leak_subprocess_or_config_error(self):
        output = io.StringIO()
        with (
            patch.object(q, 'read', side_effect=ValueError('SECRET')),
            patch('sys.stdout', output),
        ):
            self.assertEqual(q.main(['/root/pins.json']), 1)
        self.assertNotIn('SECRET', output.getvalue())
        self.assertIn('qualification_read_failed', output.getvalue())

    def test_archive_reader_failures_refuse_and_never_load_images(self):
        result = subprocess.CompletedProcess([], 0, b'["sha256:' + b'c' * 64 + b'"]')
        with patch.object(q.subprocess, 'run', return_value=result) as run:
            self.assertEqual(q.archive_image_ids('a' * 40, {}), {'sha256:' + 'c' * 64})
            self.assertEqual(
                run.call_args.args[0][:3], ['/usr/bin/python3', '-I', '-B']
            )
            self.assertIn('validate_image_archive', run.call_args.args[0][4])
            result.returncode = 1
            with self.assertRaisesRegex(q.Refused, 'archive_invalid'):
                q.archive_image_ids('a' * 40, {})
            result.returncode = 0
            result.stdout = b'["not-an-image"]'
            with self.assertRaisesRegex(q.Refused, 'archive_identity_invalid'):
                q.archive_image_ids('a' * 40, {})

    def test_runtime_checks_unique_service_and_reads_only_selected_fields(self):
        observed = self.binding['containers']

        def reply(args, timeout=30):
            if args[0] == 'ps':
                service = args[-1].split('=')[-1]
                return observed[service]['id'].encode()
            self.assertEqual(args[:2], ['inspect', '--format'])
            self.assertNotIn('.Config', args[2])
            return json.dumps(
                next(row for row in observed.values() if row['id'] == args[-1])
            ).encode()

        with patch.object(q, 'docker', side_effect=reply):
            self.assertEqual(q.runtime(), observed)
        with patch.object(q, 'docker', return_value=b''):
            with self.assertRaisesRegex(q.Refused, 'service_not_unique'):
                q.runtime()

    def test_rds_runtime_observes_all_required_services_without_local_database(self):
        self.rds()
        calls = []
        observed = self.binding['containers']

        def reply(args, timeout=30):
            calls.append(args)
            if args[0] == 'ps':
                return observed[args[-1].split('=')[-1]]['id'].encode()
            return json.dumps(
                next(row for row in observed.values() if row['id'] == args[-1])
            ).encode()

        target = json.loads(self.raw[q.ROOT / 'deployment-target.json'])
        with patch.object(q, 'docker', side_effect=reply):
            self.assertEqual(q.runtime(target=target), observed)
        self.assertEqual(
            {args[-1].split('=')[-1] for args in calls if args[0] == 'ps'},
            set(observed),
        )


@unittest.skipUnless(sys.platform == 'linux', 'real Linux lock and file semantics')
class LockTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'release.lock').write_bytes(b'')
        self.addCleanup(patch.stopall)
        patch.object(q, 'ROOT', self.root).start()
        patch.object(q.os, 'geteuid', return_value=0).start()
        patch.object(q, 'protected', side_effect=lambda p: p.lstat()).start()
        self.checker = patch.object(q, 'check').start()
        self.docker = patch.object(
            q, 'docker', return_value=b'{"nonterminal_jobs":0}'
        ).start()

    def test_holds_real_lock_through_caller_boundary_and_does_not_write(self):
        program = 'import fcntl,sys;f=open(sys.argv[1],"r+");fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with q.locked_qualification(self.binding) as receipt:
            probe = subprocess.run(
                [sys.executable, '-c', program, str(self.root / 'release.lock')],
                capture_output=True,
                timeout=10,
            )
            self.assertNotEqual(probe.returncode, 0)
            self.assertEqual(receipt['state'], 'qualified_no_writes')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        self.assertEqual(self.checker.call_count, 2)

    def test_busy_and_malformed_count_refuse_before_yield(self):
        for count in (1, False, '0', None):
            with self.subTest(count=count):
                self.docker.return_value = json.dumps(
                    {'nonterminal_jobs': count}
                ).encode()
                with self.assertRaisesRegex(q.Refused, 'zero_work_required'):
                    with q.locked_qualification(self.binding):
                        self.fail('write boundary reached')

    def test_rds_database_query_failure_never_reaches_write_boundary(self):
        self.binding['database'] = {
            'mode': 'rds',
            'host': 'inkseq-stage.abc123.ap-southeast-1.rds.amazonaws.com',
        }
        self.binding['containers'].pop('db')
        self.docker.side_effect = q.Refused('qualification_docker_read_failed')
        with self.assertRaisesRegex(q.Refused, 'docker_read_failed'):
            with q.locked_qualification(self.binding):
                self.fail('unobservable database reached write boundary')
        self.assertEqual(self.docker.call_args.args[0][-1], q.QUIET)
        self.assertIn('Settings.from_environment().database_url', q.QUIET)

    def test_drift_between_reads_cannot_reach_write_boundary(self):
        self.checker.side_effect = [None, q.Refused('qualification_runtime_changed')]
        with self.assertRaisesRegex(q.Refused, 'runtime_changed'):
            with q.locked_qualification(self.binding):
                self.fail('write boundary reached')

    def test_missing_or_replaced_lock_refuses(self):
        (self.root / 'release.lock').unlink()
        with self.assertRaises(FileNotFoundError):
            with q.locked_qualification(self.binding):
                self.fail('missing lock created')

    def test_replaced_lock_cannot_reach_write_boundary(self):
        def drift(_binding):
            (self.root / 'new-lock').write_bytes(b'')
            os.replace(self.root / 'new-lock', self.root / 'release.lock')

        self.checker.side_effect = [None, None]
        self.docker.side_effect = lambda *args, **kwargs: (
            drift(None) or b'{"nonterminal_jobs":0}'
        )
        with self.assertRaisesRegex(q.Refused, 'lock_changed'):
            with q.locked_qualification(self.binding):
                self.fail('replaced lock accepted')

    def test_existing_operation_and_dangling_link_are_not_absent(self):
        patch.stopall()
        original_root = q.ROOT
        operation = self.root / 'new-operation'
        with (
            patch.object(q, 'ROOT', self.root),
            patch.object(q, 'directory'),
            patch.object(
                q,
                'read',
                side_effect=lambda p: self.raw[
                    original_root / p.relative_to(self.root)
                    if p.is_relative_to(self.root)
                    else p
                ],
            ),
            patch.object(q, 'runtime', return_value=self.binding['containers']),
            patch.object(q, 'archive_image_ids', return_value={'sha256:' + 'c' * 64}),
        ):
            operation.mkdir()
            with self.assertRaisesRegex(q.Refused, 'prior_outcome_exists'):
                q.check(self.binding)
            operation.rmdir()
            operation.symlink_to(self.root / 'missing')
            with self.assertRaisesRegex(q.Refused, 'prior_outcome_exists'):
                q.check(self.binding)

    def test_symlink_and_hardlink_rejected_by_real_reader(self):
        patch.stopall()
        file = self.root / 'pins.json'
        file.write_bytes(b'{}')
        link = self.root / 'link.json'
        link.symlink_to(file)
        with patch.object(q, 'directory'):
            with self.assertRaises(q.Refused):
                q.read(link)
            link.unlink()
            os.link(file, link)
            with self.assertRaises(q.Refused):
                q.read(file)


if __name__ == '__main__':
    unittest.main()
