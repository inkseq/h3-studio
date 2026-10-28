"""No network/GPU: original launch binding and finite image context."""

import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from studio_platform import modal_entry
from studio_platform.modal_entry import launch_document, prepare_files
from studio_platform.runtime_hosts.wangp_session import _check_config
from studio_platform.modal_runtime import PROFILE
from studio_platform.runtime_catalog import engine_manifest
from tools.build_modal_sources import build


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.value = {
            'intent_id': '12345678-1234-1234-1234-123456789012',
            'mode': 'fl',
            'manifest_digest': engine_manifest(PROFILE, 'fl').digest,
            'token': 'a' * 64,
        }

    def write(self, value):
        path = self.root / 'launch.json'
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def test_launch_has_exact_profile_mode_and_one_private_token(self):
        value = launch_document(self.write(self.value))
        models = self.root / 'models'
        models.mkdir()
        prepare_files(value, self.root, models)
        manifest = json.loads((self.root / 'manifest.json').read_text())
        self.assertEqual(manifest, engine_manifest(PROFILE, 'fl').document)
        self.assertEqual((self.root / 'token').read_text(), 'a' * 64)
        _check_config(self.root / 'wgp_config.json', models, PROFILE)
        with self.assertRaises(FileExistsError):
            prepare_files(value, self.root, models)

    def test_entry_passes_the_real_native_config_contract(self):
        models = self.root / 'models'
        models.mkdir()
        state = self.root / 'private'
        observed = {}

        def install_original_launch(_seconds):
            modal_entry.install_launch(self.value, state)

        def native_main(argv, **_kwargs):
            config = Path(argv[argv.index('--config') + 1])
            observed.update(_check_config(config, models, PROFILE))
            return 0

        with (
            patch.object(modal_entry, 'STATE', state),
            patch.object(modal_entry, 'MODELS', models),
            patch.object(
                modal_entry.time, 'sleep', side_effect=install_original_launch
            ),
            patch(
                'studio_platform.runtime_hosts.wangp_launcher.main',
                side_effect=native_main,
            ),
        ):
            self.assertEqual(modal_entry.main([]), 0)
        self.assertEqual(observed['checkpoints_paths'], [str(models.resolve())])

    def test_changed_manifest_or_extra_credentials_refuse(self):
        for changed in (
            {'manifest_digest': '0' * 64},
            {'MODAL_TOKEN_SECRET': 'forbidden'},
            {'token': 'short'},
        ):
            with self.subTest(changed=tuple(changed)), self.assertRaises(ValueError):
                launch_document(self.write({**self.value, **changed}))

    def test_image_context_is_finite_no_local_secrets_and_cannot_overwrite(self):
        target = self.root / 'context'
        receipt = build(target)
        self.assertIn('studio_platform/modal_entry.py', receipt['source_sha256'])
        for name, expected in receipt['source_sha256'].items():
            self.assertEqual(
                hashlib.sha256((target / name).read_bytes()).hexdigest(), expected
            )
        self.assertTrue(
            all(not name.startswith('.local') for name in receipt['source_sha256'])
        )
        recipe = (target / 'Dockerfile').read_text()
        self.assertIn('27ef0628d6b4ea24', recipe)
        self.assertIn('studio_platform.modal_entry', recipe)
        self.assertIn('COPY studio_platform /opt/inkseq/studio_platform', recipe)
        with self.assertRaises(ValueError):
            build(target)

    def test_import_and_context_build_do_not_call_model_or_cloud(self):
        with patch('subprocess.run', side_effect=AssertionError('network_or_build')):
            build(self.root / 'offline')
