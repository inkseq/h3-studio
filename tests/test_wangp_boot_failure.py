"""A failed WanGP launch is terminal only once its runtime has stopped (#871)."""

import importlib.util
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PATH = Path(__file__).resolve().parents[1] / "deploy" / "wangp" / "bootstrap.py"
spec = importlib.util.spec_from_file_location("wangp_boot_failure_test", PATH)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def gpu(apps):
    return mock.patch.object(
        subprocess, "run", return_value=SimpleNamespace(stdout=apps)
    )


class RuntimeStoppedTests(unittest.TestCase):
    def check(self, killpg, apps):
        with (
            mock.patch.object(bootstrap.os, "killpg", side_effect=killpg),
            gpu(apps),
            mock.patch.object(bootstrap.time, "sleep"),
        ):
            return bootstrap.runtime_stopped(1234, wait=0)

    def test_empty_session_and_idle_gpu_is_stopped(self):
        self.assertTrue(self.check(ProcessLookupError, ""))

    def test_any_doubt_stays_unknown(self):
        self.assertFalse(self.check(None, ""))  # A session member is alive.
        self.assertFalse(self.check(ProcessLookupError, "4321\n"))  # GPU in use.
        self.assertFalse(self.check(PermissionError, ""))
        with (
            mock.patch.object(bootstrap.os, "killpg", side_effect=ProcessLookupError),
            mock.patch.object(subprocess, "run", side_effect=OSError),
        ):
            self.assertFalse(bootstrap.runtime_stopped(1234, wait=0))


if __name__ == "__main__":
    unittest.main()
