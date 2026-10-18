"""CPU-only boot state machine tests; never SSH, rent, install, or generate."""
from dataclasses import replace
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import test_platform_repository as ledger
from studio_platform.lium_bootstrap import (BootConfig, BootController, BootError, COMFY_REVISION,
    MODEL_REVISION, SSHHost, safe_bootstrap_diagnosis, main)
from studio_platform.worker import Outcome


POD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
GPU = "GPU-01234567-0123-0123-0123-012345678901"


class FakeHost:
    def __init__(self):
        self.starts = self.uploads = self.tunnels = 0
        self.identity = None
        self.state = "ready"
        self.start_error = False
        self.patch = {}
        self.manifest = json.loads((Path(__file__).resolve().parents[1]/"model_manifest.json").read_text())

    def upload(self, files):
        self.uploads += 1
        assert set(files) == {"bootstrap_cloud.py", "model_manifest.json"}

    def start(self, identity):
        self.starts += 1
        self.identity = identity
        if self.start_error:
            raise OSError("lost response")

    def report(self):
        return {"identity": self.identity, "state": self.state, "phase": "download",
            "model_revision": MODEL_REVISION, "comfyui_revision": COMFY_REVISION,
            "actual_comfy_revision": COMFY_REVISION,
            "runtime": {"gpu_total_bytes": 96*1024**3},
            "gpus": [{"uuid": GPU, "memory_mib": 98304, "name": "test GPU"}],
            "files": {item["path"]: {"state": "verified_size", "revision": MODEL_REVISION,
                "size_bytes": item["size_bytes"]} for item in self.manifest["files"]}, **self.patch}

    def open_tunnel(self, port):
        self.tunnels += 1

    def close(self):
        pass


class FakeBackend:
    def __init__(self):
        self.submissions = self.reconciles = self.fetches = 0
        self.fail_submit = False
        self.queue = {"queue_running": [], "queue_pending": []}
        self.outcome = Outcome("running", "task-test")

    def _json(self, *args):
        return self.queue

    def submit(self, graph, tag):
        self.submissions += 1
        assert graph
        if self.fail_submit:
            raise OSError("unknown")
        return "task-test"

    def poll(self, *args):
        return self.outcome

    def reconcile(self, tag):
        self.reconciles += 1
        return self.outcome

    def fetch(self, job, tag, task, directory, heartbeat):
        self.fetches += 1
        return {"video": directory/"fake.mp4", "audio": directory/"fake.flac"}

    def close(self):
        pass


class FakeFleet:
    def __init__(self, config, repo, path):
        self.config, self.path = config, path
        self.starts = self.drains = self.stops = 0

    def start(self):
        self.starts += 1

    def tick(self):
        return {"running": 1}

    def drain(self):
        self.drains += 1

    def shutdown(self):
        self.stops += 1


class BootTests(ledger.LedgerCase):
    def setUp(self):
        super().setUp()
        root = Path(self.temp.name)
        self.config = BootConfig(root/"boot", Path(__file__).resolve().parents[1], root/"key", root/"known_hosts",
            18881, "h3-live-test", enabled=True)
        self.repo.configure_pool("boot-test", max_instances=2, max_physical_gpus=2)
        self.intent = self.repo.reserve_instance_intent(self.scope, "boot-test", "bootstrap-test",
            physical_gpus=1, slots=1, reserved_cost_microusd=100_000, hard_deadline=self.now+10000,
            budget_account_ids=("owner-budget",), dry_run=False, provider="lium")
        self.repo.update_instance(self.intent["id"], "creating")
        self.repo.update_instance(self.intent["id"], "starting", provider_instance_id=POD)
        self.host, self.backend = FakeHost(), FakeBackend()
        self.provider_calls = []
        def coordinates(tag, instance):
            self.provider_calls.append((tag, instance))
            return {"host": "8.8.8.8", "port": 2022, "instance_id": POD}
        self.provider = SimpleNamespace(ssh_connection=coordinates)

    def controller(self, **changes):
        return BootController(self.repo, self.provider, replace(self.config, **changes),
            ssh_factory=lambda *a: self.host, backend_factory=lambda **k: self.backend,
            fleet_factory=FakeFleet, verify_smoke=lambda paths, request: {"offline_test": True})

    def tick(self, controller):
        return controller.tick(self.intent["id"])

    def test_disabled_has_no_files_provider_or_ledger_access(self):
        controller = self.controller(enabled=False)
        controller.repo = None
        self.assertEqual(controller.tick("not-an-id")["state"], "disabled")
        self.assertFalse(self.config.work_dir.exists())
        self.assertEqual(self.provider_calls, [])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main([]), 0)
        self.assertIn('"state": "disabled"', output.getvalue())

    def test_qualification_opt_in_and_pinned_boot_identity(self):
        controller = self.controller()
        result = self.tick(controller)
        self.assertEqual(result, {"state": "ready_for_qualification", "generation_verified": False})
        self.assertEqual((self.host.starts, self.host.uploads, self.backend.submissions), (1, 1, 0))
        self.tick(controller)
        self.assertEqual(self.host.starts, 1)
        self.assertEqual(self.provider_calls, [(self.intent["id"], POD)])
        self.assertIn("bootstrap_cloud.py", self.host.identity["sources"])

    def test_lost_boot_start_never_relaunches_after_restart(self):
        self.host.start_error = True
        self.assertEqual(self.tick(self.controller())["state"], "bootstrap_start_unknown")
        self.host.start_error = False
        self.assertEqual(self.tick(self.controller())["state"], "ready_for_qualification")
        self.assertEqual((self.host.starts, self.host.uploads), (1, 1))

    def test_unknown_remote_marker_does_not_restart(self):
        self.host.start_error = True
        self.tick(self.controller())
        self.host.identity = None
        self.assertEqual(self.tick(self.controller())["state"], "bootstrap_start_unknown")
        self.assertEqual(self.host.starts, 1)

    def test_unknown_runtime_reports_reconciliation_without_relaunch_or_failure_commit(self):
        self.host.state = "unknown"
        self.host.patch = {"phase":"runtime_start_unknown", "failure_phase":"runtime_manifest",
            "error_code":"wangp_configuration_permissions", "error_type":"ValueError", "log":"SECRET"}
        controller = self.controller()
        first = self.tick(controller)
        self.assertEqual(first, {"state":"bootstrap_reconciliation_required", "phase":"runtime_start_unknown",
            "failure_phase":"runtime_manifest", "error_code":"wangp_configuration_permissions", "error_type":"ValueError"})
        path = self.config.work_dir/self.intent['id']/'bootstrap-state.json'
        original = path.read_bytes()
        self.assertEqual(self.tick(controller), first)
        self.assertEqual(self.tick(self.controller()), first)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(json.loads(original)['phase'], 'booting')
        self.assertEqual((self.host.starts, self.host.uploads, self.backend.submissions), (1,1,0))
        self.assertNotIn('SECRET', json.dumps(first))

    def test_bad_gpu_or_revision_never_smokes(self):
        for patch in ({"actual_comfy_revision": "0"*40}, {"gpus": []},
                      {"runtime": {"gpu_total_bytes": 32*1024**3}}, {"files": {}}):
            self.host.patch = patch
            with self.subTest(patch=patch), self.assertRaises(BootError):
                self.tick(self.controller(smoke_enabled=True))
        self.assertEqual(self.backend.submissions, 0)

    def test_config_port_change_fails_before_another_ssh_connection(self):
        self.tick(self.controller())
        with self.assertRaisesRegex(BootError, "receipt_identity_conflict"):
            self.tick(self.controller(local_port=18882))
        self.assertEqual(len(self.provider_calls), 1)

    def test_smoke_unknown_submission_reconciles_without_resubmit(self):
        self.backend.fail_submit = True
        self.assertEqual(self.tick(self.controller(smoke_enabled=True))["state"], "smoke_submission_unknown")
        self.backend.fail_submit = False
        self.assertEqual(self.tick(self.controller(smoke_enabled=True))["state"], "smoke_running")
        self.assertEqual((self.backend.submissions, self.backend.reconciles), (1, 1))
        self.backend.outcome = Outcome("succeeded", "task-test")
        self.assertEqual(self.tick(self.controller(smoke_enabled=True))["state"], "qualified")
        self.assertEqual(self.backend.submissions, 1)

    def test_smoke_busy_and_failed_not_qualified_or_retried(self):
        controller = self.controller(smoke_enabled=True, fleet_enabled=True)
        self.backend.queue["queue_running"] = ["foreign"]
        self.assertEqual(self.tick(controller)["state"], "qualification_upstream_busy")
        self.assertEqual(self.backend.submissions, 0)
        self.backend.queue["queue_running"] = []
        self.backend.outcome = Outcome("failed", "task-test")
        self.assertEqual(self.tick(controller)["state"], "qualification_failed")
        self.assertEqual(self.tick(controller)["state"], "qualification_failed")
        self.assertIsNone(controller.fleet)
        self.assertEqual(self.backend.submissions, 1)

    def test_media_verification_failure_never_registers_fleet(self):
        controller = self.controller(smoke_enabled=True, fleet_enabled=True)
        self.backend.outcome = Outcome("succeeded", "task-test")
        def reject(*args):
            raise BootError("qualification_bad_media")
        controller.verify_smoke = reject
        with self.assertRaisesRegex(BootError, "qualification_bad_media"):
            self.tick(controller)
        self.assertIsNone(controller.fleet)

    def test_fleet_after_qualification_once_and_restart_requires_recovery(self):
        controller = self.controller(smoke_enabled=True, fleet_enabled=True)
        self.assertEqual(self.tick(controller)["state"], "smoke_running")
        self.assertIsNone(controller.fleet)
        self.backend.outcome = Outcome("succeeded", "task-test")
        self.assertEqual(self.tick(controller)["state"], "fleet_running")
        self.assertEqual(self.tick(controller)["state"], "fleet_running")
        self.assertEqual(controller.fleet.starts, 1)
        self.assertEqual(controller.fleet.config.slots[0].spec.physical_gpu_ids, (GPU,))
        self.assertEqual(controller.fleet.config.slots[0].spec.recipe_ids, ("h3-base-fl2va-v1",))
        persisted = json.loads(controller.fleet.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["version"], 1)
        self.assertNotIn("output_delivery", persisted["slots"][0])
        # Historical reader rejects unknown fields; preserve its exact schema,
        # not merely the new reader's optional-field tolerance/fingerprint.
        self.assertEqual(set(persisted["slots"][0]), {"worker_id", "pool", "provider", "instance_id",
            "physical_gpu_ids", "recipe_ids", "model_id", "configuration_id", "backend",
            "engine_manifest_digest", "enabled", "endpoint", "allowed_origins", "comfy_revision", "confirmed_idle"})
        self.assertEqual(self.tick(self.controller(smoke_enabled=True, fleet_enabled=True))["state"], "fleet_recovery_required")
        self.assertEqual(self.backend.submissions, 1)

    def test_deadline_prevents_boot_and_draining_prevents_new_work(self):
        self.now += 9000
        self.assertEqual(self.tick(self.controller())["state"], "bootstrap_deadline_insufficient")
        self.assertEqual(self.host.starts, 0)
        self.repo.update_instance(self.intent["id"], "draining")
        self.assertEqual(self.tick(self.controller())["state"], "instance_not_admitting")
        self.assertEqual(self.provider_calls, [])

    def test_fleet_exited_child_is_not_reported_running(self):
        controller = self.controller(smoke_enabled=True, fleet_enabled=True)
        self.backend.outcome = Outcome("succeeded", "task-test")
        self.tick(controller)
        controller.fleet.tick = lambda: {"children": [{"state": "exited", "exit_code": 1}]}
        self.assertEqual(self.tick(controller)["state"], "fleet_attention_required")

    def test_idle_requires_exact_instance_and_continuous_empty_queue(self):
        controller = self.controller()
        self.tick(controller)
        with self.assertRaisesRegex(BootError, "not_bound"):
            controller.idle_probe(self.intent["id"], "other")
        first = controller.idle_probe(self.intent["id"], POD)
        self.now += 10
        second = controller.idle_probe(self.intent["id"], POD)
        self.assertEqual(first.idle_since, second.idle_since)
        self.backend.queue["queue_pending"] = ["job"]
        self.assertFalse(controller.idle_probe(self.intent["id"], POD).idle)
        self.backend.queue["queue_pending"] = []
        self.now += 10
        self.assertGreater(controller.idle_probe(self.intent["id"], POD).idle_since, first.idle_since)

    def test_failed_setup_static_diagnosis_persists_after_restart_and_destroy(self):
        self.host.state = "failed"
        self.host.patch = {"phase": "failed", "failure_phase": "download_file", "error_code": "ModelDownloadFailed",
            "error_type": "SetupError", "failure_details": [{"phase": "download_file", "error_type": "RuntimeError"}],
            "log": "SECRET_LOG_SHOULD_NOT_BE_RETAINED", "url": "https://private.invalid/?sig=SECRET_SIGNATURE"}
        controller = self.controller()
        result = self.tick(controller)
        self.assertEqual(result["error_code"], "ModelDownloadFailed")
        self.assertEqual(result["failure_phase"], "download_file")
        self.assertEqual(result["failure_details"], [{"phase": "download_file", "error_type": "RuntimeError"}])
        folder = self.config.work_dir/self.intent["id"]
        saved = json.loads((folder/"bootstrap-state.json").read_text())
        self.assertEqual(saved["phase"], "bootstrap_failed")
        self.assertEqual(saved["failure"], {k: v for k, v in result.items() if k != "state"})
        self.host.state = "ready"  # A failed marker must not automatically recover.
        other = self.controller()
        self.assertEqual(self.tick(other), result)
        self.assertEqual((self.host.starts, self.host.uploads), (1, 1))
        self.assertIsNone(other.host)
        self.repo.update_instance(self.intent["id"], "draining")
        self.repo.update_instance(self.intent["id"], "destroying")
        self.repo.update_instance(self.intent["id"], "destroyed", destruction_confirmed=True)
        self.assertEqual(other.status(self.intent["id"]), result)
        self.assertEqual(self.backend.submissions, 0)
        raw = (folder/"bootstrap-state.json").read_text() + (folder/"bootstrap-status.json").read_text()
        for denied in ("SECRET_LOG", "SECRET_SIGNATURE", "private.invalid"):
            self.assertNotIn(denied, raw)

    def test_untrusted_failure_fields_and_progress_phases_are_masked(self):
        self.host.state = "failed"
        self.host.patch = {"phase": "https://private.invalid/?sig=SECRET", "error_code": "SECRET_TOKEN",
                           "error_type": "SECRET_PASSWORD", "failure_phase": {"secret": "SECRET"}}
        result = self.tick(self.controller())
        self.assertEqual(result["error_code"], "UnclassifiedBootstrapFailure")
        self.assertEqual(result["error_type"], "UnknownSetupError")
        self.assertEqual(result["phase"], "unknown")
        self.assertEqual(result["failure_phase"], "unknown")
        diagnosis = safe_bootstrap_diagnosis({"failure_details": [{"phase": "download_file", "error_type": "OSError",
            "detail": "SECRET"}]*100})
        self.assertEqual(len(diagnosis["failure_details"]), 5)
        self.assertNotIn("SECRET", json.dumps(diagnosis))

    def test_download_stop_uncertainty_and_original_cause_survive_retained_cpu_receipts(self):
        self.host.state = "failed"
        self.host.patch = {"phase": "setup_failed", "failure_phase": "model_download",
            "error_code": "model_download_stop_unconfirmed", "error_type": "ValueError",
            "download_failure": {"error_code": "model_download_timeout", "stop_status": "unconfirmed",
                                 "sdk_log": "SECRET"}}
        result = self.tick(self.controller())
        self.assertEqual(result["state"], "bootstrap_failed")
        self.assertEqual(result["error_code"], "model_download_stop_unconfirmed")
        self.assertEqual(result["download_failure"],
            {"error_code": "model_download_timeout", "stop_status": "unconfirmed"})
        self.host.state = "ready"  # Later reports cannot erase the durable failure.
        self.assertEqual(self.tick(self.controller()), result)
        self.repo.update_instance(self.intent["id"], "draining")
        self.repo.update_instance(self.intent["id"], "destroying")
        self.repo.update_instance(self.intent["id"], "destroyed", destruction_confirmed=True)
        self.assertEqual(self.controller().status(self.intent["id"]), result)
        folder = self.config.work_dir/self.intent["id"]
        for name in ("bootstrap-state.json", "bootstrap-status.json"):
            raw = (folder/name).read_text()
            self.assertNotIn("SECRET", raw)
            self.assertIn('"stop_status": "unconfirmed"', raw)
        self.assertEqual((self.host.starts, self.backend.submissions), (1, 0))

    def test_system_package_diagnostics_survive_cpu_restart_and_node_destruction(self):
        self.host.state = "failed"
        packages = {"total": 1, "truncated": False, "mismatches": [
            {"package": "openssl", "expected": "3.0.2-0ubuntu1.18", "observed": "3.0.2-0ubuntu1.20", "log": "SECRET"}]}
        self.host.patch = {"phase": "setup_failed", "failure_phase": "system_package_verification",
            "error_code": "system_package_mismatch", "error_type": "ValueError", "system_package_diagnostics": packages}
        result = self.tick(self.controller())
        self.assertEqual(result["failure_phase"], "system_package_verification")
        self.assertEqual(result["error_type"], "ValueError")
        self.assertEqual(result["system_package_diagnostics"]["total"], 1)
        self.repo.update_instance(self.intent["id"], "draining")
        self.repo.update_instance(self.intent["id"], "destroying")
        self.repo.update_instance(self.intent["id"], "destroyed", destruction_confirmed=True)
        self.assertEqual(self.controller().status(self.intent["id"]), result)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual((self.host.starts, self.backend.submissions), (1, 0))


class SSHInspectionTests(unittest.TestCase):
    """Execute the actual inspection scripts against temporary fake procfs."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/"remote"
        self.proc = Path(self.temp.name)/"proc"
        self.root.mkdir(); self.proc.mkdir()
        (self.proc/"net").mkdir()
        self.identity = {"intent_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "instance_id": POD,
            "configuration_id": "h3-test", "sources": {"bootstrap_cloud.py": "a"*64, "model_manifest.json": "b"*64}}
        (self.root/"sixnine-bootstrap-identity.json").write_text(json.dumps(self.identity))
        (self.root/"setup-status.json").write_text(json.dumps({"state": "failed", "phase": "failed",
            "error_code": "ModelDownloadFailed", "error_type": "SetupError", "events": [
                {"phase": "download_file", "state": "failed", "error_type": "RuntimeError",
                 "url": "https://private.invalid/?sig=SECRET"}, {"phase": "failed", "error_code": "ModelDownloadFailed"}]}))
        self.write_process("100", b"python3\0-c\0read-only inspection text bootstrap_cloud.py\0")
        for name in ("tcp", "tcp6"):
            (self.proc/"net"/name).write_text("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n")
        self.host = SSHHost.__new__(SSHHost)

    def write_process(self, pid, raw):
        (self.proc/pid).mkdir(exist_ok=True)
        (self.proc/pid/"cmdline").write_bytes(raw)

    def execute(self, script, **kwargs):
        script = script.replace("/workspace/h3-studio", self.root.as_posix()).replace("Path('/proc')", "Path("+repr(self.proc.as_posix())+")")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(compile(script, "<offline-remote-inspection>", "exec"), {})
        return json.loads(output.getvalue())

    def test_report_preserves_static_failure_stage_without_signed_url_or_log(self):
        self.host.run = self.execute
        result = self.host.report()
        self.assertEqual(result["error_code"], "ModelDownloadFailed")
        self.assertEqual(result["failure_phase"], "download_file")
        self.assertEqual(result["failure_details"], [{"phase": "download_file", "error_type": "RuntimeError"}])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_idle_proof_requires_complete_process_and_both_listener_scans(self):
        self.host.run = self.execute
        result = self.host.preparation_idle_report()
        self.assertEqual(result["identity"], self.identity)
        self.assertEqual(result["state"], "failed")
        self.assertIs(result["process_visibility_complete"], True)
        self.assertEqual(result["bootstrap_process_count"], 0)
        self.assertEqual(result["comfy_process_count"], 0)
        self.assertIs(result["comfy_port_listening"], False)
        self.assertIsInstance(result["observed_at"], float)
        # Do not match the literal filename in this script's python -c argument.
        self.assertNotIn("cmdline", result)
        (self.proc/"net"/"tcp6").unlink()
        result = self.host.preparation_idle_report()
        self.assertIs(result["process_visibility_complete"], False)
        self.assertIsNone(result["bootstrap_process_count"])
        self.assertIsNone(result["comfy_port_listening"])

    def test_real_setup_comfy_or_listener_blocks_idle_without_retaining_args(self):
        self.host.run = self.execute
        self.write_process("101", b"python3\0-u\0/workspace/h3-studio/bootstrap_cloud.py\0--token=SECRET\0")
        self.write_process("102", b"python3\0/workspace/h3-studio/ComfyUI/main.py\0")
        (self.proc/"net"/"tcp").write_text((self.proc/"net"/"tcp").read_text()+
            "0: 0100007F:1FFC 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 42\n")
        result = self.host.preparation_idle_report()
        self.assertIs(result["process_visibility_complete"], True)
        self.assertEqual(result["bootstrap_process_count"], 1)
        self.assertEqual(result["comfy_process_count"], 1)
        self.assertIs(result["comfy_port_listening"], True)
        self.assertNotIn("SECRET", json.dumps(result))

    def test_unreadable_process_invalid_identity_or_malformed_socket_are_inconclusive(self):
        self.host.run = self.execute
        (self.proc/"100"/"cmdline").unlink()
        self.assertIs(self.host.preparation_idle_report()["process_visibility_complete"], False)
        self.write_process("100", b"python3\0")
        (self.proc/"net"/"tcp6").write_text("garbage\n")
        self.assertIs(self.host.preparation_idle_report()["process_visibility_complete"], False)
        (self.root/"sixnine-bootstrap-identity.json").write_text(json.dumps({**self.identity, "sources": {"url": "SECRET"}}))
        result = self.host.preparation_idle_report()
        self.assertIs(result["process_visibility_complete"], False)
        self.assertEqual(result["identity"], {})
        self.assertNotIn("SECRET", json.dumps(result))

    def test_start_uses_encrypted_local_volume_cache_and_retains_one_shot_marker(self):
        scripts = []
        self.host.run = lambda script, **kwargs: scripts.append(script) or {"state": "started"}
        self.host.start(self.identity)
        script = scripts[0]
        self.assertIn("'--cache-dir','/root/hf-cache'", script)
        self.assertNotIn("/workspace/hf-cache", script)
        self.assertLess(script.index("with marker.open('x')"), script.index("subprocess.Popen"))
        self.assertIn("'state':'already_reserved'", script)
        compile(script, "<offline-start-syntax>", "exec")


if __name__ == "__main__":
    unittest.main()
