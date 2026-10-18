"""Regression for provider SFTP umask 0002; no network or cloud operations."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from studio_platform.wangp_bootstrap import BootError, SOURCE_NAMES, WanGPSSHHost


class Remote:
    def __init__(self):
        self.files = {}
        self.writes = 0

    def __enter__(self): return self
    def __exit__(self, *args): pass

    def open(self, path, mode):
        if mode == 'rb' and path not in self.files:
            raise FileNotFoundError(path)
        if mode == 'wx':
            if path in self.files: raise FileExistsError(path)
            self.files[path] = {'content': b'', 'mode': 0o664}
        entry, remote = self.files[path], self

        class Handle:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return entry['content'][:size]
            def chmod(self, permissions): entry['mode'] = permissions
            def write(self, data):
                # Enforce the downstream private-document boundary, including
                # the interval before source bytes become visible remotely.
                if entry['mode'] & 0o077:
                    raise PermissionError('source_not_private_before_write')
                entry['content'] += data
                remote.writes += 1
        return Handle()


class SourcePermissionTests(unittest.TestCase):
    def setUp(self):
        self.remote = Remote()
        self.host = object.__new__(WanGPSSHHost)
        self.host.remote_root = '/workspace/h3-studio/profile-slot-0'
        self.host.config = SimpleNamespace()
        self.host.client = SimpleNamespace(open_sftp=lambda: self.remote)
        self.host._capture_system_observation = lambda: None
        self.host.run = lambda *args, **kwargs: {'ok': True}
        self.files = {name: b'{}' if name == 'wangp-runtime.json' else name.encode()
                      for name in SOURCE_NAMES}

    def upload(self):
        with patch('studio_platform.wangp_bootstrap.dependency_source', return_value=None):
            self.host.upload(self.files)

    def test_new_source_is_private_despite_provider_umask(self):
        self.upload()
        self.assertEqual(self.remote.writes, len(SOURCE_NAMES))
        for name, expected in self.files.items():
            entry = self.remote.files[self.host.remote_root + '/' + name]
            self.assertEqual(entry, {'mode': 0o600, 'content': expected})

    def test_exact_existing_source_is_tightened_without_rewrite(self):
        for name, value in self.files.items():
            self.remote.files[self.host.remote_root + '/' + name] = {'mode': 0o664, 'content': value}
        self.upload()
        self.assertEqual(self.remote.writes, 0)
        self.assertTrue(all(value['mode'] == 0o600 for value in self.remote.files.values()))

    def test_existing_mismatch_is_never_overwritten_or_repermissioned(self):
        name = next(iter(self.files))
        entry = {'mode': 0o664, 'content': b'other-source'}
        self.remote.files[self.host.remote_root + '/' + name] = entry
        with self.assertRaisesRegex(BootError, 'existing_source_mismatch'):
            self.upload()
        self.assertEqual(entry, {'mode': 0o664, 'content': b'other-source'})
        self.assertEqual(self.remote.writes, 0)


if __name__ == '__main__':
    unittest.main()
