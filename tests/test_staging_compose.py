"""Real read-only Compose rendering; standard library only, no daemon/service IO."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from deploy.platform import check_config, deployment_target
from tools.check_migration_deploy import check as check_migration
from test_migration_deploy import fixture_environment

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI required; no service is started')
class StagingComposeTests(unittest.TestCase):
    def test_compact_profile_is_explicit_and_does_not_reduce_service_caps(self):
        from tools.check_migration_deploy import validate
        env = fixture_environment()
        selected = deployment_target.profile('staging')
        env.update(INKSEQ_DEPLOYMENT_TARGET='staging', INKSEQ_PUBLIC_ORIGIN=selected['origin'],
            INKSEQ_PUBLIC_ORIGIN_ALIASES='', INKSEQ_CADDY_PROFILE=selected['caddy'], INKSEQ_LEGACY_REDIRECT='0',
            INKSEQ_RESOURCE_PROFILE=deployment_target.COMPACT_RESOURCES,
            INKSEQ_CPU_CGROUP_PARENT=deployment_target.COMPACT_PARENT,
            SIXNINE_DB_ADMIN_SECRET_FILE='/run/sixnine-secrets/db_admin_password',
            SIXNINE_APP_DSN_SECRET_FILE='/run/sixnine-secrets/app_database_url')
        version = subprocess.check_output(['docker', 'compose', 'version', '--short'], text=True).strip()
        with tempfile.TemporaryDirectory() as temporary:
            empty = Path(temporary)/'empty.env'; empty.write_bytes(b'')
            base = json.loads(subprocess.check_output(['docker', 'compose', '--env-file', str(empty),
                '-f', str(ROOT/'deploy/platform/compose.yaml'), 'config', '--format', 'json'], env=env))
        self.assertTrue(check_config.validate(base, deployment_directory=ROOT/'deploy/platform',
            compose_version=version, target='staging', resource_environment=env))
        with self.assertRaises(check_config.ConfigurationError):
            check_config.validate(base, deployment_directory=ROOT/'deploy/platform', compose_version=version, target='staging')
        documents = []
        def render(command, **kwargs):
            result = subprocess.run(command, **kwargs)
            if result.returncode == 0: documents.append(json.loads(result.stdout))
            return result
        receipt = check_migration(ROOT/'deploy/platform/compose.yaml', ROOT/'deploy/dstack/platform-overlay.yaml',
            environment=env, runner=render, host_memory_mib=1840)
        self.assertEqual((receipt['service_memory_cap_mib'], receipt['parent_resident_mib'],
                          receipt['parent_swap_mib'], receipt['host_reserve_mib']), (2528, 1280, 2048, 560))
        document = documents[0]
        for name in document['services']:
            broken = copy.deepcopy(document); broken['services'][name].pop('cgroup_parent')
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'cgroup_parent_invalid'):
                validate(broken, target='staging', resource_environment=env, host_memory_mib=1840)
        broken = copy.deepcopy(document); broken['services']['dstack']['mem_limit'] = 512*1024**2
        with self.assertRaisesRegex(ValueError, 'service_caps_changed'):
            validate(broken, target='staging', resource_environment=env, host_memory_mib=1840)
        with self.assertRaisesRegex(ValueError, 'resource_profile_invalid'):
            validate(document, target='production', resource_environment=env, host_memory_mib=1840)
        with self.assertRaisesRegex(ValueError, 'cgroup_parent_invalid'):
            validate(document, target='staging', host_memory_mib=1840)

    def test_same_bundle_renders_two_closed_targets(self):
        version = subprocess.check_output(['docker', 'compose', 'version', '--short'], text=True).strip()
        for name in ('production', 'staging'):
            selected = deployment_target.profile(name)
            env = fixture_environment()
            env.update(INKSEQ_DEPLOYMENT_TARGET=name, INKSEQ_PUBLIC_ORIGIN=selected['origin'],
                INKSEQ_PUBLIC_ORIGIN_ALIASES=selected['aliases'], INKSEQ_CADDY_PROFILE=selected['caddy'],
                INKSEQ_LEGACY_REDIRECT='0',
                SIXNINE_DB_ADMIN_SECRET_FILE='/run/sixnine-secrets/db_admin_password',
                SIXNINE_APP_DSN_SECRET_FILE='/run/sixnine-secrets/app_database_url')
            with tempfile.TemporaryDirectory() as temporary:
                empty = Path(temporary)/'empty.env'; empty.write_bytes(b'')
                command = ['docker', 'compose', '--env-file', str(empty), '-f', str(ROOT/'deploy/platform/compose.yaml'),
                    'config', '--format', 'json']
                config = json.loads(subprocess.check_output(command, env=env, stderr=subprocess.DEVNULL))
            self.assertTrue(check_config.validate(config, deployment_directory=ROOT/'deploy/platform', compose_version=version, target=name))
            with self.assertRaises(check_config.ConfigurationError):
                check_config.validate(config, deployment_directory=ROOT/'deploy/platform', compose_version=version,
                    target='production' if name == 'staging' else 'staging')
            env.update(SIXNINE_STORAGE_PROVIDER='r2', SIXNINE_STORAGE_ENDPOINT='https://'+'a'*32+'.r2.cloudflarestorage.com',
                SIXNINE_STORAGE_REGION='auto', SIXNINE_STORAGE_BUCKET=selected['r2_bucket'],
                SIXNINE_STORAGE_PROFILE=selected['r2_profile'], SIXNINE_STORAGE_CREDENTIAL_FILE='/run/sixnine-secrets/r2_storage')
            receipt = check_migration(ROOT/'deploy/platform/compose.yaml', ROOT/'deploy/dstack/platform-overlay.yaml', environment=env)
            self.assertTrue(receipt)


if __name__ == '__main__':
    unittest.main()
