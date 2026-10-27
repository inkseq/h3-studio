"""Offline rollout boundaries; never load credentials or start containers."""

import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools.check_migration_deploy import ROOT, check
from tools.inky_deploy_validation import validate_inky


class InkyDeploymentTests(unittest.TestCase):
    def document(self, enabled=True):
        return {
            'services': {
                'app': {
                    'environment': {
                        'INKSEQ_INKY_CONFIG_FILE': '/run/secrets/inky'
                        if enabled
                        else ''
                    },
                    'secrets': ['inky'],
                },
                'controller': {'environment': {}, 'secrets': []},
            },
            'secrets': {
                'inky': {
                    'file': '/run/sixnine-secrets/inky' if enabled else '/recipes'
                },
                'execution_profiles': {'file': '/recipes'},
            },
        }

    def test_old_and_disabled_rollouts_need_no_new_secret(self):
        validate_inky(self.document(False))
        old = self.document(False)
        old['services']['app'] = {'environment': {}, 'secrets': []}
        del old['secrets']['inky']
        validate_inky(old)
        old['services']['app']['secrets'] = ['inky']
        with self.assertRaises(ValueError):
            validate_inky(old)

    def test_credentials_cannot_reach_controller_or_another_path(self):
        original = self.document()
        validate_inky(original)
        for mutate in (
            lambda d: d['services']['controller']['secrets'].append('inky'),
            lambda d: d['services']['controller']['environment'].update(
                INKSEQ_INKY_CONFIG_FILE=''
            ),
            lambda d: d['services']['app']['environment'].update(
                INKSEQ_INKY_CONFIG_FILE='/run/secrets/workos'
            ),
            lambda d: d['secrets']['inky'].update(file='/run/sixnine-secrets/workos'),
            lambda d: d['services']['app'].update(
                secrets=[{'source': 'inky', 'target': '/run/secrets/workos'}]
            ),
        ):
            changed = copy.deepcopy(original)
            mutate(changed)
            with self.assertRaises(ValueError):
                validate_inky(changed)

    @unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI required')
    def test_rendered_stack_keeps_opt_in_on_app_only(self):
        from test_migration_deploy import fixture_environment

        for selected in ('', '/run/sixnine-secrets/inky'):
            environment = fixture_environment()
            environment['INKSEQ_INKY_CONFIG_FILE'] = selected
            rendered = {}

            def render(command, **kwargs):
                result = subprocess.run(command, **kwargs)
                if result.returncode == 0:
                    rendered.update(json.loads(result.stdout))
                return result

            check(
                ROOT / 'deploy/platform/compose.yaml',
                ROOT / 'deploy/dstack/platform-overlay.yaml',
                environment=environment,
                runner=render,
            )
            validate_inky(rendered)
            self.assertEqual(
                rendered['services']['app']['environment']['INKSEQ_INKY_CONFIG_FILE'],
                '/run/secrets/inky' if selected else '',
            )
            self.assertEqual(
                rendered['secrets']['inky']['file'],
                selected or environment['SIXNINE_EXECUTION_PROFILES_SECRET_FILE'],
            )
            for name, service in rendered['services'].items():
                if name != 'app':
                    self.assertNotIn(
                        'INKSEQ_INKY_CONFIG_FILE', service.get('environment', {})
                    )

    def test_host_activation_is_deferred_until_qualified_helper_install(self):
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
            candidate = {
                'version': 1,
                'host_memory_mib': 4096,
                'environment': environment,
            }
            self.assertEqual(maintenance.settings_document(candidate), candidate)
            for selected in ('', '/run/sixnine-secrets/inky', '/run/sixnine-secrets/workos'):
                environment['INKSEQ_INKY_CONFIG_FILE'] = selected
                with self.subTest(selected=selected), self.assertRaisesRegex(
                    maintenance.release.ReleaseError, 'migration_paths_invalid'
                ):
                    maintenance.settings_document(candidate)
