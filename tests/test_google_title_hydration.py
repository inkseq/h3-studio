"""Optional Google configuration cannot gate database hydration; synthetic only."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from deploy.platform import runtime_secrets_aws as module
from studio_platform.google_title_config import GoogleTitleConfigError, validate_config

ROOT = Path(__file__).resolve().parents[1]
FAKE = {
    'enabled': True,
    'service': 'gemini',
    'profile': 'gemini--user-supplied',
    'base_url': 'https://generativelanguage.googleapis.com',
    'api_key': 'synthetic-private-key-for-test-only',
}


class TitleHydrationTests(unittest.TestCase):
    def test_each_closed_target_reads_only_its_own_title_secret(self):
        for target, source in (
            ('production', '/sixnine/platform/google-titles'),
            ('staging', '/sixnine/staging/google-titles'),
        ):
            with self.subTest(target=target):
                api = Mock()
                api.get_secret_value.return_value = {'SecretString': json.dumps(FAKE)}
                root = Mock()
                root.mkdir.side_effect = RuntimeError('test filesystem boundary')
                with patch.object(module, 'ROOT', root):
                    with self.assertRaisesRegex(RuntimeError, 'test filesystem boundary'):
                        module.hydrate_titles(api, target=target)
                api.get_secret_value.assert_called_once_with(SecretId=source)

    def test_missing_staging_secret_disables_without_production_fallback(self):
        api = Mock()
        api.get_secret_value.side_effect = RuntimeError('private upstream details')
        root = Mock()
        root.mkdir.side_effect = RuntimeError('test filesystem boundary')
        with patch.object(module, 'ROOT', root), patch('builtins.print') as output:
            with self.assertRaisesRegex(RuntimeError, 'test filesystem boundary'):
                module.hydrate_titles(api, target='staging')
        api.get_secret_value.assert_called_once_with(
            SecretId='/sixnine/staging/google-titles'
        )
        self.assertNotIn('private upstream details', str(output.call_args))

    def test_unknown_target_is_rejected_before_any_secret_or_runtime_write(self):
        for target in ('', 'test', 'Staging', None, ['staging']):
            with self.subTest(target=target):
                api, root = Mock(), Mock()
                with patch.object(module, 'ROOT', root):
                    with self.assertRaisesRegex(ValueError, 'deployment_target_invalid'):
                        module.hydrate_titles(api, target=target)
                api.get_secret_value.assert_not_called()
                root.mkdir.assert_not_called()

    def test_host_validator_loads_with_no_site_packages_and_an_unrelated_cwd(self):
        # Host copies this shared stdlib-only validator beside its protected helper.
        source = ROOT / 'studio_platform/google_title_config.py'
        with tempfile.TemporaryDirectory() as tmp:
            program = (
                "import runpy; ns=runpy.run_path("
                + repr(str(source))
                + "); assert ns['validate_config']({'enabled':False}) is None"
            )
            result = subprocess.run(
                [sys.executable, '-I', '-S', '-c', program],
                cwd=tmp,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    @unittest.skipIf(
        os.name == 'nt', 'Host atomic file ownership exercised on Linux CI'
    )
    def test_unavailable_or_invalid_google_disables_only_title_and_preserves_bound_inode(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o700)
            path = root / 'google_titles'
            original = Path.lstat

            def root_stat(path):
                values = list(original(path))
                values[4] = 0
                values[5] = 0 if path == root else 10001
                return os.stat_result(values)

            api = Mock()
            with (
                patch.object(module, 'ROOT', root),
                patch('pathlib.Path.lstat', root_stat),
                patch.object(module.os, 'fchown'),
            ):
                api.get_secret_value.return_value = {'SecretString': json.dumps(FAKE)}
                module.hydrate_titles(api)
                self.assertEqual(
                    validate_config(json.loads(path.read_text())).api_key,
                    FAKE['api_key'],
                )
                with path.open() as bound:
                    api.get_secret_value.side_effect = RuntimeError(
                        'synthetic-private upstream text'
                    )
                    with patch('builtins.print') as output:
                        module.hydrate_titles(api)
                    self.assertEqual(json.loads(path.read_text()), {'enabled': False})
                    self.assertEqual(json.loads(bound.read()), FAKE)
                    self.assertNotIn('synthetic-private', str(output.call_args))
                api.get_secret_value.side_effect = None
                for value in (
                    '{}',
                    '{"enabled":false,"enabled":true}',
                    json.dumps({**FAKE, 'base_url': 'https://wrong.example'}),
                ):
                    api.get_secret_value.return_value = {'SecretString': value}
                    with patch('builtins.print'):
                        module.hydrate_titles(api)
                    self.assertEqual(json.loads(path.read_text()), {'enabled': False})
                self.assertEqual(path.stat().st_mode & 0o777, 0o440)
                self.assertFalse(list(root.glob('.google-titles-*')))

    @unittest.skipIf(os.name == 'nt', 'Host atomic file ownership exercised on Linux CI')
    def test_staging_rotation_is_atomic_and_missing_secret_replaces_with_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o700)
            path = root / 'google_titles'
            original = Path.lstat

            def root_stat(path):
                values = list(original(path))
                values[4] = 0
                values[5] = 0 if path == root else 10001
                return os.stat_result(values)

            api = Mock()
            with (
                patch.object(module, 'ROOT', root),
                patch('pathlib.Path.lstat', root_stat),
                patch.object(module.os, 'fchown'),
            ):
                api.get_secret_value.return_value = {'SecretString': json.dumps(FAKE)}
                module.hydrate_titles(api, target='staging')
                with path.open() as bound:
                    api.get_secret_value.side_effect = RuntimeError('private staging error')
                    with patch('builtins.print') as output:
                        module.hydrate_titles(api, target='staging')
                    self.assertEqual(json.loads(path.read_text()), {'enabled': False})
                    self.assertEqual(json.loads(bound.read()), FAKE)
                    self.assertNotIn('private staging error', str(output.call_args))
                self.assertEqual(path.stat().st_mode & 0o777, 0o440)
                self.assertFalse(list(root.glob('.google-titles-*')))
            self.assertEqual(api.get_secret_value.call_count, 2)
            for call in api.get_secret_value.call_args_list:
                self.assertEqual(call.kwargs, {'SecretId': '/sixnine/staging/google-titles'})


if __name__ == '__main__':
    unittest.main()
