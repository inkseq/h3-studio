"""Safe startup evidence and actual native callbacks, with no GPU/network work."""
from types import SimpleNamespace as NS
import json
from pathlib import Path
import tempfile
import unittest

from studio_platform.dstack_capacity import DstackCapacity, DstackError, image_preparation, native_preparation
from studio_platform.inference.wangp_contract import HostReadiness, safe_model_progress
from studio_platform.runtime_hosts.wangp_session import ModelLoadProgress, PinnedWanGPSession
from studio_platform.wangp_bootstrap import safe_preparation_report, WanGPSSHHost
from test_dstack_capacity import FakeClient, Store, request, INTENT


class SafeStartupProgressTests(unittest.TestCase):
    def test_receipt_counts_are_completed_files_without_private_text(self):
        result = safe_preparation_report({"phase": "model_download", "started_unix": 100,
            "phase_started_unix": 120, "updated_unix": 125,
            "bytes_complete": 5, "bytes_total": 100, "files_complete": 1, "files_total": 11,
            "path": "SECRET", "url": "SECRET", "token": "SECRET", "log": "SECRET"})
        self.assertEqual(result["download"], {"bytes_complete": 5, "bytes_total": 100,
            "files_complete": 1, "files_total": 11, "basis": "completed_files"})
        self.assertEqual(result["phase_started_at"], 120)
        self.assertNotIn("SECRET", json.dumps(result))

    def test_older_source_timestamp_is_not_fabricated_or_unknown_counter_zeroed(self):
        result = safe_preparation_report({"phase": "model_download", "updated_unix": 125})
        self.assertIsNone(result["phase_started_at"])
        self.assertIsNone(result["download"])
        for bad in (True, float("nan"), float("inf"), -1, "123"):
            result = safe_preparation_report({"phase": "model_download", "updated_unix": bad,
                "bytes_complete": bad, "bytes_total": 100, "files_complete": 1, "files_total": 11})
            self.assertIsNone(result["updated_at"])
            self.assertIsNone(result["download"])

    def test_exact_download_failure_is_preserved_but_exception_text_is_not(self):
        result = safe_preparation_report({"phase": "setup_failed", "state": "failed",
            "error_code": "model_download_disk_headroom", "failure_phase": "model_download",
            "exception": "https://SECRET?token=SECRET"})
        self.assertEqual(result["reason_code"], "model_download_disk_headroom")
        self.assertEqual(result["failure_phase"], "model_download")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(safe_preparation_report({"state": "failed", "error_code": "SECRET"})["reason_code"],
            "UnclassifiedBootstrapFailure")

    def test_dstack_image_counter_total_can_be_provisional_without_leaking_layers(self):
        submission = {"image_pull_progress": {"downloaded_bytes": 50, "extracted_bytes": 60,
            "total_bytes": 10, "is_total_bytes_final": False, "layers": "SECRET"}}
        result = image_preparation(submission)
        self.assertEqual(result["downloaded_bytes"], 50)
        self.assertFalse(result["is_total_bytes_final"])
        self.assertNotIn("SECRET", json.dumps(result))
        submission["image_pull_progress"]["downloaded_bytes"] = True
        self.assertIsNone(image_preparation(submission))

    def test_preparation_callback_is_revalidated(self):
        result = native_preparation({"phase": "model_download", "updated_at": 120,
            "download": {"bytes_complete": 0, "bytes_total": 100, "files_complete": 0,
                "files_total": 11, "private": "SECRET"},
            "model": {"state": "loading", "updated_at": 130, "phase_started_at": 125, "prompt": "SECRET"},
            "reason_code": "SECRET", "traceback": "SECRET"})
        self.assertEqual(result["model"]["state"], "loading")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_unhashable_remote_enum_fields_do_not_break_safe_progress_read(self):
        for malformed in ([], {}, ["SECRET"], {"SECRET": 1}):
            self.assertEqual(safe_preparation_report({"phase": malformed, "state": malformed})["phase"], "unknown")
            self.assertIsNone(safe_model_progress({"state": malformed}))
            self.assertEqual(native_preparation({"phase": malformed, "reason_code": malformed})["phase"], "unknown")

    def test_ssh_report_returns_only_sanitized_progress_not_raw_numeric_field_values(self):
        host = WanGPSSHHost.__new__(WanGPSSHHost)
        host.config = NS()
        host.run = lambda script: {"state": "preparing", "phase": "model_download", "identity": {},
            "updated_unix": "SECRET", "bytes_complete": "SECRET", "bytes_total": 100,
            "files_complete": 1, "files_total": 11}
        report = host.report()
        self.assertIsNone(report["preparation"]["download"])
        self.assertNotIn("bytes_complete", report)
        self.assertNotIn("updated_unix", report)
        self.assertNotIn("SECRET", json.dumps(report))

    def test_image_pull_progress_flows_through_owned_run_observation(self):
        store = Store()
        client = FakeClient(store)
        capacity = DstackCapacity(client, store, clock=lambda: 20)
        capacity.start(request(), "ssh-ed25519 offline")
        client.run.update(status="provisioning", latest_job_submission={"status": "pulling",
            "image_pull_progress": {"downloaded_bytes": 30, "extracted_bytes": 0,
                "total_bytes": 100, "is_total_bytes_final": True}})
        value = capacity.observe(INTENT)
        self.assertEqual(value["preparation"]["phase"], "image_pull")
        self.assertEqual(value["preparation"]["image"]["downloaded_bytes"], 30)
        self.assertFalse(value["ready"])

    def test_specific_native_reason_and_progress_survive_failure_recovery(self):
        store = Store()
        client = FakeClient(store)
        capacity = DstackCapacity(client, store, clock=lambda: 20)
        capacity.start(request(), "ssh-ed25519 offline")
        client.running()
        def read(_binding, _run):
            error = DstackError("dstack_runtime_bootstrap_failed")
            error.preparation = {"phase": "setup_failed", "updated_at": 19,
                "phase_started_at": 18, "reason_code": "model_download_disk_headroom",
                "failure_phase": "model_download"}
            raise error
        capacity.readiness = read
        value = capacity.observe(INTENT)
        self.assertEqual(value["reason_code"], "model_download_disk_headroom")
        self.assertEqual(value["preparation"]["phase_started_at"], 18)
        self.assertFalse(value["ready"])
        capacity.readiness = lambda binding, run: HostReadiness(binding["manifest_digest"],
            INTENT, "c"*32, True, preparation={"phase": "runtime_ready", "updated_at": 20,
                "model": {"state": "not_loaded", "updated_at": 20, "phase_started_at": 20}})
        value = capacity.observe(INTENT)
        self.assertTrue(value["ready"])
        self.assertEqual(value["model_load_state"], "not_loaded")
        self.assertNotIn("reason_code", value["preparation"])


class ActualModelProgressTests(unittest.TestCase):
    def test_callback_is_added_to_same_submission_without_altering_accepted_settings(self):
        observer = ModelLoadProgress(lambda: "not_loaded", clock=lambda: 100)
        class Native:
            active_job = None
            calls = []
            def get_default_settings(self, model):
                return {"model_type": model, "num_inference_steps": 20}
            def submit_task(self, settings, *, callbacks):
                self.calls.append((settings, callbacks))
                callbacks.on_status("Loading Model pinned model...")
                self.active_job = NS(done=False)
                return self.active_job
        with tempfile.TemporaryDirectory() as temporary:
            native = Native()
            session = PinnedWanGPSession(native, Path(temporary), quiesce=lambda: None,
                worker_alive=lambda: False, model_progress=observer)
            settings = {"model_type": "minimax_h3_ref2va", "num_inference_steps": 50, "prompt": "SECRET"}
            handle = session.submit_task(settings)
            self.assertEqual(native.calls, [(settings, observer)])
            self.assertEqual(session.preparation()["model"]["state"], "loading")
            self.assertEqual(handle.observe().state, "running")
            self.assertEqual(len(native.calls), 1)
            self.assertNotIn("SECRET", json.dumps(session.preparation()))

    def test_actual_loading_then_probe_confirmed_loaded_and_clock_rollback(self):
        now, loaded = [100], ["not_loaded"]
        observer = ModelLoadProgress(lambda: loaded[0], clock=lambda: now[0])
        self.assertEqual(observer.snapshot()["state"], "not_loaded")
        now[0] = 110
        observer.on_progress(NS(phase="loading_model", status="SECRET prompt"))
        now[0] = 115
        observer.on_progress(NS(phase="loading_model"))
        self.assertEqual(observer.snapshot()["phase_started_at"], 110)
        observer.on_progress(NS(phase="inference"))
        self.assertEqual(observer.snapshot()["state"], "loading")
        loaded[0] = "loaded"
        now[0] = 90  # Wall-clock adjustment cannot make progress move backwards.
        observer.on_progress(NS(phase="inference"))
        self.assertEqual(observer.snapshot(), {"state": "loaded", "phase_started_at": 115, "updated_at": 115})
        self.assertNotIn("SECRET", json.dumps(observer.snapshot()))

    def test_model_load_failure_preserves_static_reason_without_error_message(self):
        loaded = ["not_loaded"]
        observer = ModelLoadProgress(lambda: loaded[0], clock=lambda: 100)
        observer.on_progress(NS(phase="loading_model"))
        observer.on_error(NS(message="SECRET prompt and signed URL", stage="runtime"))
        value = observer.snapshot()
        self.assertEqual(value["state"], "not_loaded")
        self.assertEqual(value["reason_code"], "wangp_model_load_failed")
        loaded[0] = "loaded"
        observer.on_complete(NS(private="SECRET"))
        self.assertEqual(observer.snapshot()["state"], "loaded")
        self.assertNotIn("reason_code", observer.snapshot())

    def test_pinned_preload_status_sets_loading_before_first_block_callback(self):
        observer = ModelLoadProgress(lambda: "not_loaded", clock=lambda: 100)
        observer.on_status("Loading Model SECRET model name...")
        self.assertEqual(observer.snapshot()["state"], "loading")
        self.assertNotIn("SECRET", json.dumps(observer.snapshot()))

    def test_unavailable_probe_and_malformed_model_payload_never_claim_loaded(self):
        def broken():
            raise RuntimeError("SECRET")
        observer = ModelLoadProgress(broken, clock=lambda: 100)
        observer.on_progress(NS(phase="inference"))
        self.assertEqual(observer.snapshot()["state"], "not_observed")
        self.assertIsNone(safe_model_progress({"state": "SECRET"}))
        value = safe_model_progress({"state": "loading", "updated_at": True,
            "phase_started_at": float("nan"), "private": "SECRET"})
        self.assertIsNone(value["updated_at"])
        self.assertIsNone(value["phase_started_at"])
        self.assertNotIn("SECRET", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
