"""Exact selected-binary guards; no native/GPU/network or media generation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from studio_platform.runtime_hosts import wangp_media_tools as tools


class MediaToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.cwd = self.root / "child"
        self.cwd.mkdir()
        self.calls = []
        self.versions = {}
        self.help = "  -fps_mode           set framerate mode for matching video streams\n"

    def binary(self, directory, name):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (name + (".exe" if os.name == "nt" else ""))
        path.write_bytes((str(path) + " fixture executable").encode())
        path.chmod(0o755)
        return path

    def fake_run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        name = Path(command[0]).stem
        text = (name + " version " + self.versions.get(command[0], "6.1.1") + " fixture\n"
                if command[1:] == ["-version"] else self.help)
        kwargs["stdout"].write(text.encode())
        return subprocess.CompletedProcess(command, 0)

    def verify(self, environment):
        with patch.object(tools.subprocess, "run", side_effect=self.fake_run):
            return tools.verify_media_tools(self.runtime, environment, cwd=self.cwd)

    def test_existing_environment_override_is_checked_without_newer_path_fallback(self):
        old = self.binary(self.cwd / "old", "ffmpeg")
        probe = self.binary(self.root / "path", "ffprobe")
        self.binary(probe.parent, "ffmpeg")
        self.versions[str(old)] = "4.4.2"
        environment = {"PATH": str(probe.parent), "FFMPEG_BINARY": os.path.relpath(old, self.cwd)}
        with self.assertRaisesRegex(ValueError, "^wangp_media_tools_version_unsupported$"):
            self.verify(environment)
        self.assertEqual([call[0][0] for call in self.calls], [str(old)])
        self.assertEqual(environment["FFMPEG_BINARY"], os.path.relpath(old, self.cwd))

    def test_runtime_bins_precede_path_and_evidence_hashes_actual_command_files(self):
        selected = {name: self.binary(self.runtime / "ffmpeg_bins", name) for name in ("ffmpeg", "ffprobe")}
        for name in selected:
            self.binary(self.root / "path", name)
        environment = {"PATH": str(self.root / "path"), "HOME": "unchanged"}
        result = self.verify(environment)
        self.assertEqual([call[0][0] for call in self.calls],
                         [str(selected["ffmpeg"]), str(selected["ffmpeg"]), str(selected["ffprobe"])])
        for name, path in selected.items():
            self.assertEqual(result[name]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(result[name]["version"], "6.1.1")
        for command, kwargs in self.calls:
            self.assertEqual(kwargs["env"], environment)
            self.assertEqual(kwargs["cwd"], str(self.cwd))
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertFalse(kwargs["shell"])
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(environment, {"PATH": str(self.root / "path"), "HOME": "unchanged"})

    def test_relative_path_uses_child_cwd_and_missing_override_matches_upstream_fallback(self):
        for name in ("ffmpeg", "ffprobe"):
            self.binary(self.cwd / "tools", name)
        result = self.verify({"PATH": "tools", "FFMPEG_BINARY": "missing"})
        self.assertEqual(self.calls[0][0][0], str(self.cwd / "tools" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")))
        self.assertEqual(result["ffmpeg"]["fps_mode"], "supported")

    def test_both_versions_and_actual_fps_mode_option_are_required(self):
        for name in ("ffmpeg", "ffprobe"):
            self.binary(self.runtime / "ffmpeg_bins", name)
        probe = self.runtime / "ffmpeg_bins" / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
        self.versions[str(probe)] = "5.0.9"
        with self.assertRaisesRegex(ValueError, "version_unsupported"):
            self.verify({"PATH": ""})
        self.versions[str(probe)] = "n5.1.6-0+deb12u1"
        self.help = "Option description mentions fps_mode, but there is no such option.\n"
        with self.assertRaisesRegex(ValueError, "fps_mode_unsupported"):
            self.verify({"PATH": ""})
        self.help = "-fps_mode mode     set framerate mode\n"
        self.assertEqual(self.verify({"PATH": ""})["ffprobe"]["version"], "5.1.6")

    def test_missing_empty_path_unknown_version_and_probe_errors_are_safe(self):
        with patch.dict(os.environ, {"PATH": "PRIVATE-parent-must-not-be-used"}):
            with self.assertRaisesRegex(ValueError, "^wangp_media_tools_missing$"):
                self.verify({"PATH": ""})
        ffmpeg = self.binary(self.runtime / "ffmpeg_bins", "ffmpeg")
        self.versions[str(ffmpeg)] = "N-PRIVATE"
        with self.assertRaisesRegex(ValueError, "^wangp_media_tools_version_unconfirmed$"):
            self.verify({"PATH": ""})
        for error, code in ((subprocess.TimeoutExpired("PRIVATE", 5, output=b"PRIVATE"), "probe_timeout"),
                            (OSError("PRIVATE"), "probe_failed")):
            with patch.object(tools.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(ValueError, "^wangp_media_tools_" + code + "$") as caught:
                    tools.verify_media_tools(self.runtime, {"PATH": ""}, cwd=self.cwd)
                self.assertNotIn("PRIVATE", str(caught.exception))

    def test_changed_binary_and_oversized_probe_output_refuse_ready_evidence(self):
        ffmpeg = self.binary(self.runtime / "ffmpeg_bins", "ffmpeg")
        self.binary(self.runtime / "ffmpeg_bins", "ffprobe")
        original_run = self.fake_run
        def changing(command, **kwargs):
            result = original_run(command, **kwargs)
            if command[1:] == ["-hide_banner", "-h", "full"]:
                ffmpeg.write_bytes(b"changed binary")
            return result
        with patch.object(tools.subprocess, "run", side_effect=changing):
            with self.assertRaisesRegex(ValueError, "binary_changed"):
                tools.verify_media_tools(self.runtime, {"PATH": ""}, cwd=self.cwd)
        self.help = "X" * (tools.MAX_OUTPUT_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "output_invalid"):
            self.verify({"PATH": ""})


if __name__ == "__main__":
    unittest.main()
