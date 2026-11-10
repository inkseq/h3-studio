"""Secret scan wrapper (#976): pinned binary, commit range, redaction; no network."""

import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools import secret_scan


def archive(content=b"#!/bin/sh\nexit 0\n"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("gitleaks")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class SecretScanTests(unittest.TestCase):
    def test_range_selection(self):
        self.assertEqual(secret_scan.log_range("a" * 40), "a" * 40 + "..HEAD")
        self.assertEqual(secret_scan.log_range("0" * 40), "-1")
        self.assertEqual(secret_scan.log_range(""), "-1")
        with self.assertRaises(SystemExit):
            secret_scan.log_range("main; rm -rf /")

    def test_command_is_redacted_and_uses_repo_config(self):
        command = secret_scan.command(Path("/bin/gitleaks"), "b" * 40)
        self.assertIn("--redact=100", command)
        self.assertIn(str(secret_scan.ROOT / ".gitleaks.toml"), command)
        self.assertIn("--log-opts=" + "b" * 40 + "..HEAD", command)
        self.assertEqual(command[command.index("--exit-code") + 1], "1")

    def test_download_is_checksum_verified(self):
        data = archive()
        with patch.object(secret_scan, "SHA256", hashlib.sha256(data).hexdigest()):
            reply = SimpleNamespace(read=lambda: data)
            self.assertEqual(secret_scan.fetch(opener=lambda *a, **k: reply), data)
        with self.assertRaisesRegex(SystemExit, "checksum_mismatch"):
            secret_scan.fetch(opener=lambda *a, **k: SimpleNamespace(read=lambda: data))

    def test_install_extracts_only_the_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = secret_scan.install(directory, archive(b"binary"))
            self.assertEqual(path.read_bytes(), b"binary")

    def test_findings_fail_the_run(self):
        run = Mock(return_value=SimpleNamespace(returncode=1))
        code = secret_scan.main(
            ["--base", "c" * 40, "--gitleaks", "/bin/gitleaks"], run=run
        )
        self.assertEqual(code, 1)
        self.assertEqual(run.call_args.args[0][3], "--log-opts=" + "c" * 40 + "..HEAD")

    def test_config_extends_defaults_with_narrow_allowlists(self):
        text = (secret_scan.ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
        self.assertIn("useDefault = true", text)
        self.assertNotIn("paths = ['''^tests/", text)


if __name__ == "__main__":
    unittest.main()
