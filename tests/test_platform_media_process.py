"""Fresh-process limit tests use tiny synthetic tools, never cloud or media APIs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from studio_platform import media_process as target


class MediaProcessUnitTests(unittest.TestCase):
    def test_only_fixed_tool_names_and_valid_bounded_options(self):
        for argv in (["python", "-c", "pass"], ["/usr/bin/ffmpeg"], [], "ffmpeg", ["ffmpeg", "a\x00b"]):
            with self.subTest(argv=argv), self.assertRaises(target.MediaProcessError):
                target.run_media_process(argv, timeout=1)
        for options in ({"timeout": 0}, {"timeout": float("inf")}, {"timeout": True},
                        {"memory_bytes": 0}, {"memory_bytes": target.DEFAULT_ADDRESS_SPACE_BYTES + 1},
                        {"stderr": subprocess.PIPE}, {"stdout": "private-path"}):
            with self.subTest(options=options), self.assertRaises(target.MediaProcessError):
                target.run_media_process(["ffmpeg"], **({"timeout": 1} | options))

    def test_unavailable_static_error_does_not_include_arguments(self):
        with mock.patch.object(target.shutil, "which", return_value=None):
            with self.assertRaises(target.MediaProcessError) as caught:
                target.run_media_process(["ffmpeg", "private-input"], timeout=1)
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertNotIn("private", str(caught.exception))
        self.assertIsInstance(caught.exception, subprocess.SubprocessError)

    def test_windows_fallback_is_explicitly_not_linux_limit_and_no_shell(self):
        with mock.patch.object(target.sys, "platform", "win32"), \
                mock.patch.object(target.shutil, "which", return_value=sys.executable), \
                mock.patch.object(target.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            result = target.run_media_process(["ffmpeg", "-version"], timeout=1)
            self.assertEqual(result.args, ["ffmpeg"])
            self.assertEqual(Path(run.call_args.args[0][0]), Path(sys.executable).resolve())
            self.assertFalse(run.call_args.kwargs["shell"])
            self.assertNotIn("preexec_fn", run.call_args.kwargs)
            with self.assertRaises(target.MediaProcessError) as caught:
                target.run_media_process(["ffmpeg"], timeout=1, require_linux_limits=True)
            self.assertEqual(caught.exception.code, "policy")
            self.assertEqual(run.call_count, 1)

    def test_linux_policy_failure_never_retries_unlimited(self):
        with mock.patch.object(target.sys, "platform", "linux"), \
                mock.patch.object(target.shutil, "which", return_value=sys.executable), \
                mock.patch.object(target.subprocess, "run", return_value=subprocess.CompletedProcess([], 78)) as run:
            with self.assertRaises(target.MediaProcessError) as caught:
                target.run_media_process(["ffprobe", "private-input"], timeout=1)
            self.assertEqual(caught.exception.code, "policy")
            self.assertEqual(run.call_count, 1)
            self.assertNotIn("preexec_fn", run.call_args.kwargs)
            self.assertIn("--limited-exec", run.call_args.args[0])
            self.assertNotIn("private-input", str(caught.exception))

    def test_policy_validation_and_setrlimit_failure_do_not_exec(self):
        with mock.patch.object(target.sys, "platform", "linux"), mock.patch.object(target.os, "execv") as execute:
            self.assertEqual(target._limited_exec(["--limited-exec", "bad", "ffmpeg", sys.executable]), 78)
            resource = mock.Mock()
            resource.setrlimit.side_effect = OSError("private-policy-details")
            with mock.patch.dict(sys.modules, {"resource": resource}):
                self.assertEqual(target._limited_exec(["--limited-exec", "67108864", "ffmpeg", sys.executable]), 78)
            execute.assert_not_called()


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux OS limits require the actual Linux runtime")
class MediaProcessLinuxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tool = self.root / "ffmpeg"
        self.tool.write_text("#!" + sys.executable + "\n" + '''import json, os, resource, sys, time
if sys.argv[1] == "allocation":
    try:
        block = bytearray(256 * 1024**2)
    except MemoryError:
        print(json.dumps({"denied": True, "limit": resource.getrlimit(resource.RLIMIT_AS), "core": resource.getrlimit(resource.RLIMIT_CORE)}))
    else:
        raise SystemExit(99)
elif sys.argv[1] == "sleep":
    with open(sys.argv[2], "w") as file:
        file.write(str(os.getpid()))
    time.sleep(60)
elif sys.argv[1] == "failure":
    sys.stderr.write(sys.argv[2])
    raise SystemExit(42)
elif sys.argv[1] == "large":
    sys.stdout.write("x" * (1024**2 + 1))
elif sys.argv[1] == "limits":
    print(json.dumps({"limit": resource.getrlimit(resource.RLIMIT_AS), "core": resource.getrlimit(resource.RLIMIT_CORE), "pid": os.getpid()}))
''', encoding="utf-8")
        self.tool.chmod(0o700)
        # The isolated container's tmpfs is intentionally noexec. Execute the already
        # installed absolute Python binary and pass it this readable synthetic
        # script; the limited exec still retains its PID and real OS limits.
        # Do not weaken its /tmp mount policy just to run a shebang fixture.
        # Production upload spooling is a separate host disk bind mount.
        patcher = mock.patch.object(target.shutil, "which", return_value=str(Path(sys.executable).resolve()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_actual_allocation_denied_and_core_disabled_in_exec_child(self):
        result = target.run_media_process(["ffmpeg", self.tool, "allocation"], timeout=5,
                                         memory_bytes=64 * 1024**2, stdout=subprocess.PIPE)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["denied"])
        self.assertEqual(payload["limit"], [64 * 1024**2] * 2)
        self.assertEqual(payload["core"], [0, 0])
        self.assertEqual(result.args, ["ffmpeg"])

    def test_default_candidate_limit_is_installed_not_only_declared(self):
        result = target.run_media_process(["ffmpeg", self.tool, "limits"], timeout=5, stdout=subprocess.PIPE)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["limit"], [target.DEFAULT_ADDRESS_SPACE_BYTES] * 2)
        self.assertEqual(payload["core"], [0, 0])

    def test_timeout_kills_and_reaps_the_actual_exec_pid(self):
        marker = self.root / "pid.txt"
        with self.assertRaises(target.MediaProcessError) as caught:
            target.run_media_process(["ffmpeg", self.tool, "sleep", marker], timeout=1)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertTrue(marker.exists(), "test decoder must start before timeout")
        pid = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_nonzero_result_has_static_error_no_command_or_stderr(self):
        secretish = str(self.root / "not-a-secret-private-path")
        with self.assertRaises(target.MediaProcessError) as caught:
            target.run_media_process(["ffmpeg", self.tool, "failure", secretish], timeout=5)
        self.assertEqual(caught.exception.returncode, 42)
        self.assertEqual(caught.exception.code, "failed")
        self.assertNotIn(secretish, repr(caught.exception))
        self.assertFalse(hasattr(caught.exception, "stderr"))
        self.assertEqual(target.run_media_process(["ffmpeg", self.tool, "failure", secretish], timeout=5, check=False).returncode, 42)

    def test_captured_probe_output_is_bounded_before_parent_read(self):
        with self.assertRaises(target.MediaProcessError) as caught:
            target.run_media_process(["ffmpeg", self.tool, "large"], timeout=5, stdout=subprocess.PIPE)
        self.assertEqual(caught.exception.code, "output")


if __name__ == "__main__":
    unittest.main()
