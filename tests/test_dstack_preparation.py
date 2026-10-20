"""Progress receipt accuracy/security; no provider or inference operations."""

import json
import copy
import unittest

from studio_platform.dstack_preparation import (
    preparation_view,
    record_preparation,
    safe_preparation,
    download_progress_timing,
    download_timebox_due,
)


class PreparationTests(unittest.TestCase):
    def download(self, *, now=100, bytes_complete=20, files_complete=1, binding=None):
        if binding is None:
            binding = dict(
                intent_id="intent-one",
                run_id="run-one",
                provider_instance_id="123",
                backend="vastai",
                profile_id="native-one",
                mode="ref",
                configuration_id="config-one",
                manifest_digest="a" * 64,
                spec_digest="b" * 64,
            )
        binding["observed_at"] = now
        record_preparation(
            binding,
            dict(
                phase="model_download",
                phase_started_at=90,
                updated_at=95,
                download=dict(
                    bytes_complete=bytes_complete,
                    bytes_total=100,
                    files_complete=files_complete,
                    files_total=3,
                ),
            ),
            now,
        )
        return binding

    def test_download_timebox_requires_both_elapsed_and_no_progress(self):
        binding = self.download()
        self.download(binding=binding, now=1500)
        self.assertFalse(download_timebox_due(binding, 1500))
        self.download(binding=binding, now=1600)
        self.assertFalse(download_timebox_due(binding, 1600))  # Strictly >1500.
        self.download(binding=binding, now=1601)
        self.assertTrue(download_timebox_due(binding, 1601))
        self.download(binding=binding, now=1602, bytes_complete=50)
        self.assertFalse(download_timebox_due(binding, 1602))
        self.download(binding=binding, now=1901, bytes_complete=50)
        self.assertFalse(download_timebox_due(binding, 1901))
        self.download(binding=binding, now=1902, bytes_complete=50)
        self.assertTrue(download_timebox_due(binding, 1902))
        # Restarting a controller and observing identical source heartbeats
        # cannot reset server-latched progress or shorten the phase timebox.
        restored = json.loads(json.dumps(binding))
        self.assertEqual(restored["download_progress"]["started_at"], 100)
        self.assertEqual(restored["download_progress"]["last_progress_at"], 1602)
        self.assertFalse(download_timebox_due(restored, 1933))

    def test_download_clock_identity_and_counter_resets_never_authorize_stop(self):
        original = self.download()
        for change in (
            {"run_id": "changed"},
            {"provider_instance_id": "456"},
            {"manifest_digest": "c" * 64},
            {"download_progress": None},
            {"download_progress": "malformed"},
        ):
            with self.subTest(change=change):
                binding = copy.deepcopy(original)
                binding.update(change)
                self.download(binding=binding, now=2000)
                self.assertFalse(download_timebox_due(binding, 2000))
                if change.get("download_progress", False) is not None:
                    self.assertIsNone(download_progress_timing(binding, 2000))
        for changes in (
            {"phase_started_at": 91},
            {"phase_started_at": 3000},
            {"updated_at": 3000},
            {
                "download": dict(
                    bytes_complete=19, bytes_total=100, files_complete=1, files_total=3
                )
            },
            {
                "download": dict(
                    bytes_complete=20, bytes_total=101, files_complete=1, files_total=3
                )
            },
        ):
            with self.subTest(changes=changes):
                binding = copy.deepcopy(original)
                value = dict(
                    phase="model_download",
                    phase_started_at=90,
                    updated_at=95,
                    download=dict(
                        bytes_complete=20,
                        bytes_total=100,
                        files_complete=1,
                        files_total=3,
                    ),
                )
                value.update(changes)
                binding["observed_at"] = 2000
                record_preparation(binding, value, 2000)
                self.assertFalse(download_timebox_due(binding, 2000))
        # A first future/old native epoch cannot backdate the server phase.
        binding = self.download(now=2000)
        self.assertEqual(download_progress_timing(binding, 2000)["duration_seconds"], 0)
        self.assertFalse(download_timebox_due(binding, 2000))

    def test_download_completion_timing_latches_across_later_phases_and_stop(self):
        binding = self.download()
        self.download(binding=binding, now=400, bytes_complete=100, files_complete=3)
        record_preparation(binding, dict(phase="runtime_start", updated_at=450), 450)
        record_preparation(binding, dict(phase="stopped", updated_at=1000), 1000)
        self.assertEqual(
            download_progress_timing(binding, 2000),
            dict(
                started_at=100,
                duration_seconds=300,
                bytes_complete=100,
                bytes_total=100,
                basis="completed_files",
            ),
        )
        self.assertFalse(download_timebox_due(binding, 2000))
        binding["download_progress"]["started_at"] = 3000
        self.assertIsNone(download_progress_timing(binding, 2000))
        partial = self.download()
        partial["first_stopped_at"] = 400
        record_preparation(partial, dict(phase="provisioning", updated_at=400), 400)
        self.assertEqual(
            download_progress_timing(partial, 2000)["duration_seconds"], 300
        )
        self.assertEqual(download_progress_timing(partial, 2000)["bytes_complete"], 20)

    def test_download_stop_requires_fresh_provider_and_watch_observations(self):
        binding = self.download()
        self.download(binding=binding, now=2000)
        self.assertTrue(download_timebox_due(binding, 2000))
        binding["observed_at"] = 1900
        self.assertFalse(download_timebox_due(binding, 2000))
        binding["observed_at"] = 2000
        binding["download_progress"]["last_observed_at"] = 1900
        self.assertFalse(download_timebox_due(binding, 2000))

    def view(self, binding, public=None, now=200):
        return preparation_view(
            {"payload": {"dstack": binding}},
            {"created_at": 20},
            {"lifecycle_phase": "preparing", **(public or {})},
            now,
        )

    def test_phase_stopwatch_and_download_survive_repeated_poll_and_next_phase(self):
        binding = {"apply_started_at": 20, "observed_at": 150}
        receipt = {
            "phase": "model_download",
            "updated_at": 40,
            "download": {
                "bytes_complete": 60,
                "bytes_total": 100,
                "files_complete": 2,
                "files_total": 3,
            },
        }
        record_preparation(binding, receipt, 100)
        record_preparation(binding, receipt, 120)
        self.assertEqual(binding["preparation"]["phase_started_at"], 100)
        view = self.view(binding, now=150)
        self.assertEqual(view["elapsed_seconds"], 130)
        self.assertEqual(view["phase_elapsed_seconds"], 50)
        self.assertEqual(view["download"]["percent"], 60)
        self.assertEqual(view["download"]["basis"], "completed_files")
        record_preparation(binding, {"phase": "runtime_start", "updated_at": 130}, 130)
        self.assertEqual(binding["preparation"]["phase_started_at"], 130)
        self.assertEqual(self.view(binding, now=150)["download"]["bytes_complete"], 60)

    def test_native_phase_clock_and_stopped_elapsed_are_not_poll_clocks(self):
        binding = {
            "apply_started_at": 20,
            "observed_at": 160,
            "first_stopped_at": 160,
            "stop_started_at": 150,
        }
        record_preparation(
            binding,
            {"phase": "model_download", "phase_started_at": 30, "updated_at": 40},
            100,
        )
        record_preparation(
            binding,
            {"phase": "model_download", "phase_started_at": 30, "updated_at": 40},
            140,
        )
        self.assertEqual(self.view(binding, now=160)["phase_elapsed_seconds"], 130)
        end = self.view(binding, {"lifecycle_phase": "stopped"}, now=1000)
        self.assertEqual(end["phase"], "stopped")
        self.assertEqual(end["elapsed_seconds"], 140)
        self.assertFalse(end["stale"])
        self.assertFalse(end["stalled"])

    def test_unknown_malformed_totals_never_fabricate_percent_or_leak_remote_text(self):
        value = {
            "phase": ["secret"],
            "reason_code": {"token": "secret"},
            "failure_phase": ["secret"],
            "updated_at": float("nan"),
            "phase_started_at": 999999,
            "traceback": "secret",
            "download": {
                "bytes_complete": True,
                "bytes_total": -1,
                "files_complete": float("inf"),
            },
            "image": {
                "downloaded_bytes": 50,
                "extracted_bytes": 4,
                "total_bytes": 100,
                "is_total_bytes_final": False,
                "url": "secret",
            },
            "model": {"state": ["secret"]},
        }
        binding = {"observed_at": 200, "preparation": safe_preparation(value, 200)}
        view = self.view(binding)
        self.assertNotIn("secret", json.dumps(view))
        self.assertEqual(view["model"]["state"], "not_observed")
        self.assertIsNone(view["download"]["percent"])
        self.assertIsNone(view["image"]["bytes_total"])
        self.assertEqual(view["image"]["bytes_complete"], 54)
        self.assertEqual(view["image"]["downloaded_bytes"], 50)

    def test_image_total_measures_download_and_extraction_work(self):
        view = self.view(
            {
                "observed_at": 200,
                "preparation": {
                    "phase": "image_pull",
                    "image": {
                        "downloaded_bytes": 100,
                        "extracted_bytes": 100,
                        "total_bytes": 200,
                        "is_total_bytes_final": True,
                    },
                },
            }
        )
        self.assertEqual(view["image"]["percent"], 100)
        self.assertEqual(view["image"]["bytes_complete"], 200)
        self.assertEqual(view["image"]["downloaded_bytes"], 100)
        self.assertEqual(view["image"]["extracted_bytes"], 100)
        self.assertEqual(view["image"]["basis"], "download_and_extract")

    def test_model_loading_busy_job_and_service_without_model_are_distinct(self):
        binding = {
            "observed_at": 200,
            "preparation": {
                "phase": "runtime_ready",
                "model": {"state": "not_loaded", "updated_at": 198},
            },
        }
        view = self.view(binding, {"native_ready": True, "ready": True})
        self.assertEqual(view["phase"], "awaiting_first_job")
        self.assertEqual(view["model"]["state"], "not_loaded")
        binding["preparation"]["model"] = {
            "state": "loading",
            "phase_started_at": 190,
            "updated_at": 195,
        }
        self.assertEqual(
            self.view(binding, {"current_job": {"job_id": "original"}})["phase"],
            "loading_model",
        )
        self.assertEqual(self.view(binding)["phase_elapsed_seconds"], 10)
        binding["preparation"]["model"] = {"state": "loaded", "updated_at": 199}
        self.assertEqual(
            self.view(binding, {"current_job": {"job_id": "original"}})["phase"],
            "running_job",
        )
        self.assertEqual(self.view(binding, {"ready": True})["phase"], "ready")

    def test_missing_old_receipt_and_specific_failure_are_truthful(self):
        view = self.view(
            {"observed_at": 200, "bootstrap_phase": "runtime_ready"},
            {"native_ready": True},
        )
        self.assertEqual(view["model"]["state"], "not_observed")
        self.assertIsNone(view["download"])
        failed = self.view(
            {
                "observed_at": 200,
                "preparation": {
                    "phase": "failed",
                    "reason_code": "model_download_timeout",
                    "failure_phase": "model_download",
                },
            }
        )
        self.assertEqual(failed["reason_code"], "model_download_timeout")
        self.assertEqual(failed["reason_text"], "权重下载超时")
        waiting = self.view(
            {"observed_at": 200},
            {
                "native_ready": True,
                "reason_code": "dstack_runtime_ready_model_load_unobserved",
                "dispatch_reason_code": "matching_worker_unavailable",
            },
        )
        self.assertEqual(waiting["reason_code"], "matching_worker_unavailable")

    def test_factory_failures_have_static_specific_messages(self):
        for code, text in (
            ("dstack_runtime_ports_exhausted", "模型连接端口已用完"),
            ("dstack_runtime_port_conflict", "端口与另一台机器冲突"),
            ("dstack_runtime_source_selection_missing", "所选模型和运行模式的源码"),
        ):
            with self.subTest(code=code):
                view = self.view({"observed_at": 200}, {"reason_code": code})
                self.assertEqual(view["reason_code"], code)
                self.assertIn(text, view["reason_text"])

    def test_stopped_reason_supersedes_old_error_without_inventing_billing_proof(self):
        binding = {
            "observed_at": 160,
            "first_stopped_at": 160,
            "preparation": {
                "phase": "failed",
                "reason_code": "dstack_runtime_ports_exhausted",
                "model": {"state": "loading"},
            },
        }
        for code, expected in (
            ("dstack_never_allocated", "dstack_never_allocated"),
            (
                "dstack_no_capacity_billing_pending",
                "dstack_no_capacity_billing_pending",
            ),
            ("dstack_run_terminal_billing_pending", None),
            (None, None),
        ):
            with self.subTest(code=code):
                view = self.view(
                    binding,
                    {
                        "lifecycle_phase": "stopped",
                        "reason_code": code,
                        "native_ready": True,
                        "dispatch_reason_code": "matching_worker_unavailable",
                    },
                )
                self.assertEqual(view["phase"], "stopped")
                self.assertEqual(view["reason_code"], expected)
                self.assertNotIn("端口", view["reason_text"] or "")
                self.assertNotIn("billing_state", view)
                self.assertNotIn("no_allocation_proof", view)
        # A pending/unknown result never becomes the confirmed no-allocation code.
        pending = self.view(
            {"observed_at": 200},
            {"reason_code": "dstack_allocation_requires_reconciliation"},
        )
        self.assertEqual(
            pending["reason_code"], "dstack_allocation_requires_reconciliation"
        )
        self.assertIn("尚未确认", pending["reason_text"])

    def test_native_timestamp_does_not_refresh_provider_read_age(self):
        binding = {"observed_at": 100}
        record_preparation(
            binding,
            {
                "phase": "runtime_ready",
                "updated_at": 150,
                "model": {
                    "state": "loading",
                    "updated_at": 155,
                    "phase_started_at": 150,
                },
            },
            100,
            validation_at=160,
        )
        view = self.view(binding, now=160)
        self.assertEqual(view["updated_at"], 150)
        self.assertEqual(view["model"]["updated_at"], 155)
        self.assertTrue(view["stale"])
        self.assertEqual(binding["observed_at"], 100)

    def test_stale_observation_does_not_claim_download_or_model_failed(self):
        binding = {
            "observed_at": 200,
            "preparation": {
                "phase": "model_download",
                "updated_at": 50,
                "download": {"bytes_complete": 60, "bytes_total": 100},
            },
        }
        view = self.view(binding)
        self.assertTrue(view["stalled"])
        self.assertFalse(view["stale"])
        self.assertIsNone(view["reason_code"])
        self.assertIn("尚不能判定失败", view["reason_text"])
        self.assertTrue(self.view(binding, now=240)["stale"])


if __name__ == "__main__":
    unittest.main()
