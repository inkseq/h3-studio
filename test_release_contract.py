import tempfile
import unittest
from pathlib import Path

from tools.release_contract import build_contracts, FIXED, AGENT_FILES


class CompatibilityFingerprints(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in (*FIXED, *AGENT_FILES, 'studio_platform/api.py',
                     'studio_platform/frontend.py', 'studio_platform/agent_discovery.py'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'first\nsecond\n')

    def test_ui_source_edits_do_not_require_backend_or_worker_restart(self):
        before = build_contracts(self.root)
        (self.root / 'yingxu').mkdir()
        (self.root / 'yingxu/index.html').write_text('new page')
        self.assertEqual(before, build_contracts(self.root))

    def test_discovery_changes_api_only_but_job_code_changes_both(self):
        before = build_contracts(self.root)
        (self.root / 'studio_platform/agent_discovery.py').write_text('new description')
        middle = build_contracts(self.root)
        self.assertNotEqual(before['api_compatibility'], middle['api_compatibility'])
        self.assertEqual(before['worker_compatibility'], middle['worker_compatibility'])
        (self.root / 'studio_platform/api.py').write_text('new admission contract')
        after = build_contracts(self.root)
        self.assertNotEqual(middle['api_compatibility'], after['api_compatibility'])
        self.assertNotEqual(middle['worker_compatibility'], after['worker_compatibility'])

    def test_unknown_modules_and_policy_are_conservative(self):
        before = build_contracts(self.root)
        (self.root / 'studio_platform/new_worker.py').write_text('new code')
        after = build_contracts(self.root)
        self.assertNotEqual(before['worker_compatibility'], after['worker_compatibility'])
        (self.root / 'tools/release_contract.py').write_text('new exclusion policy')
        self.assertNotEqual(after['worker_compatibility'], build_contracts(self.root)['worker_compatibility'])

    def test_normalized_line_endings_are_cross_platform(self):
        before = build_contracts(self.root)
        (self.root / 'studio_platform/api.py').write_bytes(b'first\r\nsecond\r\n')
        self.assertEqual(before, build_contracts(self.root))

    def test_runtime_profile_data_changes_both_compatibility_fingerprints(self):
        profiles = [name for name in FIXED if name.startswith('deploy/wangp/profiles/')]
        self.assertEqual(len(profiles), 14)
        for name in profiles:
            before = build_contracts(self.root)
            (self.root / name).write_text('changed model/component/runtime binding')
            after = build_contracts(self.root)
            self.assertNotEqual(before['api_compatibility'], after['api_compatibility'])
            self.assertNotEqual(before['worker_compatibility'], after['worker_compatibility'])

    def test_missing_required_input_fails_closed(self):
        (self.root / 'requirements.lock.txt').unlink()
        with self.assertRaises(FileNotFoundError):
            build_contracts(self.root)


if __name__ == '__main__':
    unittest.main()
