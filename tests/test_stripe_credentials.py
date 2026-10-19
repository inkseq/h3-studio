"""Test-only Stripe host hydration; synthetic secrets and no cloud calls."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from deploy.platform import runtime_secrets_aws as runtime

FAKE = {
    'version': 1,
    'account_id': 'acct_synthetic',
    'publishable_key': 'pk_test_' + 'p' * 32,
    'secret_key': 'sk_test_' + 's' * 32,
    'webhook_secret': 'whsec_' + 'w' * 32,
}


class StripeHydrationTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'The fixed runtime paths target Linux hosts')
    def test_maintenance_settings_keep_legacy_off_and_reject_unsafe_opt_in(self):
        from test_migration_release import maintenance

        with tempfile.TemporaryDirectory() as directory:
            environment = {
                key: str(Path(directory) / key) for key in maintenance.PATH_FIELDS
            }
            environment.update({key: 'fixture' for key in maintenance.OTHER_FIELDS})
            environment.update(
                SIXNINE_MIGRATION_GENERATION_ENABLED='0',
                SIXNINE_MIGRATION_EXECUTION_BACKEND='disabled',
                SIXNINE_OPERATOR_CAPACITY_OWNERS='supervan',
            )

            def candidate(**changes):
                return {
                    'version': 1,
                    'host_memory_mib': 4096,
                    'environment': {**environment, **changes},
                }

            original = candidate()
            self.assertEqual(maintenance.settings_document(original), original)
            for mode in ('off', 'test'):
                value = candidate(
                    INKSEQ_DEPLOYMENT_TARGET='staging',
                    INKSEQ_GENERATION_QUEUE='dbos',
                    INKSEQ_STRIPE_MODE=mode,
                    INKSEQ_STRIPE_CONFIG_FILE='/run/sixnine-secrets/stripe',
                )
                self.assertEqual(maintenance.settings_document(value), value)
            for changes in (
                {'INKSEQ_STRIPE_MODE': 'test'},
                {'INKSEQ_STRIPE_MODE': 'live'},
                {
                    'INKSEQ_DEPLOYMENT_TARGET': 'staging',
                    'INKSEQ_STRIPE_CONFIG_FILE': '/run/sixnine-secrets/workos',
                },
                {
                    'INKSEQ_DEPLOYMENT_TARGET': 'staging',
                    'INKSEQ_STRIPE_CONFIG_FILE': '/run/sixnine-secrets/stripe',
                },
                {
                    'INKSEQ_DEPLOYMENT_TARGET': 'production',
                    'INKSEQ_GENERATION_QUEUE': 'dbos',
                    'INKSEQ_STRIPE_CONFIG_FILE': '/run/sixnine-secrets/stripe',
                },
            ):
                with (
                    self.subTest(changes=changes),
                    self.assertRaisesRegex(
                        maintenance.release.ReleaseError,
                        '^migration_stripe_configuration_invalid$',
                    ),
                ):
                    maintenance.settings_document(candidate(**changes))

    def test_host_validator_has_no_application_or_sdk_dependency(self):
        source = (
            Path(__file__).resolve().parents[1] / 'studio_platform/stripe_config.py'
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
        self.assertEqual(result.returncode, 0, 'isolated Stripe validator failed')
        self.assertEqual(result.stdout + result.stderr, b'')

    def test_staging_reads_only_the_independent_secret_without_writes_or_output(self):
        api = Mock()
        api.get_secret_value.return_value = {'SecretString': json.dumps(FAKE)}
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(runtime, 'write_runtime') as write,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            runtime.hydrate_stripe(api, target='staging')
        api.get_secret_value.assert_called_once_with(SecretId='/sixnine/staging/stripe')
        self.assertEqual(write.call_args.args[0], 'stripe')
        self.assertEqual(json.loads(write.call_args.args[1]), FAKE)
        api.put_secret_value.assert_not_called()
        api.create_secret.assert_not_called()
        self.assertEqual(output.getvalue() + errors.getvalue(), '')
        api.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'requires staging'):
            runtime.hydrate_stripe(api, target='production')
        api.get_secret_value.assert_not_called()

    def test_invalid_or_unavailable_secret_preserves_the_bound_inode(self):
        api = Mock()
        for raw in (
            '{}',
            '{"version":1,"version":1}',
            'x' * 4097,
            json.dumps({**FAKE, 'secret_key': 'sk_live_' + 's' * 32}),
            json.dumps({'recipes': []}),
        ):
            api.get_secret_value.return_value = {'SecretString': raw}
            with patch.object(runtime, 'write_runtime') as write:
                with self.assertRaisesRegex(
                    RuntimeError,
                    '^Stripe managed configuration is unavailable or invalid$',
                ):
                    runtime.hydrate_stripe(api, target='staging')
                write.assert_not_called()
        api.get_secret_value.side_effect = RuntimeError(FAKE['secret_key'])
        with patch.object(runtime, 'write_runtime') as write:
            with self.assertRaisesRegex(
                RuntimeError, '^Stripe managed configuration is unavailable or invalid$'
            ):
                runtime.hydrate_stripe(api, target='staging')
            write.assert_not_called()

    @unittest.skipIf(
        os.name == 'nt', 'Root-owned Linux rollout metadata is exercised by CI'
    )
    def test_protected_rollout_requires_staging_fixed_file_and_durable_consumer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'migration').mkdir()
            self.assertFalse(runtime.stripe_requested(target='staging', root=root))
            path = root / 'migration/paths.json'
            original_lstat, original_fstat = Path.lstat, os.fstat

            def root_stat(info):
                values = list(info)
                values[4] = 0
                return os.stat_result(values)

            with (
                patch.object(
                    Path, 'lstat', lambda value: root_stat(original_lstat(value))
                ),
                patch.object(
                    runtime.os, 'fstat', lambda fd: root_stat(original_fstat(fd))
                ),
            ):

                def configure(**changes):
                    path.write_text(
                        json.dumps(
                            {
                                'environment': {
                                    'INKSEQ_DEPLOYMENT_TARGET': 'staging',
                                    'INKSEQ_GENERATION_QUEUE': 'dbos',
                                    **changes,
                                }
                            }
                        ),
                        encoding='utf-8',
                    )
                    path.chmod(0o600)

                configure()
                self.assertFalse(runtime.stripe_requested(target='staging', root=root))
                for mode in ('off', 'test'):
                    configure(
                        INKSEQ_STRIPE_MODE=mode,
                        INKSEQ_STRIPE_CONFIG_FILE='/run/sixnine-secrets/stripe',
                    )
                    self.assertTrue(
                        runtime.stripe_requested(target='staging', root=root)
                    )
                for changes in (
                    {'INKSEQ_STRIPE_MODE': 'test'},
                    {'INKSEQ_STRIPE_MODE': 'live'},
                    {'INKSEQ_STRIPE_CONFIG_FILE': '/run/sixnine-secrets/workos'},
                    {
                        'INKSEQ_STRIPE_CONFIG_FILE': '/run/sixnine-secrets/stripe',
                        'INKSEQ_DEPLOYMENT_TARGET': 'production',
                    },
                    {
                        'INKSEQ_STRIPE_CONFIG_FILE': '/run/sixnine-secrets/stripe',
                        'INKSEQ_GENERATION_QUEUE': 'postgres',
                    },
                ):
                    configure(**changes)
                    with (
                        self.subTest(changes=changes),
                        self.assertRaisesRegex(
                            RuntimeError, 'rollout configuration is invalid'
                        ),
                    ):
                        runtime.stripe_requested(target='staging', root=root)
                configure(INKSEQ_STRIPE_CONFIG_FILE='/run/sixnine-secrets/stripe')
                with self.assertRaisesRegex(
                    RuntimeError, 'rollout configuration is invalid'
                ):
                    runtime.stripe_requested(target='production', root=root)


if __name__ == '__main__':
    unittest.main()
