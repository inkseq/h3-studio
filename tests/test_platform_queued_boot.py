"""Offline real-queue startup contracts; fake hosts never generate or rent."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import select

from studio_platform.control import WorkerControl
from studio_platform.lium_bootstrap import BootConfig, BootError
from studio_platform.production_scaler import ScalerError
from studio_platform.production_scaler_boot import ProductionBoot, run_child
from studio_platform.qualification_profiles import QUEUED_TASK_PROFILE
from studio_platform.repository import registered_workers
from test_platform_production_scaler import configuration, FakeProvider
from test_platform_lium_bootstrap import FakeBackend, FakeHost
from test_platform_repository import LedgerCase


class QueuedTaskBootTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.config = configuration(Path(self.temp.name), self.now)
        self.config.work_dir.mkdir()
        self.repo.configure_pool(self.config.pool, max_instances=2, max_physical_gpus=2)
        self.intent = self.repo.reserve_instance_intent(self.scope, self.config.pool, "queued-boot", physical_gpus=1,
            slots=1, reserved_cost_microusd=2_000_000, hard_deadline=self.now+7200,
            budget_account_ids=["owner-budget"], dry_run=False, provider="lium")
        self.repo.update_instance(self.intent["id"], "creating")
        self.repo.update_instance(self.intent["id"], "starting", provider_instance_id=str(uuid.uuid4()))
        self.intent = self.repo.list_instance_intents()[0]
        self.host, self.backend = FakeHost(), FakeBackend()
        self.backend.kind = "comfy-worker"
        self.provider = FakeProvider(lambda: self.now)
        self.provider.ssh_connection = lambda *args: {"host": "203.0.113.1", "port": 22}
        self.legacy_config = self.config
        self.config = replace(self.config, qualification_profile=QUEUED_TASK_PROFILE)
        self.boot = self.make_boot()

    def make_boot(self, config=None):
        return ProductionBoot(self.repo, self.provider, config or self.config, self.intent, 19300,
            config_path=Path(self.temp.name)/"config.json", ssh_factory=lambda *a: self.host,
            backend_factory=lambda **kw: self.backend,
            verify_smoke=lambda *args: (_ for _ in ()).throw(AssertionError("synthetic verification prohibited")))

    @property
    def receipt(self):
        return self.boot.config.work_dir/self.intent["id"]/"bootstrap-state.json"

    def workers(self):
        with self.repo.engine.connect() as conn:
            return list(conn.execute(select(registered_workers.c.id)).scalars())

    def start(self):
        process = SimpleNamespace(pid=4242, poll=lambda: None, send_signal=lambda value: None)
        with patch.object(self.boot, "_popen_impl", return_value=process):
            return self.boot.tick(self.intent["id"])

    def test_ready_runtime_starts_queue_worker_without_synthetic_post_or_fetch(self):
        result = self.start()
        self.assertEqual(result["state"], "fleet_running")
        self.assertFalse(result["generation_verified"])
        self.assertTrue(result["awaiting_real_task"])
        self.assertEqual(result["qualification_scope"], "runtime_ready_awaiting_real_task")
        self.assertEqual(self.backend.submissions, 0)
        self.assertEqual(self.backend.fetches, 0)
        self.assertEqual(len(self.workers()), 1)
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["qualification_profile"], QUEUED_TASK_PROFILE)
        self.assertEqual(receipt["runtime_validation"], {
            "profile": QUEUED_TASK_PROFILE, "state": "runtime_ready", "generation_verified": False})
        self.assertNotIn("evidence", receipt)
        self.assertNotIn("smoke_task_id", receipt)
        # A subsequent real task may occupy this same endpoint: startup must
        # leave its existing fleet/attempt controls in charge.
        self.backend.queue = {"queue_running": [[1, "real-user-task"]], "queue_pending": []}
        again = self.boot.tick(self.intent["id"])
        self.assertEqual(again["state"], "fleet_running")
        self.assertFalse(again["generation_verified"])
        self.assertEqual(self.backend.submissions, 0)

    def test_busy_or_malformed_queue_does_not_register_worker(self):
        for queue in ({"queue_running": [[1, "foreign"]], "queue_pending": []},
                      {"queue_running": [], "queue_pending": [[1, "foreign"]]},
                      {}, [], {"queue_running": [], "queue_pending": None}):
            self.backend.queue = queue
            with self.subTest(queue=queue):
                result = self.boot.tick(self.intent["id"])
                self.assertEqual(result["state"], "runtime_upstream_busy")
                self.assertFalse(result["generation_verified"])
                self.assertIsNone(self.boot.fleet)
                self.assertEqual(self.workers(), [])
        self.assertEqual(self.backend.submissions, 0)

    def test_draining_keeps_original_tunnel_recoverable_without_restarting_runtime(self):
        self.start()
        self.backend.queue = {"queue_running": [[1, "accepted-real-task"]], "queue_pending": []}
        before = (self.host.starts, self.backend.submissions, self.workers())
        refreshed = []
        self.host.ensure_connected = lambda: refreshed.append(True)
        result = self.boot.tick(self.intent['id'], stopping=True)
        self.assertEqual(result['state'], 'draining')
        self.assertFalse(result['children_done'])
        self.assertEqual(refreshed, [True])
        self.assertEqual((self.host.starts, self.backend.submissions, self.workers()), before)

    def test_ready_report_still_requires_pinned_runtime_weights_and_memory(self):
        for report in ({"actual_comfy_revision": "0"*40}, {"files": {}}, {"gpus": []},
                       {"runtime": {"gpu_total_bytes": 32*1024**3}}):
            self.host.patch = report
            with self.subTest(report=report), self.assertRaises(BootError):
                self.boot.tick(self.intent["id"])
            self.assertEqual(self.workers(), [])
        self.assertEqual(self.backend.submissions, 0)

    def test_startup_resume_does_not_repeat_remote_setup_or_claim_inference(self):
        self.backend.queue = {"queue_running": [[1, "foreign"]], "queue_pending": []}
        self.assertEqual(self.boot.tick(self.intent["id"])["state"], "runtime_upstream_busy")
        self.backend.queue = {"queue_running": [], "queue_pending": []}
        self.boot = self.make_boot()
        result = self.start()
        self.assertEqual(result["state"], "fleet_running")
        self.assertEqual(self.host.starts, 1)
        self.assertEqual(self.backend.submissions, 0)
        self.boot = self.make_boot()
        with patch("studio_platform.queued_task_runner.read_verification_summary") as evidence:
            result = self.boot.tick(self.intent["id"])
        evidence.assert_not_called()
        self.assertEqual(result["state"], "fleet_recovery_required")
        self.assertFalse(result["generation_verified"])
        self.assertIsNone(self.boot.fleet)
        self.assertFalse(self.boot.children_done())
        self.assertEqual(len(self.workers()), 1)
        self.assertEqual(self.host.starts, 1)

    def test_old_pending_smoke_cannot_be_reclassified_as_runtime_ready(self):
        self.boot = self.make_boot(self.legacy_config)
        self.assertEqual(self.boot.tick(self.intent["id"])["state"], "smoke_running")
        self.boot = self.make_boot()
        with self.assertRaisesRegex(BootError, "profile_change_requires_new_configuration"):
            self.boot.tick(self.intent["id"])
        self.assertEqual(self.backend.submissions, 1)
        self.assertEqual(self.workers(), [])

    def test_queued_receipt_cannot_be_reused_by_old_profile(self):
        self.start()
        self.boot = self.make_boot(self.legacy_config)
        with self.assertRaisesRegex(BootError, "profile_change_requires_new_configuration"):
            self.boot.tick(self.intent["id"])
        self.assertEqual(self.backend.submissions, 0)

    def test_queued_profile_rejects_synthetic_evidence_or_old_extra_receipt(self):
        self.backend.queue = {"queue_running": [[1, "foreign"]], "queue_pending": []}
        self.boot.tick(self.intent["id"])
        receipt = json.loads(self.receipt.read_text())
        for field in ("smoke_submission_started", "smoke_task_id", "evidence"):
            poisoned = {**receipt, field: {"synthetic": True}}
            self.receipt.write_text(json.dumps(poisoned))
            with self.subTest(field=field), self.assertRaisesRegex(BootError, "runtime_receipt_conflict"):
                self.boot.tick(self.intent["id"])
        self.receipt.write_text(json.dumps(receipt))
        directory = self.receipt.parent/"firstlast4-768p-5s-v1"
        directory.mkdir()
        (directory/"state.json").write_text('{"phase":"running"}')
        with self.assertRaisesRegex(BootError, "runtime_receipt_conflict"):
            self.boot.tick(self.intent["id"])
        self.assertEqual(self.workers(), [])

    def test_child_cannot_start_without_bound_runtime_validation(self):
        self.start()
        receipt = json.loads(self.receipt.read_text())
        receipt.pop("runtime_validation")
        self.receipt.write_text(json.dumps(receipt))
        with patch("studio_platform.production_scaler_boot.Repository", return_value=SimpleNamespace(
                engine=self.repo.engine, close=lambda: None, clock=lambda: self.now)), \
                patch("studio_platform.production_scaler_boot.run_slot", side_effect=AssertionError("no runner")):
            with self.assertRaisesRegex(ScalerError, "runtime_validation_required"):
                run_child(self.config, self.intent["id"], self.boot.fleet.config.fingerprint(),
                    SimpleNamespace(database_url=self.repo.engine.url))

    def test_real_completion_scope_is_not_overwritten_by_runtime_readiness(self):
        self.start()
        scopes = [{"job_id": "actual-job", "recipe_id": "h3-base-fl2va-v1"}]
        summary = {"generation_verified": True, "verified_job_scopes": scopes,
            "worker_id": "lium-"+self.intent["id"].replace("-", ""),
            "model_id": self.boot.config.model_id, "runtime_quarantined": False}
        with patch("studio_platform.queued_task_runner.read_verification_summary", return_value=summary):
            result = self.boot.tick(self.intent["id"])
        self.assertTrue(result["generation_verified"])
        self.assertFalse(result["awaiting_real_task"])
        self.assertEqual(result["verified_job_scopes"], scopes)
        self.assertEqual(result["qualification_scope"], "single_host_completed_queued_jobs_only")
        self.assertFalse(json.loads(self.receipt.read_text())["runtime_validation"]["generation_verified"])
        self.assertEqual(self.backend.submissions, 0)

    def test_child_uses_real_queue_runner_with_bound_proof_destination(self):
        self.start()
        from studio_platform.queued_task_runner import QueuedTaskRunner
        from studio_platform.storage import LocalObjectStore
        def fake_slot(fleet, worker_id, settings, **kwargs):
            runner = kwargs["runner_factory"](self.repo, LocalObjectStore(Path(self.temp.name)/"objects"),
                Path(self.temp.name)/"child", backend=self.backend, control=WorkerControl(self.repo))
            self.assertIsInstance(runner, QueuedTaskRunner)
            self.assertEqual(runner.qualification_evidence_file, self.receipt.parent/"queued-task-evidence.json")
            self.assertEqual(runner.evidence_identity, {
                **json.loads(self.receipt.read_text())["identity"], "qualification_profile": QUEUED_TASK_PROFILE})
            return 0
        with patch("studio_platform.production_scaler_boot.Repository", return_value=SimpleNamespace(
                engine=self.repo.engine, close=lambda: None, clock=lambda: self.now)), \
                patch("studio_platform.production_scaler_boot.run_slot", side_effect=fake_slot) as slot:
            self.assertEqual(run_child(self.config, self.intent["id"], self.boot.fleet.config.fingerprint(),
                SimpleNamespace(database_url=self.repo.engine.url)), 0)
        self.assertEqual(slot.call_count, 1)
        self.assertEqual(self.backend.submissions, 0)

    def _assert_real_evidence_quarantines(self, failure):
        self.start()
        # This controller owns the still-running child. A reconstructed
        # controller must stop earlier at the separate fleet-recovery guard.
        children = dict(self.boot.fleet.children)
        with patch("studio_platform.queued_task_runner.read_verification_summary", side_effect=failure) as evidence:
            result = self.boot.tick(self.intent["id"])
        evidence.assert_called_once()
        self.assertEqual(result["state"], "fleet_attention_required")
        self.assertEqual(result["error_code"], "finite_real_task_evidence_unconfirmed")
        self.assertTrue(result["runtime_quarantined"])
        self.assertFalse(result["generation_verified"])
        self.assertTrue(self.boot._stopping)
        self.assertEqual(self.boot.fleet.children, children)
        self.assertFalse(self.boot.children_done())
        for worker_id in children:
            self.assertEqual(WorkerControl(self.repo).get(worker_id)["drain_requested"], 1)
        self.assertEqual(self.host.starts, 1)
        self.assertNotIn("private detail", json.dumps(result))
        self.assertEqual(self.backend.submissions, 0)

    def test_invalid_real_evidence_quarantines_instead_of_silent_wait(self):
        self._assert_real_evidence_quarantines(ValueError("synthetic private detail"))

    def test_unreadable_real_evidence_quarantines_instead_of_silent_wait(self):
        self._assert_real_evidence_quarantines(OSError("synthetic private detail"))

    def test_child_restart_does_not_clear_persisted_drain_when_evidence_write_failed(self):
        self.start()
        worker_id = "lium-"+self.intent["id"].replace("-", "")
        WorkerControl(self.repo).drain(worker_id)
        self.assertFalse((self.receipt.parent/"queued-task-evidence.json").exists())
        with patch("studio_platform.production_scaler_boot.Repository", return_value=SimpleNamespace(
                engine=self.repo.engine, transaction=self.repo.transaction, _locked=self.repo._locked,
                close=lambda: None, clock=lambda: self.now)), \
                patch("studio_platform.production_scaler_boot.run_slot") as slot:
            self.assertEqual(run_child(self.config, self.intent["id"], self.boot.fleet.config.fingerprint(),
                SimpleNamespace(database_url=self.repo.engine.url)), 0)
        slot.assert_not_called()
        self.assertEqual(WorkerControl(self.repo).get(worker_id)["drain_requested"], 1)

    def test_base_legacy_fleet_still_requires_smoke_opt_in(self):
        config = replace(self.boot.config, qualification_profile="", smoke_enabled=True)
        with self.assertRaisesRegex(ValueError, "fleet_requires_successful_smoke"):
            replace(config, smoke_enabled=False)
        with self.assertRaisesRegex(ValueError, "cannot_submit_synthetic_smoke"):
            replace(config, qualification_profile=QUEUED_TASK_PROFILE)
        self.assertEqual(BootConfig.__dataclass_fields__["qualification_profile"].default, "")


if __name__ == "__main__":
    unittest.main()
