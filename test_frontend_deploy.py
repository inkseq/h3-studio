"""Offline transaction tests: static releases must not recreate app/GPU/DB."""
import contextlib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / 'deploy/platform'))
host = importlib.import_module('frontend_release')
from tools import build_frontend_release as builder, deploy_aws_release as runner

COMMIT = 'a' * 40
CONTRACTS = {'version': 1, 'api_compatibility': 'b' * 64,
             'worker_compatibility': 'c' * 64, 'frontend_contract': 'sixnine-web-v1'}


class FrontendTransactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'dist'
        (self.source / 'assets').mkdir(parents=True)
        (self.source / 'index.html').write_text('<script src="/assets/index-abcdefgh.js"></script>')
        (self.source / 'assets/index-abcdefgh.js').write_text('console.log("synthetic")')
        self.bundle = self.root / 'bundle'
        with mock.patch('tools.release_contract.build_contracts', return_value=CONTRACTS):
            builder.build(COMMIT, self.source, self.bundle)
        (self.root / 'approved-frontends').mkdir()
        digest = hashlib.sha256((self.bundle / 'frontend-manifest.json').read_bytes()).hexdigest()
        (self.root / 'approved-frontends' / (COMMIT + '.sha256')).write_text(digest)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        if os.name == 'posix':
            # The installer is root-only on the host. CI deliberately runs as
            # an unprivileged user; emulate ownership, retaining real type,
            # link-count, permission, size and inode checks on temporary files.
            original_lstat = Path.lstat
            def root_owned_stat(path, *args, **kwargs):
                fields = list(original_lstat(path, *args, **kwargs))
                fields[4] = 0
                return os.stat_result(fields)
            self.stack.enter_context(mock.patch.object(Path, 'lstat', root_owned_stat))
        self.manifest = self.stack.enter_context(mock.patch.object(host.release, 'manifest',
            return_value={'contracts': CONTRACTS, 'image_id': 'sha256:' + 'd' * 64}))
        self.stack.enter_context(mock.patch.object(host.release, 'validate_image_archive', return_value={'sha256:' + 'd' * 64}))
        self.stack.enter_context(mock.patch.object(host.release, 'verify_running_app'))
        self.docker = self.stack.enter_context(mock.patch.object(host.release, 'compose',
            side_effect=AssertionError('static install must not run any Docker mutation')))

    def probe(self, *_):
        pointer = self.root / 'frontend/current.json'
        commit = json.loads(pointer.read_text())['commit'] if pointer.exists() else None
        return {'contract': 'sixnine-web-v1', 'html_contract': 'sixnine-web-v1', 'commit': commit}

    def install(self, probe=None):
        return host.install_locked(self.root, self.bundle, COMMIT,
            current_api=lambda _: ('e' * 40, self.root / 'api', {}), probe=probe or self.probe)

    def test_atomic_selection_reuses_services_and_old_assets(self):
        assets = self.root / 'frontend/assets'
        assets.mkdir(parents=True)
        old = assets / 'old-12345678.js'
        old.write_text('old tab')
        self.assertEqual(self.install()['state'], 'approved_frontend_release_healthy')
        self.assertEqual(self.probe()['commit'], COMMIT)
        self.assertEqual(old.read_text(), 'old tab')
        self.install()  # idempotent selection
        self.docker.assert_not_called()

    def test_mismatched_api_and_approval_leave_pointer_untouched(self):
        self.manifest.return_value['contracts'] = {**CONTRACTS, 'api_compatibility': 'f' * 64}
        with self.assertRaisesRegex(host.release.ReleaseError, 'matching_deployed_api'):
            self.install()
        self.assertFalse((self.root / 'frontend/current.json').exists())
        self.manifest.return_value['contracts'] = CONTRACTS
        (self.root / 'approved-frontends' / (COMMIT + '.sha256')).write_text('0' * 64)
        with self.assertRaisesRegex(host.release.ReleaseError, 'independent_approval'):
            self.install()

    def test_failed_probe_restores_previous_selection(self):
        base = self.root / 'frontend'
        base.mkdir()
        prior = json.dumps({'version': 1, 'commit': 'f' * 40, 'api_contract': 'sixnine-web-v1'}).encode()
        (base / 'current.json').write_bytes(prior)
        before = self.probe()
        with self.assertRaisesRegex(host.release.ReleaseError, 'activation_probe_failed'):
            self.install(mock.Mock(side_effect=[before, before]))
        self.assertEqual((base / 'current.json').read_bytes(), prior)

    def test_failed_initial_probe_preserves_bundled_fallback(self):
        before = self.probe()
        with self.assertRaisesRegex(host.release.ReleaseError, 'activation_probe_failed'):
            self.install(mock.Mock(side_effect=[before, before]))
        self.assertFalse((self.root / 'frontend/current.json').exists())

    def test_asset_collision_never_changes_served_version(self):
        assets = self.root / 'frontend/assets'
        assets.mkdir(parents=True)
        (assets / 'index-abcdefgh.js').write_text('conflicting bytes')
        with self.assertRaisesRegex(host.release.ReleaseError, 'collision'):
            self.install()
        self.assertFalse((self.root / 'frontend/current.json').exists())

    @unittest.skipUnless(os.name == 'posix', 'POSIX directory mode contract')
    def test_root_private_umask_does_not_hide_public_static_files(self):
        previous = os.umask(0o077)
        try:
            self.install()
        finally:
            os.umask(previous)
        for path in ('frontend', 'frontend/assets', 'frontend/releases', 'frontend/releases/' + COMMIT):
            self.assertEqual((self.root / path).stat().st_mode & 0o777, 0o755)


class FrontendDeploymentDocument(unittest.TestCase):
    def test_frontend_uses_separate_fixed_document_and_receipt(self):
        api = mock.Mock()
        command = 'a1234567-1234-1234-1234-123456789abc'
        api.send_command.return_value = {'Command': {'CommandId': command}}
        api.get_command_invocation.return_value = {'Status': 'Success',
            'DocumentName': 'Sixnine-DeployApprovedFrontend', 'DocumentVersion': '2',
            'StandardOutputContent': json.dumps({'state': 'approved_frontend_release_healthy', 'commit': COMMIT})}
        with contextlib.redirect_stdout(io.StringIO()):
            runner.deploy(COMMIT, frontend=True, api=api)
        self.assertEqual(api.send_command.call_args.kwargs['DocumentName'], 'Sixnine-DeployApprovedFrontend')
        self.assertEqual(api.send_command.call_args.kwargs['DocumentVersion'], '2')
        self.assertEqual(api.get_command_invocation.call_args.kwargs['PluginName'], 'deployApprovedFrontend')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'document version'):
            runner.deploy(COMMIT, resume_command=command, api=api)


if __name__ == '__main__':
    unittest.main()
