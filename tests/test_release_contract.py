import tempfile
import unittest
from pathlib import Path

from tools.release_contract import build_contracts, FIXED, AGENT_FILES
from tools.worker_dependencies import (
    WORKER_ROOTS,
    WORKER_POLICY_FILES,
    NATIVE_MANIFESTS,
)


class CompatibilityFingerprints(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in (
            *FIXED,
            *AGENT_FILES,
            *WORKER_POLICY_FILES,
            *NATIVE_MANIFESTS,
            *(name.replace('.', '/') + '.py' for name in WORKER_ROOTS),
            'studio_platform/__init__.py',
            'studio_platform/runtime_hosts/__init__.py',
            'studio_platform/migrations/__init__.py',
            'studio_platform/migrations/env.py',
            'studio_platform/migrations/versions/__init__.py',
            'studio_platform/api.py',
            'studio_platform/frontend.py',
            'studio_platform/agent_discovery.py',
        ):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'VALUE = 1\n')
        (self.root / 'studio_platform/worker.py').write_text('from .api import VALUE\n')
        for name, variable in NATIVE_MANIFESTS.items():
            (self.root / name).write_text(
                f'{variable} = ("studio_platform/worker.py",)\n'
            )

    def test_ui_source_edits_do_not_require_backend_or_worker_restart(self):
        before = build_contracts(self.root)
        (self.root / 'inkseq-web').mkdir()
        (self.root / 'inkseq-web/index.html').write_text('new page')
        self.assertEqual(before, build_contracts(self.root))

    def test_discovery_changes_api_only_but_job_code_changes_both(self):
        before = build_contracts(self.root)
        (self.root / 'studio_platform/agent_discovery.py').write_text('VALUE = 2\n')
        middle = build_contracts(self.root)
        self.assertNotEqual(before['api_compatibility'], middle['api_compatibility'])
        self.assertEqual(before['worker_compatibility'], middle['worker_compatibility'])
        (self.root / 'studio_platform/api.py').write_text('VALUE = 2\n')
        after = build_contracts(self.root)
        self.assertNotEqual(middle['api_compatibility'], after['api_compatibility'])
        self.assertNotEqual(
            middle['worker_compatibility'], after['worker_compatibility']
        )

    def test_reference_changes_are_in_api_source_identity(self):
        before = build_contracts(self.root)
        reference = self.root / 'skills/inkseq/reference/api.md'
        reference.write_text('changed supported task contract')
        after = build_contracts(self.root)
        self.assertNotEqual(before['api_compatibility'], after['api_compatibility'])
        self.assertEqual(before['worker_compatibility'], after['worker_compatibility'])
        reference.unlink()
        with self.assertRaises(FileNotFoundError):
            build_contracts(self.root)

    def test_new_modules_join_worker_identity_when_referenced(self):
        before = build_contracts(self.root)
        (self.root / 'studio_platform/new_worker.py').write_text('VALUE = 2\n')
        after = build_contracts(self.root)
        self.assertEqual(before['worker_compatibility'], after['worker_compatibility'])
        self.assertNotEqual(before['api_compatibility'], after['api_compatibility'])
        (self.root / 'studio_platform/worker.py').write_text(
            'from . import new_worker\n'
        )
        referenced = build_contracts(self.root)
        self.assertNotEqual(
            after['worker_compatibility'], referenced['worker_compatibility']
        )
        (self.root / 'tools/release_contract.py').write_text(
            '# changed dependency policy\n'
        )
        self.assertNotEqual(
            referenced['worker_compatibility'],
            build_contracts(self.root)['worker_compatibility'],
        )

    def test_private_manifest_and_policy_bytes_always_affect_worker(self):
        for name in (*NATIVE_MANIFESTS, *WORKER_POLICY_FILES):
            before = build_contracts(self.root)
            with (self.root / name).open('a') as stream:
                stream.write('\n# changed source contract\n')
            self.assertNotEqual(
                before['worker_compatibility'],
                build_contracts(self.root)['worker_compatibility'],
            )

    def test_normalized_line_endings_are_cross_platform(self):
        before = build_contracts(self.root)
        (self.root / 'studio_platform/api.py').write_bytes(b'VALUE = 1\r\n')
        self.assertEqual(before, build_contracts(self.root))

    def test_runtime_profile_data_changes_both_compatibility_fingerprints(self):
        profiles = [name for name in FIXED if name.startswith('deploy/wangp/profiles/')]
        self.assertEqual(len(profiles), 20)
        for name in profiles:
            before = build_contracts(self.root)
            (self.root / name).write_text('changed model/component/runtime binding')
            after = build_contracts(self.root)
            self.assertNotEqual(before['api_compatibility'], after['api_compatibility'])
            self.assertNotEqual(
                before['worker_compatibility'], after['worker_compatibility']
            )

    def test_missing_required_input_fails_closed(self):
        (self.root / 'requirements.lock.txt').unlink()
        with self.assertRaises(FileNotFoundError):
            build_contracts(self.root)

    def test_path_discovered_migration_changes_and_additions_affect_worker(self):
        before = build_contracts(self.root)
        (self.root / 'studio_platform/migrations/env.py').write_text('VALUE = 2\n')
        changed = build_contracts(self.root)
        self.assertNotEqual(
            before['worker_compatibility'], changed['worker_compatibility']
        )
        (self.root / 'studio_platform/migrations/versions/new_revision.py').write_text(
            "raise RuntimeError('never execute migration')\n"
        )
        added = build_contracts(self.root)
        self.assertNotEqual(
            changed['worker_compatibility'], added['worker_compatibility']
        )


if __name__ == '__main__':
    unittest.main()
