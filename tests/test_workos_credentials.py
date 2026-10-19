"""WorkOS credential transport boundaries; synthetic values and fake AWS only."""

import contextlib
import ctypes
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from deploy.platform import runtime_secrets_aws as runtime
from studio_platform.settings import Settings
from studio_platform.workos_config import load_credentials, validate_credentials
from tools import import_workos_credentials as importer

FAKE = {'client_id': 'client_' + 'a' * 26, 'api_key': 'sk_test_' + 'b' * 40}
OTHER = {**FAKE, 'api_key': 'sk_test_' + 'c' * 40}
VERSION = '11111111-1111-4111-8111-111111111111'


class NotFound(ClientError):
    def __init__(self):
        super().__init__(
            {'Error': {'Code': 'ResourceNotFoundException'}}, 'GetSecretValue'
        )


class FakeSecrets:
    exceptions = types.SimpleNamespace(ResourceNotFoundException=NotFound)

    def __init__(self, *, target='staging', existing=None, container=False):
        self.name = importer.selection(target)[1]
        self.arn = f'arn:aws:secretsmanager:{importer.REGION}:{importer.ACCOUNT}:secret:{self.name}-Abc123'
        self.exists = container or existing is not None
        self.versions = {VERSION: json.dumps(existing)} if existing else {}
        self.current = VERSION if existing else None
        self.writes = []
        self.reads = []

    def get_secret_value(self, **arguments):
        self.reads.append(arguments)
        assert arguments['SecretId'] == self.name
        version = arguments.get('VersionId', self.current)
        if not self.exists or version not in self.versions:
            raise NotFound()
        return dict(
            Name=self.name,
            ARN=self.arn,
            VersionId=version,
            SecretString=self.versions[version],
        )

    def describe_secret(self, **arguments):
        assert arguments['SecretId'] == self.name
        if not self.exists:
            raise NotFound()
        return dict(
            Name=self.name,
            ARN=self.arn,
            VersionIdsToStages={key: ['AWSCURRENT'] for key in self.versions},
        )

    def create_secret(self, **arguments):
        assert arguments['Name'] == self.name
        self.exists = True
        self.writes.append(('create', arguments))
        self.current = arguments['ClientRequestToken']
        self.versions[self.current] = arguments['SecretString']

    def put_secret_value(self, **arguments):
        assert arguments['SecretId'] == self.name
        self.writes.append(('put', arguments))
        self.current = arguments['ClientRequestToken']
        self.versions[self.current] = arguments['SecretString']


def fake_session(api, *, account=importer.ACCOUNT):
    sts = Mock()
    sts.get_caller_identity.return_value = {'Account': account}
    session = Mock()
    session.client.side_effect = lambda service, **_kwargs: (
        sts if service == 'sts' else api
    )
    return session


class WorkOSConfigTests(unittest.TestCase):
    def test_host_validator_runs_without_the_app_sdk_or_site_packages(self):
        source = (
            Path(__file__).resolve().parents[1] / 'studio_platform/workos_config.py'
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    '-I',
                    '-S',
                    '-c',
                    "import runpy; value=runpy.run_path("
                    + repr(str(source))
                    + "); assert value['load_credentials'](None) is None",
                ],
                cwd=directory,
                capture_output=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, 'isolated host validator failed')
        self.assertEqual(result.stdout, b'')

    def test_unset_does_not_read_files_and_wrong_shape_cannot_enable_authkit(self):
        with patch.object(Path, 'lstat', side_effect=AssertionError('unexpected read')):
            self.assertIsNone(load_credentials(None))
        for value in (
            {},
            {'enabled': True},
            {**FAKE, 'origin': 'https://wrong.example'},
            {**FAKE, 'api_key': 'invalid-private-canary'},
            {**FAKE, 'client_id': 'client_\nprivate-canary'},
        ):
            with self.subTest(fields=sorted(value)):
                with self.assertRaisesRegex(
                    ValueError, '^workos_configuration_invalid$'
                ):
                    validate_credentials(value)

    def test_file_bounds_duplicates_links_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'workos.json'
            path.write_text(json.dumps(FAKE), encoding='utf-8')
            path.chmod(0o600)
            self.assertEqual(load_credentials(path), FAKE)
            for content in (
                '{"client_id":"duplicate","client_id":"again"}',
                'x' * 4097,
                json.dumps({'recipes': []}),
            ):
                path.write_text(content, encoding='utf-8')
                with self.assertRaisesRegex(
                    ValueError, '^workos_configuration_invalid$'
                ):
                    load_credentials(path)
            path.write_text(json.dumps(FAKE), encoding='utf-8')
            link = Path(directory) / 'hardlink'
            os.link(path, link)
            with self.assertRaisesRegex(ValueError, '^workos_configuration_invalid$'):
                load_credentials(path)
            link.unlink()
            if os.name != 'nt':
                path.chmod(0o644)
                with self.assertRaisesRegex(
                    ValueError, '^workos_configuration_invalid$'
                ):
                    load_credentials(path)

    def test_settings_only_explicit_absolute_path(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(Settings.from_environment().workos_config_file)
            with patch.dict(
                os.environ, {'INKSEQ_WORKOS_CONFIG_FILE': str(Path.cwd() / 'workos')}
            ):
                self.assertEqual(
                    Settings.from_environment().workos_config_file,
                    Path.cwd() / 'workos',
                )
        with self.assertRaisesRegex(
            ValueError, 'WorkOS credential path must be absolute'
        ):
            Settings(data_dir=Path.cwd(), workos_config_file=Path('relative'))


class WorkOSImportTests(unittest.TestCase):
    def test_destination_account_is_verified_before_reading_owner_credential(self):
        reader = Mock(return_value=FAKE)
        api = FakeSecrets()
        with self.assertRaisesRegex(
            importer.ImportErrorSafe, 'aws_account_unconfirmed'
        ):
            importer.import_existing(
                'staging',
                session=fake_session(api, account='000000000000'),
                read_credential=reader,
            )
        reader.assert_not_called()
        self.assertEqual(api.reads, [])
        self.assertEqual(api.writes, [])

    def test_fixed_sources_and_readback_no_rotation_or_file_output(self):
        for target, expected in importer.SOURCES.items():
            with self.subTest(target=target):
                api = FakeSecrets(target=target)
                reader = Mock(return_value=FAKE)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = importer.import_existing(
                        target, session=fake_session(api), read_credential=reader
                    )
                    repeated = importer.import_existing(
                        target, session=fake_session(api), read_credential=reader
                    )
                self.assertEqual(result['status'], 'imported')
                self.assertEqual(
                    repeated, {'status': 'verified', 'version_id': result['version_id']}
                )
                self.assertEqual(reader.call_args.args, (expected,))
                self.assertEqual(len(api.writes), 1)
                self.assertEqual(api.writes[0][0], 'create')
                self.assertEqual(
                    api.reads[-2], {'SecretId': api.name, 'VersionStage': 'AWSCURRENT'}
                )
                self.assertEqual(output.getvalue(), '')

    def test_existing_container_is_initialized_but_existing_key_is_never_changed(self):
        api = FakeSecrets(container=True)
        importer.import_existing(
            'staging', session=fake_session(api), read_credential=lambda _: FAKE
        )
        self.assertEqual(api.writes[0][0], 'put')
        with self.assertRaisesRegex(
            importer.ImportErrorSafe, 'rotation_not_authorized'
        ):
            importer.import_existing(
                'staging', session=fake_session(api), read_credential=lambda _: OTHER
            )
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(json.loads(api.versions[api.current]), FAKE)

    def test_other_destination_or_stale_history_cannot_receive_credentials(self):
        api = FakeSecrets(existing=FAKE)
        api.arn = api.arn.replace(importer.ACCOUNT, '000000000000')
        with self.assertRaisesRegex(
            importer.ImportErrorSafe, 'destination_unconfirmed'
        ):
            importer.import_existing(
                'staging', session=fake_session(api), read_credential=lambda _: FAKE
            )
        self.assertEqual(api.writes, [])
        api = FakeSecrets(existing=FAKE)
        api.current = None
        with self.assertRaisesRegex(
            importer.ImportErrorSafe, 'existing_secret_requires_review'
        ):
            importer.import_existing(
                'staging', session=fake_session(api), read_credential=lambda _: FAKE
            )
        self.assertEqual(api.writes, [])

    def test_cli_never_prints_upstream_secret_diagnostics(self):
        error = ClientError(
            {'Error': {'Code': 'AccessDeniedException', 'Message': FAKE['api_key']}},
            'PutSecretValue',
        )
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(importer, 'import_existing', side_effect=error),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(importer.main(['--target', 'staging']), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(errors.getvalue().strip(), 'workos_credential_import_failed')
        self.assertNotIn(FAKE['api_key'], output.getvalue() + errors.getvalue())
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            importer.main(['--target', 'staging', '--api-key', FAKE['api_key']])
        self.assertNotIn(FAKE['api_key'], errors.getvalue())

    def test_windows_read_selects_only_exact_target_and_frees_memory_copy(self):
        storage = {}

        def read(target, kind, flags, output):
            record = output._obj._type_()
            blob = FAKE['api_key'].encode('utf-16-le')
            buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
            record.Type, record.TargetName, record.UserName = (
                kind,
                target,
                FAKE['client_id'],
            )
            record.CredentialBlobSize, record.CredentialBlob = len(blob), buffer
            storage.update(record=record, buffer=buffer)
            output._obj.contents = record
            return True

        api = types.SimpleNamespace(CredReadW=Mock(side_effect=read), CredFree=Mock())
        with (
            patch.object(importer.os, 'name', 'nt'),
            patch.object(importer.ctypes, 'WinDLL', return_value=api, create=True),
        ):
            self.assertEqual(
                importer.read_windows_credential('inkseq/workos-staging'), FAKE
            )
            with self.assertRaisesRegex(importer.ImportErrorSafe, 'unavailable'):
                importer.read_windows_credential('inkseq/workos-other')
        self.assertEqual(api.CredReadW.call_count, 1)
        api.CredFree.assert_called_once()
        self.assertTrue(all(value == 0 for value in storage['buffer']))


class WorkOSHydrationTests(unittest.TestCase):
    @unittest.skipIf(
        os.name == 'nt', 'Protected root-owned host metadata exercised on Linux CI'
    )
    def test_rollout_configuration_is_opt_in_fixed_path_and_environment_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'migration').mkdir()
            self.assertFalse(runtime.workos_requested(target='staging', root=root))
            path = root / 'migration' / 'paths.json'
            original_lstat, original_fstat = Path.lstat, os.fstat

            def owned_lstat(path):
                values = list(original_lstat(path))
                values[4] = 0
                return os.stat_result(values)

            def owned_fstat(fd):
                values = list(original_fstat(fd))
                values[4] = 0
                return os.stat_result(values)

            with (
                patch.object(Path, 'lstat', owned_lstat),
                patch.object(runtime.os, 'fstat', owned_fstat),
            ):
                for requested, expected in (
                    ('', False),
                    ('/run/sixnine-secrets/workos', True),
                ):
                    path.write_text(
                        json.dumps(
                            {
                                'environment': {
                                    'INKSEQ_DEPLOYMENT_TARGET': 'staging',
                                    'INKSEQ_WORKOS_CONFIG_FILE': requested,
                                }
                            }
                        ),
                        encoding='utf-8',
                    )
                    path.chmod(0o600)
                    self.assertEqual(
                        runtime.workos_requested(target='staging', root=root), expected
                    )
                with self.assertRaisesRegex(
                    RuntimeError, 'rollout configuration is invalid'
                ):
                    runtime.workos_requested(target='production', root=root)
                path.write_text(
                    json.dumps(
                        {
                            'environment': {
                                'INKSEQ_DEPLOYMENT_TARGET': 'staging',
                                'INKSEQ_WORKOS_CONFIG_FILE': '/run/sixnine-secrets/app_database_url',
                            }
                        }
                    ),
                    encoding='utf-8',
                )
                with self.assertRaisesRegex(
                    RuntimeError, 'rollout configuration is invalid'
                ):
                    runtime.workos_requested(target='staging', root=root)

    def test_only_selected_independent_secret_and_protected_writer_are_used(self):
        for target in importer.SOURCES:
            api = Mock()
            api.get_secret_value.return_value = {'SecretString': json.dumps(FAKE)}
            with patch.object(runtime, 'write_runtime') as write:
                runtime.hydrate_workos(api, target=target)
            api.get_secret_value.assert_called_once_with(
                SecretId=importer.selection(target)[1]
            )
            self.assertEqual(write.call_args.args[0], 'workos')
            self.assertEqual(json.loads(write.call_args.args[1]), FAKE)
            api.put_secret_value.assert_not_called()

    def test_invalid_or_missing_secret_never_overwrites_bound_file(self):
        api = Mock()
        for response in (
            '{}',
            '{"client_id":"duplicate","client_id":"again"}',
            json.dumps({'recipes': []}),
        ):
            api.get_secret_value.return_value = {'SecretString': response}
            with patch.object(runtime, 'write_runtime') as write:
                with self.assertRaisesRegex(RuntimeError, 'unavailable or invalid'):
                    runtime.hydrate_workos(api)
                write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
