"""Pinned GPU execution versus app releases. All containers/providers are fake."""
from contextlib import ExitStack
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_platform_release import release

OLD, NEW = 'a'*40, 'b'*40
IMAGE = 'sha256:'+'c'*64
CONTAINER = 'd'*64
CONTRACTS = {'version': 1, 'api_compatibility': '1'*64,
    'worker_compatibility': '2'*64, 'frontend_contract': 'sixnine-web-v1'}
FILES = {name: '4'*64 for name in ('compose.yaml', 'Caddyfile', 'init_database.py', 'check_config.py')}


class PinnedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root/'gpu-scaler'
        for name in ('operator', 'public-source'):
            (self.folder/name).mkdir(parents=True)
        self.policy = {'revision': 'synthetic', 'enabled': True}
        self.config = {'cycle_id': 'same-budget-and-window',
            'execution_policy_sha256': release.canonical_hash(self.policy), 'source_sha256': {}}
        for name in ('bootstrap_cloud.py', 'model_manifest.json'):
            (self.folder/'public-source'/name).write_bytes(b'inert-fixture')
            self.config['source_sha256'][name] = hashlib.sha256(b'inert-fixture').hexdigest()
        self.pin = {'version': 2, 'active': True, 'commit': OLD, 'image_id': IMAGE,
            'contracts': CONTRACTS, 'cycle_id': self.config['cycle_id'], 'config_hash': release.canonical_hash(self.config),
            'container_name': 'sixnine-finite-'+hashlib.sha256(self.config['cycle_id'].encode()).hexdigest()[:20],
            'admission': 'open', 'updated_at': 123.0}
        self.put('active.json', self.pin)
        self.put('operator/scaler.json', self.config)
        self.put('operator/execution-policy.json', self.policy)
        self.actual = {'Name': '/'+self.pin['container_name'], 'Image': IMAGE,
            'Config': {'Labels': {'com.docker.compose.project': 'sixnine-platform',
                'com.docker.compose.service': 'gpu-controller', 'com.sixnine.finite.config-hash': self.pin['config_hash']}},
            'State': {'Running': True, 'Restarting': False, 'OOMKilled': False}}
        self.commands = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(release, 'regular'))
        self.stack.enter_context(patch.object(release, 'protected_directory'))
        # Temporary CI files belong to the runner, not root. Install the
        # existing filesystem boundary fakes before reading operator input.
        self.put('app-admission.json', release.app_admission_overlay(self.root))
        self.stack.enter_context(patch.object(release, 'approved_manifest'))
        self.stack.enter_context(patch.object(release, 'manifest', return_value={'contracts': CONTRACTS, 'files': FILES}))
        self.stack.enter_context(patch.object(release, 'validate_image_archive', return_value={IMAGE}))
        self.stack.enter_context(patch.object(release, 'command', side_effect=self.command))

    def put(self, name, value):
        (self.folder/name).write_text(json.dumps(value), encoding='utf-8')

    def command(self, arguments, **kwargs):
        self.commands.append(arguments)
        if arguments[0] == 'ps':
            return CONTAINER.encode() if arguments[-1].endswith('gpu-controller') else b''
        if arguments == ['inspect', CONTAINER]:
            return json.dumps([self.actual]).encode()
        raise AssertionError('Unexpected operation; no service mutations are allowed here')

    def target(self, **values):
        return {'contracts': {**CONTRACTS, 'api_compatibility': '3'*64, **values}, 'files': FILES}

    def test_compatible_app_keeps_execution_pin_and_permits_different_api_digest(self):
        context = release.gpu_deployment_context(self.root, self.target())
        self.assertEqual(context['commit'], OLD)
        self.assertEqual(context['image_id'], IMAGE)
        self.assertEqual(context['admission'], 'open')
        self.assertEqual(json.loads((self.folder/'active.json').read_text()), self.pin)
        self.assertTrue(all(c[0] in ('ps', 'inspect') for c in self.commands))

    def test_legacy_unknown_or_incompatible_contract_never_opens_fast_path(self):
        for target in ({}, self.target(worker_compatibility='f'*64)):
            with self.subTest(target=target), self.assertRaises(release.ReleaseError):
                release.gpu_deployment_context(self.root, target)
        for pin in ({**self.pin, 'version': 1}, {**self.pin, 'version': True}, {**self.pin, 'extra': 'unreviewed'}):
            self.put('active.json', pin)
            with self.assertRaises(release.ReleaseError):
                release.gpu_deployment_context(self.root, self.target())

    def test_policy_or_source_or_configuration_change_requires_reconciliation(self):
        for name, bad in [('operator/scaler.json', {**self.config, 'hard_deadline': 999}),
                          ('operator/execution-policy.json', {**self.policy, 'enabled': False})]:
            original = (self.folder/name).read_text()
            self.put(name, bad)
            with self.assertRaisesRegex(release.ReleaseError, 'config_changed'):
                release.gpu_deployment_context(self.root, self.target())
            (self.folder/name).write_text(original)
        (self.folder/'public-source'/'bootstrap_cloud.py').write_bytes(b'changed')
        with self.assertRaisesRegex(release.ReleaseError, 'sources_changed'):
            release.gpu_deployment_context(self.root, self.target())

    def test_host_configuration_change_requires_drain_even_if_contract_is_mislabeled(self):
        target = {**self.target(), 'files': {**FILES, 'compose.yaml': 'e'*64}}
        with self.assertRaisesRegex(release.ReleaseError, 'host_configuration_change'):
            release.gpu_deployment_context(self.root, target)

    def test_unknown_image_container_and_multiple_controllers_fail_closed(self):
        for key, bad in [('Image', 'sha256:'+'e'*64), ('Name', '/other-controller')]:
            old = self.actual[key]
            self.actual[key] = bad
            with self.assertRaisesRegex(release.ReleaseError, 'identity_mismatch'):
                release.gpu_deployment_context(self.root, self.target())
            self.actual[key] = old
        self.actual['State']['Running'] = False
        with self.assertRaises(release.ReleaseError):
            release.gpu_deployment_context(self.root, self.target())
        with patch.object(release, 'command', return_value=(CONTAINER+'\n'+'e'*64).encode()):
            with self.assertRaises(release.ReleaseError):
                release.gpu_deployment_context(self.root, self.target())

    def test_closed_admission_remains_closed_during_compatible_app_release(self):
        self.put('active.json', {**self.pin, 'admission': 'closed'})
        context = release.gpu_deployment_context(self.root, self.target())
        with patch.object(release, 'compose', return_value=b'ok') as compose:
            release.application_compose(Path('/new-release'), {}, 'up', '-d', '--no-deps', 'app', gpu_context=context)
        compose.assert_called_once()
        self.assertNotIn('gpu-controller', compose.call_args.args)

    def test_app_overlay_must_contain_only_exact_policy_delta(self):
        value = release.app_admission_overlay(self.root)
        value['services']['gpu-controller'] = {'image': 'unapproved'}
        self.put('app-admission.json', value)
        with self.assertRaisesRegex(release.ReleaseError, 'overlay_changed'):
            release.gpu_deployment_context(self.root, self.target())

    def test_legacy_acceptance_still_blocks_even_with_valid_v2_scaler(self):
        folder = self.root/'gpu-acceptance'
        folder.mkdir()
        (folder/'active.json').write_text(json.dumps({'version': 1, 'active': True}))
        with self.assertRaisesRegex(release.ReleaseError, 'explicit_safe_restore'):
            release.gpu_deployment_context(self.root, self.target())

    def test_inactive_marker_does_not_hide_orphan_controller(self):
        self.put('active.json', {**self.pin, 'active': False})
        with self.assertRaisesRegex(release.ReleaseError, 'worker_still_running'):
            release.gpu_deployment_context(self.root, self.target())


class CurrentApplicationTests(unittest.TestCase):
    def test_restore_resolves_current_app_and_never_replays_execution_image(self):
        current = Path('/srv/sixnine/releases')/NEW
        expected = {'image_id': IMAGE}
        with patch.object(release, 'operator_release_fence'), \
                patch.object(release, 'current_application', return_value=(NEW, current, {'SIXNINE_IMAGE': NEW})), \
                patch.object(release, 'application_compose') as compose, patch.object(release, 'wait_ready'), \
                patch.object(release, 'manifest', return_value=expected), \
                patch.object(release, 'validate_image_archive', return_value={IMAGE}), \
                patch.object(release, 'verify_running_app'):
            self.assertEqual(release.restore_current_cpu_locked(), NEW)
        self.assertEqual(compose.call_args.args[0], current)
        self.assertEqual(compose.call_args.args[2:], ('up', '-d', '--no-deps', 'app'))
        self.assertNotIn(OLD, str(compose.call_args))

    def test_pending_or_unconfirmed_release_cannot_choose_an_old_ready_image(self):
        for state in ({'current': NEW, 'pending': OLD, 'status': 'deploying'},
                      {'current': NEW, 'status': 'rollback_failed_needs_reconciliation'}):
            with patch.object(release, '_protected_json', return_value=state), \
                    patch.object(release, 'command') as command, self.assertRaises(release.ReleaseError):
                release.current_application()
            command.assert_not_called()

    def test_frontend_pointer_is_archived_without_removing_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frontend = root/'frontend'
            frontend.mkdir()
            (frontend/'current.json').write_text('{"release":"synthetic"}')
            (frontend/'assets').mkdir()
            (frontend/'assets'/'still-used.js').write_text('inert')
            with patch.object(release, 'regular'), patch.object(release.os, 'name', 'nt'):
                release.retire_frontend_pointer(root)
            self.assertFalse((frontend/'current.json').exists())
            self.assertEqual(json.loads((frontend/'previous-platform-pointer.json').read_text()), {'release': 'synthetic'})
            self.assertTrue((frontend/'assets'/'still-used.js').exists())


if __name__ == '__main__':
    unittest.main()
