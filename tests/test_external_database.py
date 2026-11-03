"""External database selection and management transport, without a live database."""

import hashlib
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
from contextlib import nullcontext
import unittest
from unittest.mock import patch

from deploy.platform import deployment_target as target
from tests.test_platform_release import release


HOST = 'inkseq-example.example.ap-southeast-1.rds.amazonaws.com'
RDS = {
    'INKSEQ_DATABASE_MODE': 'rds',
    'INKSEQ_DATABASE_HOST': HOST,
    'INKSEQ_DATABASE_CA_FILE': target.DATABASE_CA_FILE,
}


class ExternalDatabaseTests(unittest.TestCase):
    def test_sealed_selection_owns_endpoint_and_local_remains_default(self):
        self.assertEqual(
            target.database({}), {'mode': 'local', 'host': 'db', 'ca_file': None}
        )
        self.assertEqual(
            target.database_environment({'database': {'mode': 'rds', 'host': HOST}}),
            RDS,
        )
        for changed in (
            {'INKSEQ_DATABASE_MODE': 'unknown'},
            {'INKSEQ_DATABASE_HOST': HOST},
            {**RDS, 'INKSEQ_DATABASE_HOST': 'db'},
            {**RDS, 'INKSEQ_DATABASE_HOST': HOST + '.evil.example'},
            {**RDS, 'INKSEQ_DATABASE_CA_FILE': '/tmp/untrusted.pem'},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                target.database(changed)

    def test_external_admin_client_has_only_file_references_and_verified_tls(self):
        env = {**RDS, 'SIXNINE_POSTGRES_IMAGE': 'postgres@sha256:' + 'a' * 64}
        protected = SimpleNamespace(st_mode=stat.S_IFREG | 0o400)
        sql = 'BEGIN READ ONLY; SELECT 1; COMMIT;'
        with (
            patch.object(release, 'regular', return_value=protected) as check,
            patch.object(
                release,
                'database_admin_file',
                return_value=nullcontext(Path('/private/password')),
            ),
            patch.object(release, 'command', return_value=b'BEGIN\n1\nCOMMIT\n') as run,
        ):
            self.assertEqual(
                release.database_query(Path('/approved'), env, sql),
                b'BEGIN\n1\nCOMMIT\n',
            )
        args = run.call_args.args[0]
        self.assertEqual(
            args[:6], ['run', '--rm', '-i', '--pull', 'never', '--network']
        )
        self.assertIn('PGSSLMODE=verify-full', args)
        self.assertIn('PGHOST=' + HOST, args)
        self.assertIn('PGSSLROOTCERT=' + target.DATABASE_CONTAINER_CA, args)
        self.assertIn('PGUSER=inkseq_operator', args)
        env_values = [args[i + 1] for i, value in enumerate(args) if value == '--env']
        self.assertFalse(any(value.startswith('PGPASSWORD=') for value in env_values))
        self.assertEqual(run.call_args.kwargs['input_data'], sql.encode())
        self.assertEqual(run.call_args.kwargs['timeout'], 30)
        self.assertEqual(check.call_args_list[0].args[0], Path(target.DATABASE_CA_FILE))
        with (
            patch.object(
                release, 'regular', side_effect=release.ReleaseError('unprotected CA')
            ),
            patch.object(release, 'command') as run,
            self.assertRaises(release.ReleaseError),
        ):
            release.database_query(Path('/approved'), env, sql)
        run.assert_not_called()

    def test_managed_master_is_fresh_private_and_removed_even_on_failure(self):
        from unittest.mock import Mock
        import boto3

        arn = 'arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:rds!db-example-AbCdEf'
        api = Mock()
        api.get_secret_value.side_effect = [
            {
                'SecretString': json.dumps(
                    {'username': 'inkseq_operator', 'password': password}
                )
            }
            for password in (
                'synthetic-original-password',
                'synthetic-rotated-password',
            )
        ]
        with patch.object(boto3, 'client', return_value=api):
            for expected in (
                'synthetic-original-password',
                'synthetic-rotated-password',
            ):
                with self.assertRaisesRegex(RuntimeError, 'client failed'):
                    with release.database_admin_file(
                        {'INKSEQ_DATABASE_ADMIN_SECRET_ARN': arn}
                    ) as path:
                        self.assertEqual(path.read_text(), expected)
                        if __import__('os').name != 'nt':
                            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
                        raise RuntimeError('client failed')
                self.assertFalse(path.exists())
        self.assertEqual(api.get_secret_value.call_count, 2)
        self.assertEqual(api.get_secret_value.call_args.kwargs, {'SecretId': arn})

    def test_external_overrides_follow_existing_compose_files(self):
        base = Path('/approved')
        self.assertEqual(target.database_compose_args(base, {}), [])
        self.assertEqual(
            target.database_compose_args(base, RDS, migration=True),
            [
                '-f',
                str(base / 'database-rds.yaml'),
                '-f',
                str(base / 'database-rds-migration.yaml'),
            ],
        )
        with patch.object(release, 'command', return_value=b'{}') as run:
            release.compose(base, RDS, 'config', '--format', 'json')
        self.assertEqual(
            run.call_args.args[0][-5:],
            ['-f', str(base / 'database-rds.yaml'), 'config', '--format', 'json'],
        )

    def test_bundle_includes_both_overrides_and_rejects_partial_pair(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            hashes = {}
            for name in release.RDS_FILES:
                raw = ('inert ' + name).encode()
                (root / name).write_bytes(raw)
                hashes[name] = hashlib.sha256(raw).hexdigest()
            value = {
                'commit': 'a' * 40,
                'image': 'sixnine-platform:' + 'a' * 40,
                'image_id': 'sha256:' + 'b' * 64,
                'files': hashes,
            }
            (root / 'release-manifest.json').write_text(json.dumps(value))
            self.assertEqual(release.manifest(root, 'a' * 40), value)
            del hashes['database-rds-migration.yaml']
            (root / 'release-manifest.json').write_text(json.dumps(value))
            with self.assertRaises(release.ReleaseError):
                release.manifest(root, 'a' * 40)
