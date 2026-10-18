"""Offline contracts for manifest-bound production VRAM admission.

These fake-host tests do not establish H100 inference qualification.
"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import insert, select, update

from studio_platform.lium_bootstrap import BootConfig, BootError
from studio_platform.production_scaler_boot import ProductionBoot
from studio_platform.production_scaler import ScalerError
from studio_platform.repository import registered_workers, scaler_actions
from studio_platform.worker import Outcome
from test_platform_lium_bootstrap import FakeBackend, FakeHost
from test_platform_production_scaler import configuration, FakeProvider, ProductionBootTests
from test_platform_repository import LedgerCase


class ManifestMemoryTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.config = configuration(self.root, self.now)
        self.config.work_dir.mkdir()
        self.repo.configure_pool(self.config.pool, max_instances=2, max_physical_gpus=2)
        self.intent = self.repo.reserve_instance_intent(self.scope, self.config.pool, "memory-boot",
            physical_gpus=1, slots=1, reserved_cost_microusd=2_000_000,
            hard_deadline=self.now+7200, budget_account_ids=["owner-budget"],
            dry_run=False, provider="lium")
        self.repo.update_instance(self.intent["id"], "creating")
        self.repo.update_instance(self.intent["id"], "starting", provider_instance_id=str(uuid.uuid4()))
        self.intent = self.repo.list_instance_intents()[0]
        self.host, self.backend = FakeHost(), FakeBackend()
        self.host.patch = {"runtime": {"gpu_total_bytes": 80*1024**3},
            "gpus": [{"uuid": "GPU-synthetic-h100-memory", "memory_mib": 81920,
                      "name": "NVIDIA H100 80GB HBM3"}]}
        self.provider = FakeProvider(lambda: self.now)
        self.provider.ssh_connection = lambda *args: {"host": "203.0.113.1", "port": 22}

    def filtered(self, *, slot=0, minimum=75*1024):
        manifests = [dict(value) for value in self.config.manifests]
        manifests[slot].update(compatible_gpu_names=["NVIDIA H100 80GB HBM3"],
                              minimum_vram_mib=minimum)
        self.config = replace(self.config, manifests=manifests)

    def action(self, slot=0):
        with self.repo.transaction() as conn:
            conn.execute(insert(scaler_actions).values(intent_id=self.intent["id"],
                pool=self.config.pool, launch_spec=self.config.launches[slot]))

    def boot(self):
        self.boot = ProductionBoot(self.repo, self.provider, self.config, self.intent, 19300,
            config_path=self.root/"config.json", ssh_factory=lambda *args: self.host,
            backend_factory=lambda **kwargs: self.backend,
            verify_smoke=lambda paths, request: {"request": request})
        return self.boot

    def workers(self):
        with self.repo.engine.connect() as conn:
            return list(conn.execute(select(registered_workers.c.id)).scalars())

    def test_legacy_manifest_and_base_default_keep_90_gib_and_reject_80_gib(self):
        boot = self.boot()
        self.assertEqual(BootConfig.__dataclass_fields__["min_gpu_bytes"].default, 90*1024**3)
        self.assertEqual(boot.config.min_gpu_bytes, 90*1024**3)
        with self.assertRaisesRegex(BootError, "gpu_identity_or_memory_mismatch"):
            boot.tick(self.intent["id"])
        self.assertEqual(self.backend.submissions, 0)
        self.assertEqual(self.workers(), [])

    def test_exact_approved_manifest_admits_80_gib_only_to_running_qualification(self):
        self.filtered()
        self.action()
        boot = self.boot()
        self.assertEqual(boot.config.min_gpu_bytes, 75*1024**3)
        result = boot.tick(self.intent["id"])
        self.assertEqual(result["state"], "smoke_running")
        self.assertFalse(result.get("generation_verified", False))
        self.assertEqual(self.backend.submissions, 1)
        self.assertIsNone(boot.fleet)
        self.assertEqual(self.workers(), [])

    def test_other_candidates_lower_threshold_is_not_used_for_this_intent(self):
        self.filtered(slot=1)
        self.action(slot=0)
        self.assertEqual(self.boot().config.min_gpu_bytes, 90*1024**3)
        with self.assertRaisesRegex(BootError, "gpu_identity_or_memory_mismatch"):
            self.boot.tick(self.intent["id"])
        self.assertEqual(self.backend.submissions, 0)

    def test_filtered_configuration_requires_matching_persistent_launch_action(self):
        self.filtered()
        with self.assertRaisesRegex(BootError, "manifest_identity_unconfirmed"):
            self.boot()
        self.action()
        with self.repo.transaction() as conn:
            conn.execute(update(scaler_actions).where(scaler_actions.c.intent_id == self.intent["id"])
                .values(launch_spec={**self.config.launches[0], "offer_id": str(uuid.uuid4())}))
        with self.assertRaisesRegex(BootError, "manifest_identity_unconfirmed"):
            self.boot()
        self.assertEqual(self.backend.submissions, 0)
        self.assertEqual(self.workers(), [])

    def test_actual_memory_below_explicit_threshold_still_rejects_before_smoke(self):
        self.filtered(minimum=81*1024)
        self.action()
        self.boot()
        with self.assertRaisesRegex(BootError, "gpu_identity_or_memory_mismatch"):
            self.boot.tick(self.intent["id"])
        self.assertEqual(self.backend.submissions, 0)
        self.assertEqual(self.workers(), [])

    def test_explicit_manifest_cannot_bypass_existing_30_gib_floor(self):
        with self.assertRaises((BootError, ValueError, ScalerError)):
            self.filtered(minimum=30*1024-1)
            self.action()
            self.boot()
        self.assertEqual(self.backend.submissions, 0)
        self.assertEqual(self.workers(), [])

    def test_lower_approved_threshold_still_uses_real_mib_to_bytes(self):
        self.filtered(minimum=30*1024)
        self.action()
        self.assertEqual(self.boot().config.min_gpu_bytes, 30*1024**3)
        self.assertEqual(self.boot.tick(self.intent["id"])["state"], "smoke_running")
        self.assertEqual(self.workers(), [])

    def test_80_gib_multimodal_worker_requires_fl50_firstlast4_and_ref4_outputs(self):
        self.filtered()
        self.action()
        self.boot()
        # Reuse the existing offline qualification fixture, not real GPU work.
        first, ref = ProductionBootTests.multimodal(self)
        result = self.boot.tick(self.intent["id"])
        self.assertEqual(result["qualification_stage"], first)
        self.assertEqual(self.workers(), [])
        self.stage_outcomes[first] = Outcome("succeeded", "firstlast-task")
        result = self.boot.tick(self.intent["id"])
        self.assertEqual(result["qualification_stage"], ref)
        self.assertEqual(self.workers(), [])
        self.stage_outcomes[ref] = Outcome("succeeded", "reference-task")
        process = SimpleNamespace(pid=4242, poll=lambda: None, send_signal=lambda value: None)
        with patch.object(self.boot, "_popen_impl", return_value=process):
            result = self.boot.tick(self.intent["id"])
        self.assertEqual(result["state"], "fleet_running")
        self.assertEqual(self.backend.submissions, 3)
        self.assertEqual(len(self.workers()), 1)


if __name__ == "__main__":
    unittest.main()
