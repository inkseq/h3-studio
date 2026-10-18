"""Offline handoff, deadline and drain tests; never connect/rent/stop a GPU."""
from dataclasses import asdict, replace
import contextlib
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import update

import test_platform_repository as ledger
from studio_platform.control import WorkerControl
from studio_platform.lium_bootstrap import COMFY_REVISION, MODEL_REVISION
from studio_platform.production_worker import (AcceptanceConfig, AcceptanceError, AcceptanceRunner,
    ProductionController, ledger_status, main, read_config, request_drain, status, verify_report)
from studio_platform.repository import registered_workers


class FakeHost:
    def __init__(self, config):
        self.config, self.closed, self.reserves, self.tunnels = config, False, 0, 0
        self.idle = True

    def report(self):
        return {"identity": self.config.boot_identity, "state": "ready", "model_revision": MODEL_REVISION,
            "comfyui_revision": COMFY_REVISION, "actual_comfy_revision": COMFY_REVISION,
            "gpus": [{"uuid": self.config.gpu_uuid}], "runtime": {"gpu_total_bytes": 96*1024**3},
            "files": {str(i): {"state": "verified_size", "size_bytes": 100, "revision": MODEL_REVISION} for i in range(5)}}

    def run(self, script):
        if "urllib.request" in script:
            return {"identity": self.config.identity(), "idle": self.idle}
        self.reserves += 1
        return {"reserved": True}

    def open_tunnel(self, port):
        self.tunnels += 1

    def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self):
        self.queue = {"queue_running": [], "queue_pending": []}

    def _json(self, *args):
        return self.queue

    def close(self):
        pass


class FakeFleet:
    def __init__(self, config, repo, path, **kwargs):
        self.config, self.control = config, WorkerControl(repo)
        self.worker = config.slots[0].spec.worker_id

    def start(self):
        self.control.register(self.config.slots[0].spec)
        self.control.mark_ready(self.worker, upstream_idle_confirmed=True)

    def tick(self):
        return {"children": [{"worker_id": self.worker, "state": "running"}]}

    def drain(self):
        self.control.drain(self.worker)


class ProductionWorkerTests(ledger.LedgerCase):
    def setUp(self):
        super().setUp()
        root = Path(self.temp.name)
        self.config = AcceptanceConfig(1, True, "handoff-test", root/"worker", root/"key", root/"known_hosts",
            "8.8.8.8", 2022, 18881,
            {"intent_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "instance_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
             "configuration_id": "qualified-bf16", "sources": {"bootstrap_cloud.py": "a"*64, "model_manifest.json": "b"*64}},
            "GPU-01234567-0123-0123-0123-012345678901", "production-test", "acceptance-test",
            self.now+7200, 300, "real-qualified-evidence")
        self.config.ssh_key_file.touch()
        self.config.ssh_key_file.chmod(0o600)
        self.config.known_hosts_file.touch()
        self.path = root/"worker.json"
        self.path.write_text(json.dumps(asdict(self.config), default=str))
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.host, self.backend = FakeHost(self.config), FakeBackend()

    def controller(self):
        return ProductionController(self.repo, self.config, self.path, host_factory=lambda *a: self.host,
            backend_factory=lambda **kw: self.backend, fleet_factory=FakeFleet)

    def test_disabled_validates_without_database_ssh_or_credential_lookup(self):
        output = io.StringIO()
        with patch("studio_platform.production_worker.Settings.from_environment", side_effect=AssertionError("must not load runtime")), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--config", str(self.path)]), 0)
        self.assertEqual(json.loads(output.getvalue())["phase"], "disabled")
        self.assertFalse(self.config.work_dir.exists())
        self.assertFalse(read_config(self.path).trust_first_host_key)
        with self.assertRaises(AcceptanceError):
            replace(self.config, host="127.0.0.1")

    def test_identity_and_empty_queue_checked_before_unique_handoff(self):
        report = copy.deepcopy(self.host.report())
        report["gpus"][0]["uuid"] = "GPU-unexpected"
        with self.assertRaises(AcceptanceError):
            verify_report(self.config, report)
        self.backend.queue["queue_running"] = ["old-controller-task"]
        with self.assertRaises(AcceptanceError):
            self.controller().start()
        self.assertEqual(self.host.reserves, 0)
        self.backend.queue["queue_running"] = []
        controller = self.controller()
        controller.start()
        self.assertEqual(self.host.reserves, 1)
        self.assertEqual(controller.tick()["phase"], "running")
        with self.assertRaisesRegex(AcceptanceError, "recovery_required"):
            self.controller().start()
        self.assertEqual(self.host.reserves, 1)

    def test_drain_is_durable_and_status_rechecks_ledger_and_current_upstream(self):
        controller = self.controller()
        controller.start()
        controller.tick()
        self.assertFalse(status(self.repo, self.config, host_factory=lambda *a: self.host)["drained"])
        request_drain(self.repo, self.config)
        self.assertEqual(WorkerControl(self.repo).get(self.config.worker_id)["drain_requested"], 1)
        # Last parent snapshot still says running; independent status derives
        # drain state from the durable flag and live worker row, not that cache.
        independent = status(self.repo, self.config, host_factory=lambda *a: self.host)
        self.assertTrue(independent["drain_requested"])
        self.assertTrue(independent["drained"])
        self.assertTrue(controller.tick()["drained"])
        # A fresh independent status command can work after parent exit, but
        # never trusts the saved drained flag on its own.
        self.assertTrue(status(self.repo, self.config, host_factory=lambda *a: self.host)["drained"])
        self.host.idle = False
        self.assertFalse(status(self.repo, self.config, host_factory=lambda *a: self.host)["drained"])
        self.host.idle = True
        with self.repo.engine.begin() as conn:
            conn.execute(update(registered_workers).where(registered_workers.c.id == self.config.worker_id)
                .values(current_job_id="unresolved-task"))
        result = status(self.repo, self.config, host_factory=lambda *a: self.host)
        self.assertFalse(result["drained"])
        self.assertEqual(result["active_job_ids"], ["unresolved-task"])
        with self.repo.engine.begin() as conn:
            conn.execute(update(registered_workers).where(registered_workers.c.id == self.config.worker_id)
                .values(current_job_id=None, expires_at=self.now-1))
        self.assertFalse(ledger_status(self.repo, self.config)["ledger_safe"])
        with self.assertRaises(AcceptanceError):
            request_drain(self.repo, self.config)

    def test_deadline_blocks_new_submission_but_keeps_existing_reconciliation(self):
        control = SimpleNamespace(current="in-flight", drains=0, submission_allowed=lambda job: True)
        def drain(worker):
            control.drains += 1
        control.drain, control.get = drain, lambda worker: {"current_job_id": control.current}
        runner = AcceptanceRunner(self.repo, None, self.config.work_dir, acceptance=self.config,
            backend=SimpleNamespace(enabled=True, kind="comfy-worker"), control=control, submission_guard=lambda job: True)
        job = {"tenant_id": "sixnine", "owner_id": "superdan", "expected_runtime_s": 100}
        self.assertTrue(runner._submission_allowed(job))
        self.assertFalse(runner._submission_allowed({**job, "owner_id": "supervan"}))
        self.assertFalse(runner._submission_allowed({**job, "expected_runtime_s": 7200}))
        self.now = self.config.stop_claiming_at
        self.assertFalse(runner._submission_allowed(job))
        turns = []
        def reconcile(worker, pool):
            turns.append(control.current)
            control.current = None
        runner.run_once = reconcile
        with patch("studio_platform.production_worker.time.sleep"):
            runner.run_forever(self.config.worker_id, self.config.pool)
        self.assertEqual(turns, ["in-flight"])
        self.assertGreater(control.drains, 0)
        self.assertFalse(runner._drain.is_set())

    def test_fleet_child_uses_acceptance_runner_and_rejects_command_drift(self):
        import sys
        controller = self.controller()
        controller.start()
        command = [sys.executable, "-m", "studio_platform.fleet", "--config", str(self.config.work_dir/"fleet.json"),
            "--slot", self.config.worker_id, "--config-hash", controller.fleet.config.fingerprint()]
        with patch("studio_platform.production_worker.subprocess.Popen") as spawn:
            controller._popen(command, stdout=-3, stderr=-3)
            actual = spawn.call_args.args[0]
            self.assertEqual(actual[:3], [sys.executable, "-m", "studio_platform.production_worker"])
            self.assertIn("--enabled", actual)
            self.assertIn("--slot", actual)
            self.assertEqual(actual[-1], controller.fleet.config.fingerprint())
            with self.assertRaises(AcceptanceError):
                controller._popen(["unexpected", *command[1:]])
            self.assertEqual(spawn.call_count, 1)


if __name__ == "__main__":
    unittest.main()
