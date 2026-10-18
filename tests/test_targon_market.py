"""Offline deployment boundary checks; no Docker, systemd, AWS or supplier calls."""

from contextlib import ExitStack, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tarfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
release = ModuleType('release')
release.ROOT = Path('/srv/sixnine')
release.ReleaseError = type('ReleaseError', (Exception,), {})


def require(condition, code):
    if not condition:
        raise release.ReleaseError(code)


release.require = require
release.command = Mock(side_effect=AssertionError('docker_forbidden'))
release.sync_directory = Mock()
release.deployment_environment = Mock(return_value={'PATH': '/usr/bin'})
release.canonical_hash = lambda value: hashlib.sha256(
    json.dumps(value, sort_keys=True).encode()
).hexdigest()
with patch.dict(sys.modules, {'release': release}):
    spec = importlib.util.spec_from_file_location(
        'targon_market_offline', ROOT / 'deploy' / 'platform' / 'targon_market.py'
    )
    market = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(market)


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        release.command.reset_mock(side_effect=True)
        release.command.side_effect = AssertionError('docker_forbidden')

    def result(self):
        return {
            'provider': 'targon',
            'status': 'ok',
            'observed_at': 105,
            'started_at': 104,
            'finished_at': 106,
            'fresh_success': True,
            'offer_count': 2,
        }

    def test_service_has_only_database_secret_and_no_runtime_mounts(self):
        value = market.service('sha256:' + 'a' * 64)
        self.assertEqual(value['image'], 'sha256:' + 'a' * 64)
        self.assertEqual(value['user'], '10001:10001')
        self.assertEqual(
            value['secrets'],
            [{'source': 'app_database_url', 'target': '/run/secrets/app_database_url'}],
        )
        self.assertNotIn('volumes', value)
        self.assertNotIn('ports', value)
        self.assertEqual(set(value['networks']), {'database', 'edge'})
        self.assertEqual(value['command'], ['python', '-c', market.ONCE])
        self.assertEqual(value['environment']['SIXNINE_EXECUTION_BACKEND'], 'disabled')
        self.assertEqual(value['environment']['SIXNINE_GENERATION_ENABLED'], '0')
        self.assertEqual(value['cap_drop'], ['ALL'])

    def test_exit_zero_with_error_stale_or_future_row_is_not_acceptance(self):
        market.validate_result(self.result(), 100, 110)
        for changed in (
            {'status': 'error'},
            {'fresh_success': False},
            {'observed_at': 99},
            {'observed_at': 111},
            {'observed_at': True},
        ):
            with self.subTest(changed=changed), self.assertRaises(release.ReleaseError):
                market.validate_result({**self.result(), **changed}, 100, 110)
        with self.assertRaises(release.ReleaseError):
            market.validate_result(self.result(), 100, 226)

    def test_rendered_configuration_rejects_extra_credentials_or_runtime_mount(self):
        image = 'sha256:' + 'a' * 64
        market.validate_rendered(market.service(image), image)
        value = market.service(image)
        value['environment']['AWS_ACCESS_KEY_ID'] = 'not-a-secret-offline-test'
        with self.assertRaises(release.ReleaseError):
            market.validate_rendered(value, image)
        value = market.service(image)
        value['volumes'] = [
            {'source': '/srv/sixnine/operator-capacity', 'target': '/private'}
        ]
        with self.assertRaises(release.ReleaseError):
            market.validate_rendered(value, image)

    def test_sample_is_bounded_and_acceptance_follows_verified_cache(self):
        with (
            patch.object(
                market,
                'pinned',
                return_value=({'pin': 'pin'}, Path('/approved'), {'PATH': '/usr/bin'}),
            ),
            patch.object(market, 'active', return_value={'active': False}),
            patch.object(market, 'existing', return_value=''),
            patch.object(market, 'cleanup') as cleanup,
            patch.object(market, 'atomic') as atomic,
            patch.object(market.time, 'time', side_effect=[100, 110]),
        ):
            release.command.side_effect = None
            release.command.return_value = json.dumps(self.result()).encode()
            result = market.sample(acceptance=True)
            self.assertEqual(result['pin'], 'pin')
            args = release.command.call_args.args[0]
            self.assertIn('--no-deps', args)
            self.assertIn('--rm', args)
            self.assertEqual(args[-1], market.SERVICE)
            self.assertEqual(release.command.call_args.kwargs['timeout'], 90)
            self.assertEqual(atomic.call_args.args[0].name, 'accepted.json')
            cleanup.assert_called_once()

    def test_start_rejects_stale_acceptance_before_systemd(self):
        with (
            patch.object(market, 'pinned', return_value=({'pin': 'pin'}, None, None)),
            patch.object(market, 'read', return_value={'pin': 'pin', **self.result()}),
            patch.object(market.time, 'time', return_value=226),
            patch.object(market, 'systemctl') as systemctl,
        ):
            with self.assertRaises(release.ReleaseError):
                market.start()
            systemctl.assert_not_called()

    def test_cleanup_cannot_remove_other_or_controller_container(self):
        record = {
            'commit': 'commit',
            'image_id': 'image',
            'archive_image_ids': ['image'],
        }
        record['pin'] = release.canonical_hash(record)
        info = {
            'Name': '/' + market.CONTAINER,
            'Image': 'image',
            'Config': {
                'Labels': {
                    market.LABEL: record['pin'],
                    'com.docker.compose.service': 'operator-controller',
                }
            },
        }
        with (
            patch.object(market, 'read', return_value=record),
            patch.object(market, 'existing', return_value='id'),
        ):
            release.command.side_effect = None
            release.command.return_value = json.dumps([info]).encode()
            with self.assertRaisesRegex(
                release.ReleaseError, 'cleanup_identity_mismatch'
            ):
                market.cleanup()
            self.assertEqual(release.command.call_count, 1)
            self.assertEqual(release.command.call_args.args[0][0], 'inspect')

    def test_timer_scopes_stop_and_refreshes_within_cache_window(self):
        self.assertIn('OnUnitInactiveSec=30', market.timer_text())
        self.assertIn(
            'ExecStopPost=/usr/bin/python3 /opt/sixnine-release/targon_market.py cleanup',
            market.unit_text(),
        )
        self.assertIn('TimeoutStartSec=100', market.unit_text())
        self.assertIn('TimeoutStopSec=30', market.unit_text())
        self.assertNotIn('operator-controller', market.unit_text())


class OCIIdentityTests(unittest.TestCase):
    def setUp(self):
        # Reuse the release suite's inert OCI fixture and real archive verifier;
        # no Docker, service, credentials or provider client is used.
        from test_platform_release import COMMIT, ReleaseTests, release as actual

        fixture = ReleaseTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        archive, identities = fixture.oci_archive(containerd=True)
        self.directory = fixture.root / 'releases' / COMMIT
        self.directory.mkdir(parents=True)
        archive.rename(self.directory / 'image.tar.gz')
        with tarfile.open(self.directory / 'image.tar.gz') as source:
            config = json.load(source.extractfile('manifest.json'))[0]['Config']
        self.config_id = 'sha256:' + config.rsplit('/', 1)[-1]
        self.index_id = fixture.value['image_id']
        self.identities = sorted(identities)
        self.assertNotEqual(self.config_id, self.index_id)
        self.actual, self.commit = actual, COMMIT
        self.root = fixture.root / 'market'
        self.root.mkdir()
        self.environment = {'SIXNINE_IMAGE': fixture.value['image'], 'PATH': '/usr/bin'}
        self.records = {
            fixture.root / 'release-state.json': {
                'current': COMMIT,
                'pending': None,
                'status': 'app_ready',
            }
        }
        self.live = {
            'Image': self.config_id,
            'State': {'Running': True, 'Health': {'Status': 'healthy'}},
        }
        self.inspected = {
            'Id': self.index_id,
            'Config': {'Labels': {'org.opencontainers.image.revision': COMMIT}},
        }
        stack = ExitStack()
        self.addCleanup(stack.close)
        for owner, name, value in (
            (market, 'release', actual),
            (market, 'ROOT', self.root),
            (actual, 'ROOT', fixture.root),
        ):
            stack.enter_context(patch.object(owner, name, value))
        self.archive_check = stack.enter_context(
            patch.object(
                actual, 'validate_image_archive', wraps=actual.validate_image_archive
            )
        )
        stack.enter_context(
            patch.object(actual, 'manifest', return_value=fixture.value)
        )
        stack.enter_context(
            patch.object(
                actual,
                'current_application',
                return_value=(COMMIT, self.directory, self.environment),
            )
        )
        stack.enter_context(
            patch.object(
                actual, 'deployment_environment', return_value=self.environment
            )
        )
        stack.enter_context(patch.object(actual, 'approved_manifest'))
        stack.enter_context(patch.object(actual, 'regular'))
        stack.enter_context(patch.object(actual, 'checksum', return_value='a' * 64))
        stack.enter_context(
            patch.object(actual, 'inspect_service', side_effect=lambda *args: self.live)
        )
        stack.enter_context(
            patch.object(market, 'active', return_value={'active': False})
        )
        stack.enter_context(patch.object(market, 'existing', return_value=''))
        self.write = stack.enter_context(
            patch.object(
                market,
                'atomic',
                side_effect=lambda path, value: self.records.__setitem__(path, value),
            )
        )
        stack.enter_context(
            patch.object(market, 'read', side_effect=self.records.__getitem__)
        )

        def command(arguments, **kwargs):
            if arguments[:2] == ['image', 'inspect']:
                return json.dumps([self.inspected]).encode()
            if arguments[0] == 'compose' and arguments[-3:] == [
                'config',
                '--format',
                'json',
            ]:
                return json.dumps(
                    {'services': {market.SERVICE: market.service(self.index_id)}}
                ).encode()
            raise AssertionError('unapproved_docker_operation')

        self.command = stack.enter_context(
            patch.object(actual, 'command', side_effect=command)
        )

    def test_prepare_and_pinned_use_real_archive_bound_index_and_config_without_rehash_per_sample(
        self,
    ):
        result = market.prepare()
        self.assertEqual(result['state'], 'prepared_not_started')
        record = self.records[self.root / 'prepared.json']
        self.assertEqual(record['image_id'], self.index_id)
        self.assertEqual(record['archive_image_ids'], self.identities)
        self.assertEqual(market.pinned()[0], record)
        self.archive_check.assert_called_once()
        self.live['Image'] = 'sha256:' + 'f' * 64
        with self.assertRaisesRegex(
            self.actual.ReleaseError, 'running_app_does_not_match_release'
        ):
            market.pinned()
        self.assertEqual(self.archive_check.call_count, 1)

    def test_unapproved_inspected_image_fails_before_any_prepared_write(self):
        self.inspected['Id'] = 'sha256:' + 'f' * 64
        with self.assertRaisesRegex(
            self.actual.ReleaseError, 'market_image_revision_mismatch'
        ):
            market.prepare()
        self.write.assert_not_called()

    def test_existing_prepare_is_not_replaced_on_helper_upgrade(self):
        (self.root / 'prepared.json').write_text('{}')
        with self.assertRaisesRegex(
            self.actual.ReleaseError, 'market_existing_prepare_requires_review'
        ):
            market.prepare()
        self.command.assert_not_called()
        self.write.assert_not_called()

    def test_changed_pinned_aliases_are_rejected_before_docker(self):
        market.prepare()
        self.records[self.root / 'prepared.json']['archive_image_ids'] = [
            'sha256:' + 'f' * 64
        ]
        self.command.reset_mock()
        with self.assertRaisesRegex(self.actual.ReleaseError, 'market_pin_invalid'):
            market.pinned()
        self.command.assert_not_called()

    def test_cleanup_accepts_only_archive_bound_config_and_exact_sampler_labels(self):
        market.prepare()
        record = self.records[self.root / 'prepared.json']
        info = {
            'Name': '/' + market.CONTAINER,
            'Image': self.config_id,
            'Config': {
                'Labels': {
                    market.LABEL: record['pin'],
                    'com.docker.compose.service': market.SERVICE,
                }
            },
        }
        with (
            patch.object(market, 'existing', return_value='owned-container'),
            patch.object(
                self.actual, 'command', side_effect=[json.dumps([info]).encode(), b'']
            ) as command,
        ):
            self.assertEqual(market.cleanup()['state'], 'market_container_removed')
            self.assertEqual(
                command.call_args.args[0], ['rm', '--force', 'owned-container']
            )
        info['Image'] = 'sha256:' + 'f' * 64
        with (
            patch.object(market, 'existing', return_value='owned-container'),
            patch.object(
                self.actual, 'command', return_value=json.dumps([info]).encode()
            ) as command,
        ):
            with self.assertRaisesRegex(
                self.actual.ReleaseError, 'market_cleanup_identity_mismatch'
            ):
                market.cleanup()
            self.assertEqual(command.call_count, 1)


class CacheIntegrationTests(unittest.TestCase):
    def test_real_scanner_publishes_only_normalized_targon_cache_without_schema_init(
        self,
    ):
        from studio_platform.repository import Repository
        from studio_platform.capacity_market import market_inventory

        with tempfile.TemporaryDirectory() as temporary:
            database = 'sqlite:///' + (Path(temporary) / 'cache.sqlite3').as_posix()
            repo = Repository(database)
            market_inventory.create(repo.engine)
            repo.close()

            def observation(**kwargs):
                return {
                    'provider': 'targon',
                    'status': 'ok',
                    'observed_at': kwargs['clock'](),
                    'offers': [],
                }

            output = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {'SIXNINE_DATABASE_URL': database, 'SIXNINE_DATABASE_URL_FILE': ''},
                ),
                patch(
                    'studio_platform.capacity_inventory.scan_targon',
                    side_effect=observation,
                ) as scan,
                patch(
                    'studio_platform.capacity_inventory.scan_lium',
                    side_effect=AssertionError('lium_forbidden'),
                ),
                patch.object(
                    Repository,
                    'create_schema',
                    side_effect=AssertionError('schema_forbidden'),
                ),
                redirect_stdout(output),
            ):
                exec(compile(market.ONCE, '<approved-market-once>', 'exec'), {})
            result = json.loads(output.getvalue())
            self.assertTrue(result['fresh_success'])
            self.assertEqual(result['provider'], 'targon')
            self.assertEqual(result['offer_count'], 0)
            scan.assert_called_once()


if __name__ == '__main__':
    unittest.main()
