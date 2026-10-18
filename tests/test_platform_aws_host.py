"""Offline host-only secret/bundle paths with synthetic values and fake AWS."""

import contextlib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest import mock
from botocore.exceptions import ClientError

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy' / 'platform'
sys.path.insert(0, str(DEPLOY))
secrets_aws = importlib.import_module('runtime_secrets_aws')
fetcher = importlib.import_module('fetch_release_s3')
aws_bootstrap = importlib.import_module('aws_bootstrap')
release = fetcher.release
COMMIT = 'a' * 40


class SecretTests(unittest.TestCase):
    def test_existing_managed_values_do_not_write_or_reveal(self):
        api = mock.Mock()
        payload = {
            'superdan': 'synthetic-dan-' + 'a' * 32,
            'supervan': 'synthetic-van-' + 'b' * 32,
        }
        api.get_secret_value.return_value = {'SecretString': json.dumps(payload)}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                secrets_aws.value(api, secrets_aws.ACCOUNTS, True), payload
            )
        self.assertEqual(output.getvalue(), '')
        api.put_secret_value.assert_not_called()

    def test_initialization_requires_explicit_flag_and_no_existing_database(self):
        api = mock.Mock()
        api.get_secret_value.side_effect = ClientError(
            {'Error': {'Code': 'ResourceNotFoundException'}}, 'GetSecretValue'
        )
        with self.assertRaises(RuntimeError):
            secrets_aws.value(api, secrets_aws.DATABASE)
        with (
            mock.patch.object(Path, 'exists', return_value=True),
            self.assertRaises(RuntimeError),
        ):
            secrets_aws.value(api, secrets_aws.DATABASE, True)
        api.put_secret_value.assert_not_called()

    def test_wrong_dsn_cannot_silently_select_another_service(self):
        api = mock.Mock()
        api.get_secret_value.return_value = {
            'SecretString': json.dumps(
                {
                    'db_admin_password': 'synthetic-' + 'a' * 40,
                    'app_database_url': 'postgresql+psycopg://sixnine_app:synthetic-password@elsewhere:5432/sixnine',
                }
            )
        }
        with self.assertRaises(RuntimeError):
            secrets_aws.value(api, secrets_aws.DATABASE)


class FakeS3:
    def __init__(self, objects):
        self.objects = objects
        self.reads = []
        self.streams = []

    def get_object(self, **args):
        name = args['Key'].split('/')[-1]
        self.reads.append(name)
        stream = io.BytesIO(self.objects[name])
        self.streams.append(stream)
        return {'Body': stream, 'ContentLength': len(self.objects[name])}


class ReleaseDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'incoming').mkdir()
        (self.root / 'approved-releases').mkdir()
        # CI is an unprivileged Linux user. Emulate only the independently
        # provisioned approval directory/file UID; preserve modes, link count,
        # bytes, paths and every real checksum check.
        original_lstat = Path.lstat
        approval_root = self.root / 'approved-releases'

        def approval_metadata(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if path == approval_root or path.parent == approval_root:
                fields = list(info)
                fields[4] = 0
                return os.stat_result(fields)
            return info

        ownership = mock.patch.object(Path, 'lstat', approval_metadata)
        ownership.start()
        self.addCleanup(ownership.stop)
        self.objects = {name: ('synthetic ' + name).encode() for name in release.FILES}
        self.objects['release-manifest.json'] = json.dumps(
            {
                'commit': COMMIT,
                'image': 'sixnine-platform:' + COMMIT,
                'image_id': 'sha256:' + 'b' * 64,
                'files': {
                    name: hashlib.sha256(value).hexdigest()
                    for name, value in self.objects.items()
                },
            }
        ).encode()

    def tearDown(self):
        self.temp.cleanup()

    def approve(self):
        (self.root / 'approved-releases' / (COMMIT + '.sha256')).write_text(
            hashlib.sha256(self.objects['release-manifest.json']).hexdigest()
        )

    def test_no_approval_does_not_download_image_or_create_release(self):
        api = FakeS3(self.objects)
        with (
            mock.patch.object(release, 'check_host'),
            self.assertRaises(release.ReleaseError),
        ):
            fetcher.fetch(COMMIT, root=self.root, api=api)
        self.assertEqual(api.reads, ['release-manifest.json'])
        self.assertTrue(all(stream.closed for stream in api.streams))
        self.assertEqual(list((self.root / 'incoming').iterdir()), [])
        self.assertFalse(list(self.root.glob('.download-*')))

    def test_approved_bundle_exact_files_and_same_commit_retry(self):
        self.approve()
        with mock.patch.object(release, 'check_host'):
            fetcher.fetch(COMMIT, root=self.root, api=FakeS3(self.objects))
            fetcher.fetch(COMMIT, root=self.root, api=FakeS3(self.objects))
        target = self.root / 'incoming' / COMMIT
        self.assertEqual({p.name for p in target.iterdir()}, set(self.objects))
        self.assertFalse((self.root / 'release-state.json').exists())

    def test_modified_payload_rejected_and_streams_closed(self):
        self.approve()
        api = FakeS3({**self.objects, 'image.tar.gz': b'unapproved'})
        with (
            mock.patch.object(release, 'check_host'),
            self.assertRaises(release.ReleaseError),
        ):
            fetcher.fetch(COMMIT, root=self.root, api=api)
        self.assertTrue(all(stream.closed for stream in api.streams))
        self.assertFalse((self.root / 'incoming' / COMMIT).exists())


class BootstrapTests(unittest.TestCase):
    def test_secret_payload_is_stdin_only_fixed_code_and_first_time_only(self):
        self.assertIn('sys.stdin.buffer.read(4097)', aws_bootstrap.ACCOUNT_CODE)
        self.assertIn('present.intersection(value)', aws_bootstrap.ACCOUNT_CODE)
        self.assertNotIn('get_secret_value', aws_bootstrap.ACCOUNT_CODE)
        self.assertNotIn('print(password', aws_bootstrap.ACCOUNT_CODE)

    def test_actual_account_code_initializes_once_without_secret_output(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {
                **os.environ,
                'SIXNINE_DATA': directory,
                'SIXNINE_AUTH_MODE': 'password',
            }
            for key in (
                'SIXNINE_DATABASE_URL',
                'SIXNINE_DATABASE_URL_FILE',
                'SIXNINE_PUBLIC_ORIGIN',
            ):
                env.pop(key, None)
            source = (
                "import socket\nsocket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network forbidden'))\n"
                + aws_bootstrap.ACCOUNT_CODE
            )
            payload = json.dumps(
                {
                    'superdan': 'synthetic-dan-' + 'a' * 32,
                    'supervan': 'synthetic-van-' + 'b' * 32,
                }
            ).encode()
            first = subprocess.run(
                [sys.executable, '-c', source],
                input=payload,
                capture_output=True,
                env=env,
                cwd=DEPLOY.parent.parent,
                timeout=20,
            )
            self.assertEqual(first.returncode, 0, 'isolated account bootstrap failed')
            self.assertNotIn(b'synthetic-', first.stdout + first.stderr)
            second = subprocess.run(
                [sys.executable, '-c', source],
                input=payload,
                capture_output=True,
                env=env,
                cwd=DEPLOY.parent.parent,
                timeout=20,
            )
            self.assertEqual(second.returncode, 1)
            self.assertNotIn(b'synthetic-', second.stdout + second.stderr)


if __name__ == '__main__':
    unittest.main()
