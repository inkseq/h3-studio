"""Receipt publication security with no provider, runtime, or database calls."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from studio_platform import production_scaler


class PrivateReceiptTests(unittest.TestCase):
    def test_failed_serialization_keeps_original_receipt_and_removes_only_own_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "service-state.json"
            old = b'{"sequence": 1}\n'
            path.write_bytes(old)
            unrelated = root / "service-state.tmp"
            unrelated.write_bytes(b"other operation")
            with self.assertRaises(TypeError):
                production_scaler.save(path, {"sequence": 2, "invalid": object()})
            self.assertEqual(path.read_bytes(), old)
            self.assertEqual(unrelated.read_bytes(), b"other operation")
            self.assertEqual(set(root.iterdir()), {path, unrelated})

    def test_failed_replace_keeps_original_and_removes_unpublished_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "preparation-hold.json"
            path.write_text('{"reason":"original"}', encoding="utf-8")
            with patch.object(Path, "replace", side_effect=OSError("injected publication failure")):
                with self.assertRaises(OSError):
                    production_scaler.save(path, {"reason": "replacement"})
            self.assertEqual(json.loads(path.read_text()), {"reason": "original"})
            self.assertEqual(list(root.iterdir()), [path])

    @unittest.skipUnless(os.name == "posix", "POSIX file modes required")
    def test_fresh_and_rewritten_receipts_are_private_before_payload_under_open_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_dump = json.dump
            observations = []

            def inspect_then_dump(value, stream, **kwargs):
                info = os.fstat(stream.fileno())
                self.assertTrue(stat.S_ISREG(info.st_mode))
                self.assertEqual(info.st_nlink, 1)
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                self.assertEqual(info.st_uid, os.getuid())
                observations.append(value)
                return real_dump(value, stream, **kwargs)

            previous_umask = os.umask(0)
            try:
                with patch.object(production_scaler.json, "dump", side_effect=inspect_then_dump):
                    for name in ("service-state.json", "preparation-hold.json"):
                        path = root / name
                        production_scaler.save(path, {"revision": 1})
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                        # Reproduce the old deployed writer's public-readable
                        # receipt, then the next save during transfer completion.
                        path.chmod(0o644)
                        production_scaler.save(path, {"revision": 2})
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                        self.assertEqual(json.loads(path.read_text()), {"revision": 2})
                self.assertEqual(len(observations), 4)
                self.assertEqual({p.name for p in root.iterdir()},
                                 {"service-state.json", "preparation-hold.json"})
            finally:
                os.umask(previous_umask)

    @unittest.skipUnless(os.name == "posix", "POSIX symlinks required")
    def test_existing_old_temporary_symlink_is_neither_opened_nor_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "unrelated.txt"
            outside.write_bytes(b"retain this data")
            path = root / "service-state.json"
            stale = path.with_suffix(".tmp")
            stale.symlink_to(outside)
            production_scaler.save(path, {"sequence": 2})
            self.assertEqual(outside.read_bytes(), b"retain this data")
            self.assertTrue(stale.is_symlink())
            self.assertEqual(json.loads(path.read_text()), {"sequence": 2})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
