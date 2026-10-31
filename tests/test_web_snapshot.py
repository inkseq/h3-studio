import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "web_snapshot",
    Path(__file__).resolve().parents[1] / "tools" / "sync_web_source.py",
)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


class WebSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.target = self.root / "source", self.root / "target"
        (self.source / "src").mkdir(parents=True)
        for name in snapshot.ROOT_FILES:
            (self.source / name).parent.mkdir(parents=True, exist_ok=True)
            (self.source / name).write_text(
                "{}" if name.endswith("json") else "source", encoding="utf-8"
            )
        (self.source / "src" / "App.jsx").write_text(
            "export default 1", encoding="utf-8"
        )

    def test_deterministic_source_only_snapshot_and_offline_verification(self):
        (self.source / ".env").write_text("DO_NOT_COPY=test-only", encoding="utf-8")
        (self.source / "src" / "private.mp4").write_bytes(b"test-only")
        first = snapshot.synchronize(self.source, self.target, write=True)
        self.assertEqual(first, snapshot.synchronize(self.source, self.target))
        self.assertEqual(first, snapshot.verify_snapshot(self.target))
        self.assertFalse((self.target / ".env").exists())
        self.assertFalse((self.target / "src" / "private.mp4").exists())
        before = (self.target / snapshot.MANIFEST).read_bytes()
        snapshot.synchronize(self.source, self.target, write=True)
        self.assertEqual(before, (self.target / snapshot.MANIFEST).read_bytes())
        self.assertNotIn(str(self.root), before.decode())

    def test_canonical_and_release_edits_are_detected(self):
        snapshot.synchronize(self.source, self.target, write=True)
        (self.source / "src" / "App.jsx").write_text(
            "export default 2", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "Canonical source differs"):
            snapshot.synchronize(self.source, self.target)
        snapshot.synchronize(self.source, self.target, write=True)
        (self.target / "src" / "App.jsx").write_text(
            "unapproved edit", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "modified"):
            snapshot.verify_snapshot(self.target)

    def test_only_exact_dev_tooling_files_are_included(self):
        (self.source / "tools" / "private.mjs").write_text("exclude", encoding="utf-8")
        result = snapshot.synchronize(self.source, self.target, write=True)
        for name in (
            ".prettierignore",
            ".prettierrc.json",
            "eslint.config.js",
            "tools/check-changed.mjs",
        ):
            self.assertIn(name, result["files"])
        self.assertNotIn("tools/private.mjs", result["files"])
        for name in ("tools/private.mjs", ".env", ".github/workflows/source.yml"):
            with self.assertRaisesRegex(ValueError, "outside the source allowlist"):
                snapshot._safe_relative(name)

    def test_dev_tooling_parent_links_are_not_followed(self):
        tools = self.source / "tools"
        outside = self.root / "actual-tools"
        tools.rename(outside)
        try:
            tools.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("This Windows process cannot create symlinks")
        with self.assertRaisesRegex(ValueError, "link/junction"):
            snapshot.synchronize(self.source, self.target, write=True)
        self.assertFalse(self.target.exists())

    def test_only_explicit_acceptance_document_is_snapshotted(self):
        (self.source / "src" / "NOVICE-STORIES-AC.md").write_bytes(
            "# 验收\r\n无用户素材\r\n".encode("utf-8")
        )
        (self.source / "src" / "private-notes.md").write_text(
            "exclude this document", encoding="utf-8"
        )
        result = snapshot.synchronize(self.source, self.target, write=True)
        self.assertIn("src/NOVICE-STORIES-AC.md", result["files"])
        self.assertNotIn("src/private-notes.md", result["files"])
        self.assertNotIn(
            b"\r", (self.target / "src" / "NOVICE-STORIES-AC.md").read_bytes()
        )
        self.assertEqual(result, snapshot.verify_snapshot(self.target))

    def test_crlf_canonical_is_normalized_for_git_and_linux_roundtrip(self):
        canonical = self.source / "src" / "App.jsx"
        canonical.write_bytes("// 中文\r\nexport default 1\r\n".encode("utf-8"))
        first = snapshot.synchronize(self.source, self.target, write=True)
        self.assertEqual(
            (self.target / "src" / "App.jsx").read_bytes(),
            "// 中文\nexport default 1\n".encode("utf-8"),
        )
        self.assertNotIn(b"\r", (self.target / snapshot.MANIFEST).read_bytes())
        self.assertEqual(first, snapshot.synchronize(self.source, self.target))
        canonical.write_bytes(canonical.read_bytes().replace(b"\r\n", b"\n"))
        self.assertEqual(first, snapshot.synchronize(self.source, self.target))
        self.assertEqual(first, snapshot.verify_snapshot(self.target))
        release = self.target / "src" / "App.jsx"
        release.write_bytes(release.read_bytes().replace(b"\n", b"\r\n"))
        with self.assertRaisesRegex(ValueError, "modified"):
            snapshot.verify_snapshot(self.target)

    def test_approved_font_preserves_non_utf8_and_crlf_bytes_and_detects_edits(self):
        name = "src/v2/fonts/inter-latin-v20.woff2"
        canonical = self.source / name
        canonical.parent.mkdir(parents=True)
        raw = b"wOF2\x00\xff\x81\r\n\x00\rfont-bytes"
        canonical.write_bytes(raw)
        result = snapshot.synchronize(self.source, self.target, write=True)
        self.assertEqual((self.target / name).read_bytes(), raw)
        self.assertEqual(result["files"][name], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result, snapshot.synchronize(self.source, self.target))
        self.assertEqual(result, snapshot.verify_snapshot(self.target))
        canonical.write_bytes(raw + b"changed")
        with self.assertRaisesRegex(ValueError, "Canonical source differs"):
            snapshot.synchronize(self.source, self.target)
        (self.target / name).write_bytes(raw.replace(b"\r\n", b"\n"))
        with self.assertRaisesRegex(ValueError, "modified"):
            snapshot.verify_snapshot(self.target)

    def test_font_documents_are_explicit_and_arbitrary_binary_paths_are_rejected(self):
        fonts = self.source / "src" / "v2" / "fonts"
        fonts.mkdir(parents=True)
        for name in ("README.md", "OFL-Inter.txt"):
            (fonts / name).write_bytes(b"Font license/source\r\n")
        (fonts / "private-notes.txt").write_text(
            "exclude this document", encoding="utf-8"
        )
        (fonts / "other.woff2").write_bytes(b"exclude this binary")
        result = snapshot.synchronize(self.source, self.target, write=True)
        for name in ("README.md", "OFL-Inter.txt"):
            relative = "src/v2/fonts/" + name
            self.assertIn(relative, result["files"])
            self.assertEqual(
                (self.target / relative).read_bytes(), b"Font license/source\n"
            )
        self.assertFalse((self.target / "src/v2/fonts/private-notes.txt").exists())
        self.assertFalse((self.target / "src/v2/fonts/other.woff2").exists())
        result["files"]["src/v2/fonts/other.woff2"] = "0" * 64
        (self.target / snapshot.MANIFEST).write_text(
            json.dumps(result), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "outside the source allowlist"):
            snapshot.verify_snapshot(self.target)

    def test_stale_managed_file_removed_but_unmanaged_not_overwritten(self):
        snapshot.synchronize(self.source, self.target, write=True)
        (self.source / "src" / "App.jsx").unlink()
        snapshot.synchronize(self.source, self.target, write=True)
        self.assertFalse((self.target / "src" / "App.jsx").exists())
        (self.source / "src" / "new.js").write_text("canonical", encoding="utf-8")
        (self.target / "src" / "new.js").write_text("must preserve", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            snapshot.synchronize(self.source, self.target, write=True)
        self.assertEqual((self.target / "src" / "new.js").read_text(), "must preserve")

    def test_unsafe_manifest_cannot_remove_outside_file(self):
        snapshot.synchronize(self.source, self.target, write=True)
        outside = self.root / "keep.js"
        outside.write_text("keep", encoding="utf-8")
        manifest = snapshot.read_manifest(self.target)
        manifest["files"]["../keep.js"] = "0" * 64
        (self.target / snapshot.MANIFEST).write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "unsafe"):
            snapshot.synchronize(self.source, self.target, write=True)
        self.assertEqual(outside.read_text(), "keep")

    def test_source_links_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.source / "src" / "linked").symlink_to(
                outside, target_is_directory=True
            )
        except OSError:
            self.skipTest("This Windows process cannot create symlinks")
        with self.assertRaisesRegex(ValueError, "not followed"):
            snapshot.synchronize(self.source, self.target, write=True)
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
