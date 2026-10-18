"""Failed preparation needs positive process/socket evidence before removal."""
from pathlib import Path
import json
import unittest
import uuid

from studio_platform.lium_bootstrap import BootError
from studio_platform.production_scaler_boot import ProductionBoot
from test_platform_lium_bootstrap import FakeHost
from test_platform_production_scaler import configuration, FakeProvider
from test_platform_repository import LedgerCase


class PreparationIdleTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.config = configuration(Path(self.temp.name), self.now)
        self.config.work_dir.mkdir()
        self.repo.configure_pool(self.config.pool, max_instances=2, max_physical_gpus=2)
        intent = self.repo.reserve_instance_intent(self.scope, self.config.pool, "preparation-idle",
            physical_gpus=1, slots=1, reserved_cost_microusd=2_000_000,
            hard_deadline=self.now+7200, budget_account_ids=["owner-budget"],
            dry_run=False, provider="lium")
        self.repo.update_instance(intent["id"], "creating")
        self.pod = str(uuid.uuid4())
        self.repo.update_instance(intent["id"], "starting", provider_instance_id=self.pod)
        self.intent = self.repo.list_instance_intents()[0]
        self.host = FakeHost()
        self.host.patch = {"state": "failed"}
        self.provider = FakeProvider(lambda: self.now)
        self.provider.ssh_connection = lambda *args: {"host": "203.0.113.1", "port": 22}
        self.boot = ProductionBoot(self.repo, self.provider, self.config, self.intent, 19300,
            config_path=Path(self.temp.name)/"config.json", ssh_factory=lambda *args: self.host)
        self.assertEqual(self.boot.tick(self.intent["id"])["state"], "bootstrap_failed")
        self.proof = {"identity": self.host.identity, "state": "failed",
            "process_visibility_complete": True, "bootstrap_process_count": 0,
            "comfy_process_count": 0, "comfy_port_listening": False,
            "observed_at": self.now}
        self.host.preparation_idle_report = lambda: dict(self.proof)

    def probe(self):
        return self.boot.idle_probe(self.intent["id"], self.pod)

    def test_positive_failed_process_and_socket_proof_retains_idle_period(self):
        first = self.probe()
        self.assertTrue(first.idle)
        self.now += 61
        self.assertEqual(self.probe().idle_since, first.idle_since)
        self.assertIsNone(self.boot.backend)
        self.assertEqual(self.host.starts, 1)

    def test_incomplete_identity_process_socket_or_state_never_proves_idle(self):
        changes = ({"identity": {}}, {"state": "preparing"},
            {"process_visibility_complete": False}, {"bootstrap_process_count": 1},
            {"comfy_process_count": 1}, {"comfy_port_listening": True},
            {"bootstrap_process_count": False}, {"comfy_process_count": None})
        original = dict(self.proof)
        for change in changes:
            with self.subTest(change=change):
                self.proof = {**original, **change}
                self.assertFalse(self.probe().idle)

    def test_any_qualification_submission_still_requires_reconciliation(self):
        receipt = self.boot.config.work_dir/self.intent["id"]/"bootstrap-state.json"
        state = json.loads(receipt.read_text())
        state["smoke_submission_started"] = self.now
        receipt.write_text(json.dumps(state))
        with self.assertRaises(BootError):
            self.probe()

    def test_changed_pod_or_source_identity_is_rejected_without_remote_relaunch(self):
        with self.assertRaises(BootError):
            self.boot.idle_probe(self.intent["id"], str(uuid.uuid4()))
        receipt = self.boot.config.work_dir/self.intent["id"]/"bootstrap-state.json"
        state = json.loads(receipt.read_text())
        state["identity"]["sources"] = {}
        receipt.write_text(json.dumps(state))
        with self.assertRaises(BootError):
            self.probe()
        self.assertEqual(self.host.starts, 1)


if __name__ == "__main__":
    unittest.main()
