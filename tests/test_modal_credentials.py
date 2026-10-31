"""Existing-secret transport only; fake APIs, no keys, AWS or GPU calls."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deploy.platform import modal_credentials as host
from studio_platform import modal_controller, modal_credentials as cpu
from test_modal_budget import ModalBudgetCase

FAKE = {'MODAL_TOKEN_ID': 'fake-id', 'MODAL_TOKEN_SECRET': 'fake-token'}


class CredentialTransportTests(unittest.TestCase):
    def response(self):
        return {
            'Name': host.SECRET,
            'ARN': 'arn:aws:secretsmanager:ap-southeast-1:829135631045:secret:/sixnine/staging/modal-abc123',
            'VersionId': '12345678-1234-1234-1234-123456789012',
            'VersionStages': ['AWSCURRENT'],
            'SecretString': json.dumps(FAKE),
        }

    def test_only_existing_current_secret_is_read_and_return_is_metadata(self):
        api, writer = Mock(), Mock()
        api.get_secret_value.return_value = self.response()
        result = host.hydrate(api, writer, target='staging')
        self.assertEqual(
            api.mock_calls,
            [
                unittest.mock.call.get_secret_value(
                    SecretId='/sixnine/staging/modal', VersionStage='AWSCURRENT'
                )
            ],
        )
        self.assertEqual(json.loads(writer.call_args.args[0]), FAKE)
        self.assertEqual(set(result), {'state', 'name', 'version_id'})
        self.assertNotIn('fake-token', json.dumps(result))

    def test_production_refuses_before_even_reading_a_secret(self):
        api, writer = Mock(), Mock()
        with self.assertRaisesRegex(ValueError, 'modal_staging_only'):
            host.hydrate(api, writer, target='production')
        api.assert_not_called()
        self.assertFalse(api.mock_calls)
        writer.assert_not_called()

    def test_wrong_account_region_name_version_or_schema_cannot_write(self):
        for field, value in (
            ('Name', '/sixnine/platform/modal'),
            ('ARN', self.response()['ARN'].replace('829135631045', '111111111111')),
            ('ARN', self.response()['ARN'].replace('ap-southeast-1', 'us-east-1')),
            ('VersionId', 'unknown'),
            ('VersionStages', ['AWSPREVIOUS']),
            ('VersionStages', 'AWSCURRENT'),
            (
                'SecretString',
                '{"MODAL_TOKEN_ID":"a","MODAL_TOKEN_ID":"b","MODAL_TOKEN_SECRET":"c"}',
            ),
            ('SecretString', json.dumps({**FAKE, 'AWS_ACCESS_KEY_ID': 'fake-aws'})),
        ):
            with self.subTest(field=field, value=value):
                api, writer = Mock(), Mock()
                api.get_secret_value.return_value = {**self.response(), field: value}
                with self.assertRaisesRegex(
                    ValueError, 'modal_existing_staging_secret_invalid'
                ):
                    host.hydrate(api, writer, target='staging')
                writer.assert_not_called()

    def test_denied_or_unknown_read_does_not_try_another_source(self):
        api, writer = Mock(), Mock()
        api.get_secret_value.side_effect = TimeoutError('fake upstream failure')
        with self.assertRaises(TimeoutError):
            host.hydrate(api, writer, target='staging')
        self.assertEqual(api.get_secret_value.call_count, 1)
        writer.assert_not_called()

    def test_host_sdk_refuses_wrong_host_before_client_or_credentials(self):
        with (
            patch.object(host.os, 'geteuid', return_value=1, create=True),
            patch('boto3.client') as api,
        ):
            with self.assertRaisesRegex(ValueError, 'modal_staging_host_required'):
                host.client()
            api.assert_not_called()

    def test_private_file_rejects_wrong_owner_links_and_mutable_permissions(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / cpu.FILENAME
            path.write_text(json.dumps(FAKE))
            info = path.stat()
            fields = {
                k: getattr(info, k)
                for k in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
            }
            good = {
                **fields,
                'st_uid': 0,
                'st_gid': 10001,
                'st_nlink': 1,
                'st_mode': stat.S_IFREG | 0o440,
            }
            self.assertTrue(cpu.protected_file(SimpleNamespace(**good), posix=True))
            with patch.object(cpu.os, 'fstat', return_value=SimpleNamespace(**good)):
                self.assertEqual(cpu.read(path), FAKE)
            for change in (
                {'st_uid': 10001},
                {'st_gid': 0},
                {'st_nlink': 2},
                {'st_mode': stat.S_IFREG | 0o660},
            ):
                with self.subTest(change=change):
                    self.assertFalse(
                        cpu.protected_file(
                            SimpleNamespace(**{**good, **change}), posix=True
                        )
                    )

    def test_token_schema_rejects_extra_fields_blank_or_control_characters(self):
        for value in (
            {},
            {**FAKE, 'aws': 'x'},
            {**FAKE, 'MODAL_TOKEN_SECRET': ''},
            {**FAKE, 'MODAL_TOKEN_SECRET': 'fake\nkey'},
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    ValueError, 'modal_existing_credentials_invalid'
                ),
            ):
                cpu.validate(value)

    def test_host_reuses_identical_inode_and_refuses_rotation_or_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            directory = SimpleNamespace(
                parents=(),
                lstat=lambda: SimpleNamespace(st_mode=stat.S_IFDIR | 0o700),
                is_symlink=lambda: False,
            )

            class RuntimeDirectory:
                parents = directory.parents
                lstat = staticmethod(directory.lstat)
                is_symlink = staticmethod(directory.is_symlink)

                def __truediv__(self, name):
                    return root / name

            original_fstat = os.fstat

            def file_info(fd):
                actual = original_fstat(fd)
                return SimpleNamespace(
                    st_mode=stat.S_IFREG | 0o440,
                    st_nlink=actual.st_nlink,
                    st_uid=0,
                    st_gid=10001,
                    st_size=actual.st_size,
                )

            raw = json.dumps(FAKE).encode()
            path = root / cpu.FILENAME
            with (
                patch.object(host, 'DIRECTORY', RuntimeDirectory()),
                patch.object(
                    host,
                    'Path',
                    return_value=SimpleNamespace(read_text=lambda: host.INSTANCE),
                ),
                patch.object(host.os, 'geteuid', return_value=0, create=True),
                patch.object(host.os, 'fstat', side_effect=file_info),
                patch.object(host.os, 'fchown', create=True),
                patch.object(host.os, 'fchmod', create=True),
                patch.object(
                    host.os, 'O_NOFOLLOW', getattr(os, 'O_NOFOLLOW', 0), create=True
                ),
            ):
                host.write_runtime(raw)
                before = (path.stat().st_ino, path.read_bytes())
                host.write_runtime(raw)
                self.assertEqual((path.stat().st_ino, path.read_bytes()), before)
                with self.assertRaisesRegex(ValueError, 'rotation_required'):
                    host.write_runtime(
                        json.dumps(
                            {**FAKE, 'MODAL_TOKEN_SECRET': 'different-fake'}
                        ).encode()
                    )
                self.assertEqual((path.stat().st_ino, path.read_bytes()), before)


class ControllerCredentialBoundaryTests(ModalBudgetCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.service = SimpleNamespace(
            repo=self.repo,
            settings=SimpleNamespace(
                public_origin='https://staging.inkseq.com', generation_queue='dbos'
            ),
        )
        self.factory = Mock(return_value=(self.client, Mock()))

    def config(self, enabled, path=None):
        value = dict(
            version=1,
            enabled=enabled,
            host=asdict(self.spec),
            credentials_file=str(self.root / cpu.FILENAME) if path is None else path,
        )
        file = self.root / 'modal.json'
        file.write_text(json.dumps(value))
        file.chmod(0o600)

    def test_disabled_config_does_not_read_tokens_or_authenticate(self):
        self.config(False)
        self.assertIsNone(
            modal_controller.load_extension(
                self.service, self.root, client_factory=self.factory
            )
        )
        self.factory.assert_not_called()

    def test_explicit_file_stays_out_of_the_provider_host_spec(self):
        self.config(True)
        extension = modal_controller.load_extension(
            self.service, self.root, client_factory=self.factory
        )
        self.factory.assert_called_once_with(
            self.spec, credentials_file=str(self.root / cpu.FILENAME)
        )
        self.assertTrue(extension.admission.enabled)
        self.assertNotIn('credentials_file', asdict(extension.hosts.spec))

    def test_other_files_and_production_are_refused_before_auth(self):
        self.config(False, str(self.root / '..' / cpu.FILENAME))
        with self.assertRaises(ValueError):
            modal_controller.load_extension(
                self.service, self.root, client_factory=self.factory
            )
        self.config(True)
        self.service.settings.public_origin = 'https://inkseq.com'
        with self.assertRaises(ValueError):
            modal_controller.load_extension(
                self.service, self.root, client_factory=self.factory
            )
        self.factory.assert_not_called()

    def test_bad_selected_file_never_falls_back_to_aws(self):
        sdk = SimpleNamespace(__version__='1.6.3', Client=Mock())
        config = SimpleNamespace(
            config={'server_url': 'https://api.modal.com,https://api.modal2.com'}
        )
        with (
            patch.dict(sys.modules, {'modal': sdk, 'modal.config': config}),
            patch.object(
                cpu, 'read', side_effect=ValueError('private-value-must-not-leak')
            ),
            patch('boto3.client') as aws,
        ):
            with self.assertRaisesRegex(
                ValueError, '^modal_existing_staging_client_unavailable$'
            ) as caught:
                modal_controller.client_from_staging_secret(
                    self.spec, credentials_file=str(self.root / cpu.FILENAME)
                )
            self.assertIsNone(caught.exception.__cause__)
            aws.assert_not_called()
            sdk.Client.from_credentials.assert_not_called()
