"""Shared boundary safety invariants; no provider or GPU calls."""

import copy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from studio_platform.capacity_policy import cold_unadmitted_runtime, window_open
from studio_platform.dstack_capacity import (
    assert_run_owned,
    request_from_document,
    DstackError,
)
from studio_platform.slot_lock import slot_lock
from test_dstack_capacity import request, PUB, RUN


class CapacityContractTests(unittest.TestCase):
    def test_cold_exception_never_accepts_prior_worker_or_ready_latch(self):
        base = {"provider_instance_id": "123", "state": "starting", "observed_at": 100}
        self.assertTrue(cold_unadmitted_runtime(base, [], "123", 105, 10))
        for changed, workers in (
            ({}, [{"state": "offline"}]),
            ({"runtime_incarnation": "old"}, []),
            ({"first_runtime_ready_at": 1}, []),
            ({"provider_instance_id": "other"}, []),
            ({"observed_at": 80}, []),
            ({"observed_at": 106}, []),
            ({"observed_at": True}, []),
            ({"observed_at": float("nan")}, []),
            ({"state": "ready"}, []),
        ):
            with self.subTest(changed=changed, workers=workers):
                self.assertFalse(
                    cold_unadmitted_runtime(
                        {**base, **changed}, workers, "123", 105, 10
                    )
                )
        self.assertEqual(
            base,
            {"provider_instance_id": "123", "state": "starting", "observed_at": 100},
        )

    def test_deadline_is_strict_and_continuing_window_uses_existing_policy(self):
        self.assertTrue(window_open({"hard_deadline": 110}, {}, 109))
        self.assertFalse(window_open({"hard_deadline": 110}, {}, 110))
        for deadline in (True, "110", float("inf"), float("nan")):
            self.assertFalse(window_open({"hard_deadline": deadline}, {}, 100))
        with patch(
            "studio_platform.worker_admission.dstack_policy_reason",
            return_value="policy_changed",
        ) as check:
            self.assertFalse(window_open({"hard_deadline": None}, {"id": "node"}, 100))
            check.assert_called_once_with({"hard_deadline": None}, {"id": "node"}, 100)

    def test_request_decode_preserves_original_receipt_and_tuple_identity(self):
        original = json.loads(json.dumps(asdict(request())))
        saved = copy.deepcopy(original)
        decoded = request_from_document(original)
        self.assertEqual(decoded, request())
        self.assertEqual(original, saved)
        self.assertIsInstance(decoded.gpu_names, tuple)
        self.assertIsInstance(original["gpu_names"], list)

    def test_ownership_public_boundary_refuses_changed_identity_and_config(self):
        binding = request().binding(PUB)
        run = {
            "id": RUN,
            "project_name": "sixnine",
            "run_spec": copy.deepcopy(binding["run_spec"]),
        }
        assert_run_owned(run, binding, "sixnine")
        for field, value in (("project_name", "other"), ("id", "not-a-uuid")):
            with self.assertRaises(DstackError):
                assert_run_owned({**run, field: value}, binding, "sixnine")
        altered = copy.deepcopy(run)
        altered["run_spec"]["configuration"]["max_price"] = 99
        with self.assertRaises(DstackError):
            assert_run_owned(altered, binding, "sixnine")

    def test_shared_lock_excludes_and_recovers_and_import_has_no_worker_dependency(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            with slot_lock(directory, "endpoint") as first:
                self.assertTrue(first)
                with slot_lock(directory, "endpoint") as second:
                    self.assertFalse(second)
            with slot_lock(directory, "endpoint") as recovered:
                self.assertTrue(recovered)
            self.assertEqual(len(list(Path(directory).glob("*.slot"))), 1)
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import studio_platform.slot_lock; import studio_platform.capacity_policy; "
                "assert 'studio_platform.worker' not in sys.modules; "
                "assert 'studio_platform.dstack_operator' not in sys.modules; "
                "assert 'httpx' not in sys.modules",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_compatibility_exports_are_same_functions_not_parallel_implementations(
        self,
    ):
        from studio_platform import worker, dstack_operator, dstack_capacity

        self.assertIs(worker._slot_lock, slot_lock)
        self.assertIs(dstack_operator._window_open, window_open)
        self.assertIs(dstack_operator._cold_unadmitted_runtime, cold_unadmitted_runtime)
        self.assertIs(dstack_operator._request, request_from_document)
        self.assertIs(dstack_capacity._owns, assert_run_owned)
