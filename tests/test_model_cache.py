"""Mounted model cache for dial-out workers (#924); local files only."""

import os
from pathlib import Path
import tempfile
import unittest

from studio_platform.runtime_hosts.model_cache import VARIABLE, cached_root


def document(*files):
    return {
        "components": {
            "transformer": {"files": [{"path": p, "size_bytes": n} for p, n in files]}
        }
    }


class ModelCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "models"
        (self.root / "minimax_h3").mkdir(parents=True)
        (self.root / "a.safetensors").write_bytes(b"x" * 10)
        (self.root / "minimax_h3" / "vae.safetensors").write_bytes(b"y" * 4)
        self.doc = document(("a.safetensors", 10), ("minimax_h3/vae.safetensors", 4))
        self.env = {VARIABLE: str(self.root)}

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_cache_is_the_model_root(self):
        self.assertEqual(cached_root(self.doc, self.env), self.root)

    def test_unset_incomplete_or_changed_cache_falls_back_to_download(self):
        self.assertIsNone(cached_root(self.doc, {}))
        self.assertIsNone(cached_root(self.doc, {VARIABLE: "relative/models"}))
        self.assertIsNone(cached_root(self.doc, {VARIABLE: str(self.root / "missing")}))
        for files in (
            (("a.safetensors", 11), ("minimax_h3/vae.safetensors", 4)),  # Size changed.
            (("a.safetensors", 10), ("minimax_h3/other.safetensors", 4)),  # Missing.
            (("../outside.safetensors", 1),),  # Escapes the root.
            (("/etc/passwd", 1),),
        ):
            self.assertIsNone(cached_root(document(*files), self.env), files)
        self.assertIsNone(cached_root({"components": None}, self.env))

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_linked_files_or_root_are_refused(self):
        (self.root / "link.safetensors").symlink_to(self.root / "a.safetensors")
        self.assertIsNone(cached_root(document(("link.safetensors", 10)), self.env))
        linked = Path(self.temp.name) / "linked-models"
        linked.symlink_to(self.root)
        self.assertIsNone(cached_root(self.doc, {VARIABLE: str(linked)}))


if __name__ == "__main__":
    unittest.main()
