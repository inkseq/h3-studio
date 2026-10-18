"""POSIX-only host state safety; stdlib only, no AWS or application imports."""

import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from deploy.platform import alert_monitor as monitor


@unittest.skipUnless(os.name == 'posix', 'POSIX file mode/no-follow/flock contract')
class ProtectedStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state.json'
        # Only fixture parent/root identity differs from a deployed root-owned
        # state directory. Keep real modes, no-follow, link counts and flock.
        parents = patch.object(monitor, 'protected_directory')
        parents.start()
        self.addCleanup(parents.stop)
        actual_fstat = os.fstat

        def root_fixture(fd):
            info = actual_fstat(fd)
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_uid=0,
                st_nlink=info.st_nlink,
                st_size=info.st_size,
            )

        owner = patch.object(monitor.os, 'fstat', side_effect=root_fixture)
        owner.start()
        self.addCleanup(owner.stop)

    def test_atomic_mode_and_failed_replace_preserve_previous_state(self):
        monitor.atomic(self.state, {'first': True})
        before = self.state.read_bytes()
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(monitor.read_protected(self.state), {'first': True})
        with (
            patch.object(monitor.os, 'replace', side_effect=OSError('interrupted')),
            self.assertRaises(OSError),
        ):
            monitor.atomic(self.state, {'second': True})
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.state])

    def test_symlink_hardlink_and_public_mode_refused(self):
        monitor.atomic(self.state, {'first': True})
        link = self.root / 'link.json'
        link.symlink_to(self.state)
        with self.assertRaises(OSError):
            monitor.read_protected(link)
        link.unlink()
        os.link(self.state, link)
        with self.assertRaisesRegex(ValueError, 'file_unprotected'):
            monitor.read_protected(self.state)
        link.unlink()
        self.state.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'file_unprotected'):
            monitor.read_protected(self.state)

    def test_second_open_cannot_run_concurrently_and_release_is_reusable(self):
        lock = self.root / 'monitor.lock'
        with monitor.locked(lock):
            with self.assertRaises(BlockingIOError):
                with monitor.locked(lock):
                    self.fail('second monitor entered')
        with monitor.locked(lock):
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)


if __name__ == '__main__':
    unittest.main()
