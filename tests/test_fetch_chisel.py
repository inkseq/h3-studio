"""Pinned chisel fetch: cache first, verified release fallback; no network."""

import gzip
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import fetch_chisel

BINARY = b"\x7fELF fake chisel"
ASSET = gzip.compress(BINARY, mtime=0)


class FetchChiselTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        pin = patch.object(fetch_chisel, "SHA256", hashlib.sha256(ASSET).hexdigest())
        pin.start()
        self.addCleanup(pin.stop)

    def run_main(self, *args, opener=None):
        opener = opener or Mock(side_effect=AssertionError("no network"))
        sleep = Mock()
        code = fetch_chisel.main(
            ["--cache", str(self.root / "cache"), *args], opener=opener, sleep=sleep
        )
        return code, opener, sleep

    def test_valid_cache_installs_without_network(self):
        (self.root / "cache").mkdir()
        (self.root / "cache" / fetch_chisel.NAME).write_bytes(ASSET)
        target = self.root / "chisel"
        code, opener, _ = self.run_main("--install", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(target.read_bytes(), BINARY)
        opener.assert_not_called()

    def test_cache_miss_downloads_with_backoff_and_fills_cache(self):
        replies = [OSError("504"), OSError("504"), io.BytesIO(ASSET)]

        def opener(url, timeout):
            self.assertEqual(url, fetch_chisel.URL)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        code, _, sleep = self.run_main("--cache-only", opener=opener)
        self.assertEqual(code, 0)
        self.assertEqual((self.root / "cache" / fetch_chisel.NAME).read_bytes(), ASSET)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10])

    def test_tampered_cache_is_ignored_and_bad_download_is_refused(self):
        (self.root / "cache").mkdir()
        (self.root / "cache" / fetch_chisel.NAME).write_bytes(b"tampered")
        target = self.root / "chisel"
        with self.assertRaisesRegex(SystemExit, "chisel_checksum_mismatch"):
            self.run_main(
                "--install",
                str(target),
                opener=Mock(return_value=io.BytesIO(b"also tampered")),
            )
        self.assertFalse(target.exists())

    def test_persistent_outage_fails_after_bounded_attempts(self):
        opener = Mock(side_effect=OSError("504"))
        with self.assertRaisesRegex(SystemExit, "chisel_download_failed"):
            self.run_main("--cache-only", opener=opener)
        self.assertEqual(opener.call_count, fetch_chisel.ATTEMPTS)

    def test_ci_cache_key_and_dockerfile_use_the_same_pin(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn(
            "key: chisel-1.12.0-linux-amd64-"
            "f3f180f1d93aa72cce4e6386f98cc06569a0146fbd65eb4423cf83e6434bcfe6",
            workflow,
        )
        self.assertIn("path: .build-cache/" + fetch_chisel.NAME, workflow)
        dockerfile = (root / "Dockerfile.dispatch").read_text(encoding="utf-8")
        self.assertIn("tools/fetch_chisel.py", dockerfile)
        ignore = (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("!.build-cache/" + fetch_chisel.NAME, ignore)


if __name__ == "__main__":
    unittest.main()
