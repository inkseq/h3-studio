"""Offline exit-proof regression: lost handles cannot authorize cycle rotation."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.on_demand_scaler import OnDemandController
from studio_platform.production_scaler_boot import ProductionBoot
from test_platform_production_scaler import configuration
from test_platform_repository import LedgerCase


class ControllerRetirementTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.config = configuration(self.root, self.now)
        self.config.work_dir.mkdir()
        self.intent = {"id": str(uuid.uuid4()), "provider_instance_id": str(uuid.uuid4()),
            "pool": self.config.pool, "state": "destroyed", "updated_at": self.now}
        self.provider = Mock()
        self.boot = ProductionBoot(self.repo, self.provider, self.config, self.intent, 19300,
            config_path=self.root/"config.json")
        self.directory = self.boot.config.work_dir/self.intent["id"]
        self.directory.mkdir(parents=True)
        self.worker_id = "lium-"+self.intent["id"].replace("-", "")
        self.controller = object.__new__(OnDemandController)
        self.controller.repo = self.repo
        self.controller.current = SimpleNamespace(config=self.config,
            boots={self.intent["id"]: self.boot}, _managed=lambda: ([self.intent], {}),
            provider_retirement=lambda: None, _capacity_advance_allowed=lambda: True)
        # This fixture owns an already-booted fleet, not unused preparation.
        self.status = {"instances": [self.intent], "all_destroyed": True,
            "active_jobs_truncated": False, "active_job_ids": []}

    def receipt(self, phase, **extra):
        value = {"phase": phase, "identity": {"intent_id": self.intent["id"],
            "configuration_id": self.config.configuration_id}, **extra}
        (self.directory/"bootstrap-state.json").write_text(json.dumps(value))

    def assert_blocked(self):
        before = self.repo.get_budget("owner-budget")
        self.assertFalse(self.boot.children_done())
        self.assertFalse(self.controller._can_rotate(self.status))
        self.assertFalse((self.config.work_dir/"children-retired.json").exists())
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        self.assertEqual(self.provider.mock_calls, [])

    def test_restart_with_saved_launch_and_no_handles_cannot_write_retirement(self):
        for phase in ("fleet_starting", "fleet_started"):
            with self.subTest(phase=phase):
                self.receipt(phase, fleet_recipe_ids=list(self.config.recipe_ids))
                self.assert_blocked()
                self.boot.backend, self.boot.host = Mock(), Mock()
                self.assertFalse(self.boot.close_if_safe(destroyed=True))
                self.boot.backend.close.assert_not_called()
                self.boot.host.close.assert_not_called()

    def test_ambiguous_receipt_keeps_retirement_blocked(self):
        path = self.directory/"bootstrap-state.json"
        for raw in (b"{", b"[]", b'{"phase":"unknown"}', b"x"*(1024*1024+1)):
            with self.subTest(kind=raw[:20]):
                path.write_bytes(raw)
                self.assert_blocked()
        self.receipt("qualified", fleet_recipe_ids=list(self.config.recipe_ids))
        self.assert_blocked()
        self.receipt("qualified", identity={"intent_id": "different-intent",
            "configuration_id": self.config.configuration_id})
        self.assert_blocked()

    def test_missing_boot_receipt_does_not_erase_fleet_or_registration_evidence(self):
        for relative in ("fleet.json", "fleet/fleet-state.json"):
            with self.subTest(relative=relative):
                path = self.directory/relative
                path.parent.mkdir(exist_ok=True)
                path.write_text("{}")
                self.assert_blocked()
                path.unlink()
        self.repo.configure_pool(self.config.pool, max_instances=1, max_physical_gpus=1)
        WorkerControl(self.repo).register(WorkerSpec(self.worker_id, self.config.pool,
            "lium", self.intent["provider_instance_id"], ("GPU-synthetic-retirement",),
            self.config.recipe_ids, self.boot.config.model_id, self.config.configuration_id))
        self.assert_blocked()

    def test_never_launched_boot_allows_cleanup_but_unreadable_state_does_not(self):
        self.assertTrue(self.boot.children_done())
        for phase in ("reserved", "bootstrap_failed", "runtime_ready", "qualified"):
            with self.subTest(phase=phase):
                self.receipt(phase)
                self.assertTrue(self.boot.children_done())
        with patch.object(Path, "open", side_effect=PermissionError("synthetic unreadable receipt")):
            self.assertFalse(self.boot.children_done())

    def fleet(self, children):
        slot = SimpleNamespace(enabled=True, spec=SimpleNamespace(worker_id=self.worker_id))
        self.boot.fleet = SimpleNamespace(config=SimpleNamespace(slots=(slot,)), children=children)

    def test_missing_partial_running_or_unpollable_child_handles_are_not_exit_proof(self):
        for children in ({}, {"another-worker": SimpleNamespace(poll=lambda: 0)},
                {self.worker_id: SimpleNamespace(poll=lambda: None)},
                {self.worker_id: SimpleNamespace(poll=Mock(side_effect=OSError("poll unavailable")))}):
            with self.subTest(children=tuple(children)):
                self.fleet(children)
                self.assert_blocked()

    def test_all_expected_live_handles_prove_exit_and_permit_exact_retirement_receipt(self):
        self.receipt("fleet_started", fleet_recipe_ids=list(self.config.recipe_ids))
        self.fleet({self.worker_id: SimpleNamespace(poll=lambda: 0)})
        self.assertTrue(self.boot.children_done())
        self.assertTrue(self.controller._can_rotate(self.status))
        receipt = json.loads((self.config.work_dir/"children-retired.json").read_text())
        self.assertEqual(receipt, {"config_hash": self.config.fingerprint(),
            "intent_ids": [self.intent["id"]]})
        self.assertEqual(self.provider.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
