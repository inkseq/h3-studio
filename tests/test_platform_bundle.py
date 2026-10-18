"""Release artifact validation has no Docker load, credentials or network IO."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools.check_platform_bundle import verify, main


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.commit = "e"*40
        self.image = "sixnine-platform:"+self.commit
        config_value = {"os": "linux", "architecture": "amd64", "config": {"Labels": {
            "org.opencontainers.image.revision": self.commit}}}
        config_digest = hashlib.sha256(json.dumps(config_value).encode()).hexdigest()
        config = config_digest+".json"
        with tarfile.open(self.root/"image.tar.gz", "w:gz") as output:
            for name, value in ((config, config_value), ("manifest.json", [{"Config": config, "RepoTags": [self.image], "Layers": []}])):
                data = json.dumps(value).encode()
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                output.addfile(entry, io.BytesIO(data))
        for name in ("compose.yaml", "Caddyfile", "init_database.py", "check_config.py"):
            (self.root/name).write_text("raise AssertionError('Never execute bundle code')", encoding="utf-8")
        self.manifest = {"commit": self.commit, "image": self.image, "image_id": "sha256:"+config_digest,
            "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()}}
        self.publish()

    def publish(self):
        (self.root/"release-manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_only_trusted_validator_executes_and_no_daemon_command_runs(self):
        with patch("subprocess.run", side_effect=AssertionError("No Docker load")):
            result = verify(self.root, self.commit)
        self.assertEqual(result["state"], "bundle_manifest_and_archive_verified")
        self.assertFalse(result["image_loaded"])
        self.assertFalse(result["provenance_approved"])
        self.assertEqual(result["manifest_sha256"], hashlib.sha256((self.root/"release-manifest.json").read_bytes()).hexdigest())

    def test_actual_archive_structure_is_checked_after_valid_file_hash(self):
        (self.root/"image.tar.gz").write_bytes(b"not a tar archive")
        self.manifest["files"]["image.tar.gz"] = hashlib.sha256(b"not a tar archive").hexdigest()
        self.publish()
        with self.assertRaises(Exception):
            verify(self.root, self.commit)

    def test_unlisted_file_cannot_leak_through_whole_directory_artifact_upload(self):
        (self.root/"unexpected-private.txt").write_text("synthetic-only")
        with self.assertRaisesRegex(ValueError, "unreviewed_files"):
            verify(self.root, self.commit)

    def test_tamper_or_wrong_commit_cannot_pass_and_cli_hides_diagnostics(self):
        with self.assertRaises(Exception):
            verify(self.root, "d"*40)
        (self.root/"Caddyfile").write_text("synthetic-change-not-approved")
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(main([self.commit, str(self.root)]), 1)
        self.assertNotIn(str(self.root), output.getvalue())
        self.assertNotIn("synthetic-change", output.getvalue())


if __name__ == "__main__":
    unittest.main()
