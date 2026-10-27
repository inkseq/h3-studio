"""Offline startup observability; no model, runtime server, provider or network."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from studio_platform.runtime_hosts.wangp_startup import (
    read_startup_failure,
    write_startup_failure,
)


class StartupFailureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "failure.json"
        self.identity = dict(
            slot_key="slot-1",
            manifest_digest="a" * 64,
            launch_id="b" * 32,
            pid=os.getpid(),
        )

    def write(self):
        return write_startup_failure(
            self.path,
            **self.identity,
            phase="runtime_verification",
            error=ValueError("wangp_component_hash_mismatch"),
        )

    def test_identity_bound_exclusive_publication(self):
        original = self.write()
        self.assertEqual(read_startup_failure(self.path, **self.identity), original)
        for field, wrong in (
            ("pid", os.getpid() + 1),
            ("slot_key", "other"),
            ("manifest_digest", "c" * 64),
            ("launch_id", "d" * 32),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                read_startup_failure(self.path, **{**self.identity, field: wrong})
        with self.assertRaises(FileExistsError):
            self.write()
        self.assertEqual(read_startup_failure(self.path, **self.identity), original)
        self.assertEqual(list(self.path.parent.glob(".startup-*.tmp")), [])

    def test_unknown_fields_and_nonliteral_codes_are_rejected(self):
        original = self.write()
        for change in (
            {"prompt": "must never be projected"},
            {"error_code": "private-secret"},
            {"inference_verified": True},
            {"pid": True},
            {"observed_at": float("nan")},
        ):
            self.path.write_text(json.dumps({**original, **change}))
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                read_startup_failure(self.path, **self.identity)

    def test_memory_refusals_survive_bounded_preparation_projection(self):
        from studio_platform.wangp_bootstrap import safe_preparation_report

        for suffix in ("ram_total", "ram_available", "vram_free"):
            code = "wangp_profile_memory_headroom_insufficient_" + suffix
            with self.subTest(code=code):
                path = self.path.with_name(suffix + ".json")
                write_startup_failure(
                    path,
                    **self.identity,
                    phase="runtime_session_initialization",
                    error=ValueError(code),
                )
                saved = read_startup_failure(path, **self.identity)
                report = safe_preparation_report(
                    {
                        **saved,
                        "state": "failed",
                        "failure_phase": saved["phase"],
                        "private_log": "must not escape",
                    }
                )
                self.assertEqual(report["reason_code"], code)
                self.assertEqual(report["failure_phase"], saved["phase"])
                self.assertNotIn("private_log", report)

    def test_memory_thresholds_and_refusal_reasons(self):
        from studio_platform.runtime_hosts import wangp_session as session

        cases = (
            ("effective-total-v1", 28, 40, 32, "ram_total"),
            ("effective-total-v1", 29, 0, 27, "vram_free"),
            ("effective-total-v1", 29, 0, 28, None),
            ("effective-total-and-available-v1", 29, 19, 32, "ram_available"),
            ("effective-total-and-available-v1", 28, 19, 27, "ram_total"),
            ("effective-total-and-available-v1", 29, 20, 28, None),
            ("available-headroom-v1", 29, 19, 32, "ram_available"),
            ("available-headroom-v1", 29, 20, 27, "vram_free"),
            ("available-headroom-v1", 29, 20, 28, None),
        )
        for metric, total, available, free, suffix in cases:
            profile = {
                "minimum_free_vram_bytes": 28,
                "runtime": {
                    "ram_admission_metric": metric,
                    "minimum_total_ram_bytes": 29,
                    "minimum_available_ram_bytes": 20,
                },
            }
            torch = SimpleNamespace(
                cuda=SimpleNamespace(mem_get_info=lambda _, value=free: (value, 32))
            )
            with (
                self.subTest(
                    metric=metric, total=total, available=available, free=free
                ),
                patch.object(session, "_available_profile_ram", return_value=available),
                patch.object(
                    session, "_effective_total_profile_ram", return_value=total
                ),
                redirect_stdout(StringIO()),
            ):
                if suffix is None:
                    session._profile_memory_admission(torch, profile)
                else:
                    with self.assertRaises(ValueError) as caught:
                        session._profile_memory_admission(torch, profile)
                    self.assertEqual(
                        str(caught.exception),
                        "wangp_profile_memory_headroom_insufficient_" + suffix,
                    )


if __name__ == "__main__":
    unittest.main()
