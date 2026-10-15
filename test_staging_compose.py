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

ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI required; no service is started')
class StagingComposeTests(unittest.TestCase):
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
