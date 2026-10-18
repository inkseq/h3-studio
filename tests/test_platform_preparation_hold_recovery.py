"""Synthetic ledger tests only; no provider, GPU, process or host mutations."""
import copy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from sqlalchemy import insert, select, update

from test_platform_preparation_recovery import PreparationRecoveryTests
from studio_platform.on_demand_scaler import OnDemandController, cycle_config
from studio_platform.preparation_hold_recovery import prepare, verify, validate_source_binding
from studio_platform import preparation_hold_recovery as repair_module
from studio_platform.production_scaler import save
from studio_platform.repository import (Conflict, attempts, budget_reservations, capacity_approvals,
    capacity_waiters, jobs, request_hash, scaler_leaders)


class PreparationHoldRepairTests(PreparationRecoveryTests):
    def setup_repair(self, *, continuing=False, wangp=False):
        if continuing:
            self.configure_continuing_service()
        intent = self.hold()
        self.repo.settle_instance_cost(intent["id"], actual_cost_microusd=120000)
        self.intent = intent
        if wangp:
            # Convert only this synthetic fixture before establishing its proof.
            sources = {"wangp-bootstrap.py": "0"*64, "wangp-manifest.json": "1"*64,
                "wangp-runtime.json": "2"*64, "wangp-package.tar.gz": "3"*64}
            self.config = replace(self.config, execution_backend="wangp-worker", engine_manifest_digest="a"*64,
                qualification_profile="queued-task-first-v1", source_sha256=sources)
            self.controller.config = self.config
            self.controller.current.config = cycle_config(self.config, 1)
            self.controller._persist()
            c = self.controller.current.config
            hold_path = c.work_dir/"preparation-hold.json"
            held = json.loads(hold_path.read_text())
            held["config_hash"] = c.fingerprint()
            save(hold_path, held)
            path = c.work_dir/"boot"/intent["id"]/"bootstrap-state.json"
            boot = json.loads(path.read_text())
            boot["identity"].update(backend="wangp-worker", engine_manifest_digest="a"*64)
            save(path, boot)
            with self.repo.transaction() as conn:
                approval = dict(conn.execute(select(capacity_approvals).where(
                    capacity_approvals.c.id == c.capacity_approval_id)).mappings().one())
                payload = {**approval["payload"], "backend": "wangp-worker", "engine_manifest_digest": "a"*64}
                digest = request_hash(payload)
                conn.execute(update(capacity_approvals).where(capacity_approvals.c.id == c.capacity_approval_id)
                    .values(payload=payload, approval_hash=digest))
                job = self.repo._job(conn, self.job["id"])
                conn.execute(update(jobs).values(execution_plan={**job["execution_plan"],
                    "backend": "wangp-worker", "engine_manifest_digest": "a"*64, "capacity_approval_hash": digest}))
                conn.execute(update(capacity_waiters).values(approval_hash=digest))
            # Runtime source names do not affect this test's synthetic boot.
            self.config = replace(self.config, source_sha256=sources)
            self.controller.config = self.config
            self.controller.current.config = cycle_config(self.config, 1)
            self.controller._persist()
            c = self.controller.current.config
            held.update(config_hash=c.fingerprint(), sources=sources)
            save(hold_path, held)
            boot["identity"]["sources"] = sources
            save(path, boot)
        filename = "wangp-bootstrap.py" if wangp else "bootstrap_cloud.py"
        self.target = replace(self.config, source_sha256={**self.config.source_sha256, filename: "f"*64})
        self.proof = {"version": 1, "intent_id": intent["id"], "sequence": 1,
            "job_ids": [self.job["id"]], "target_config_hash": self.target.fingerprint(),
            "reviewed_source_files": [filename], "previous_commit": "1"*40, "target_commit": "2"*40,
            "frozen": {"config_hash": self.config.fingerprint(), "running": True, "paused": True,
                "restart_count": 0, "process_count": 1, "boot_children": 0, "frozen_at": self.now}}

    def stage(self, receipt):
        save(self.config.work_dir/"service-state.json", {"version": 1,
            "config_hash": self.target.fingerprint(), "sequence": receipt["next_sequence"],
            "created_at": self.config.created_at, "transfer_from": receipt["transfer_from"]})
        return {**receipt, "host_stage_confirmed": True}

    def test_dry_run_and_fence_leave_original_job_and_all_money_exact(self):
        self.setup_repair()
        before = self.repo.get_job(self.scope, self.job["id"])
        money = [self.repo.get_budget(x) for x in ("finite-budget", "job-budget")]
        with self.repo.engine.connect() as conn:
            leader = dict(conn.execute(select(scaler_leaders)).mappings().one())
        self.assertEqual(prepare(self.repo, self.config, self.target, self.proof)["phase"], "dry_run")
        with self.repo.engine.connect() as conn:
            self.assertEqual(dict(conn.execute(select(scaler_leaders)).mappings().one()), leader)
        receipt = prepare(self.repo, self.config, self.target, self.proof, apply=True)
        self.assertEqual(self.repo.get_job(self.scope, self.job["id"]), before)
        self.assertEqual([self.repo.get_budget(x) for x in ("finite-budget", "job-budget")], money)
        self.assertTrue(verify(self.repo, self.config, self.target, receipt)["verified"])
        with self.assertRaises(Conflict):
            prepare(self.repo, self.config, self.target, self.proof, apply=True)

    def test_continuing_wangp_repair_accepts_no_cycle_limit_and_exact_engine(self):
        self.setup_repair(continuing=True, wangp=True)
        receipt = prepare(self.repo, self.config, self.target, self.proof, apply=True)
        self.assertIsNone(self.config.max_cycles)
        staged = self.stage(receipt)
        self.assertTrue(verify(self.repo, self.config, self.target, staged, release_leader=True)["leader_released"])
        with self.assertRaises(Conflict):
            verify(self.repo, self.config, self.target, staged, release_leader=True)

    def test_normal_next_cycle_transfers_same_job_without_rebilling_or_new_deadline(self):
        self.setup_repair(continuing=True)
        before = self.repo.get_job(self.scope, self.job["id"])
        money = [self.repo.get_budget(x) for x in ("finite-budget", "job-budget")]
        receipt = prepare(self.repo, self.config, self.target, self.proof, apply=True)
        staged = self.stage(receipt)
        verify(self.repo, self.config, self.target, staged, release_leader=True)
        resumed = OnDemandController(self.repo, self.settings, self.target,
            provider=self.provider, boot_factory=self.controller.boot_factory)
        resumed.initialize()
        after = self.repo.get_job(self.scope, self.job["id"])
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["request"], before["request"])
        self.assertEqual(after["request_hash"], before["request_hash"])
        self.assertEqual(after["attempt_no"], 0)
        self.assertEqual(after["execution_plan"]["capacity_approval_id"], self.config.capacity_approval_id+"-002")
        self.assertEqual([self.repo.get_budget(x) for x in ("finite-budget", "job-budget")], money)
        with self.repo.engine.connect() as conn:
            waiter = dict(conn.execute(select(capacity_waiters).where(capacity_waiters.c.job_id == after["id"])).mappings().one())
        self.assertEqual(waiter["deadline"], receipt["snapshot"]["waiter_deadlines"][after["id"]])
        self.assertEqual(len(self.repo.list_instance_intents(pool=self.config.pool)), 1)

    def test_old_waiter_deadline_and_cancellation_are_not_overridden(self):
        self.setup_repair()
        with self.repo.transaction() as conn:
            conn.execute(update(capacity_waiters).values(deadline=self.now))
        with self.assertRaises(Conflict):
            prepare(self.repo, self.config, self.target, self.proof, apply=True)

    def test_cancelled_job_is_not_restored(self):
        self.setup_repair()
        self.repo.request_cancel(self.scope, self.job["id"])
        with self.assertRaises(Conflict):
            prepare(self.repo, self.config, self.target, self.proof, apply=True)

    def test_changed_budget_or_attempt_or_hold_blocks_activation(self):
        self.setup_repair()
        receipt = prepare(self.repo, self.config, self.target, self.proof, apply=True)
        with self.repo.transaction() as conn:
            conn.execute(update(budget_reservations).where(budget_reservations.c.reference_type == "job")
                .values(state="released", actual_cost_microusd=0))
        with self.assertRaises(Conflict):
            verify(self.repo, self.config, self.target, receipt)

    def test_fleet_marker_or_missing_hold_never_proves_no_execution(self):
        self.setup_repair()
        c = cycle_config(self.config, 1)
        path = c.work_dir/"boot"/self.intent["id"]/"fleet.json"
        path.write_text("{}")
        with self.assertRaises(Conflict):
            prepare(self.repo, self.config, self.target, self.proof, apply=True)

    def test_oversized_receipt_is_rejected_before_committing_fence(self):
        self.setup_repair()
        original = repair_module._snapshot
        def oversized(*args, **kwargs):
            return {**original(*args, **kwargs), 'oversized': 'x'*50000}
        with self.repo.engine.connect() as conn:
            leader = dict(conn.execute(select(scaler_leaders)).mappings().one())
        with patch.object(repair_module, '_snapshot', side_effect=oversized), self.assertRaises(Conflict):
            prepare(self.repo, self.config, self.target, self.proof, apply=True)
        with self.repo.engine.connect() as conn:
            self.assertEqual(dict(conn.execute(select(scaler_leaders)).mappings().one()), leader)

    def test_changed_policy_engine_or_window_refused(self):
        self.setup_repair()
        with self.assertRaises(Conflict):
            prepare(self.repo, self.config, replace(self.target, execution_policy_sha256="e"*64), self.proof)
        with self.assertRaises(Conflict):
            prepare(self.repo, self.config, self.config, self.proof)

    def test_wangp_package_change_requires_exact_runtime_bundle_binding(self):
        self.setup_repair(wangp=True)
        target = replace(self.config, source_sha256={**self.config.source_sha256,
            "wangp-package.tar.gz": "f"*64, "wangp-runtime.json": "e"*64})
        old = {"source_bundle_sha256": "3"*64, "dependency_artifact_sha256": "a"*64, "model": "unchanged"}
        new = {**old, "source_bundle_sha256": "f"*64}
        proof = {"runtime_source_binding": {"previous": old, "target": new,
            "previous_file_sha256": "2"*64, "target_file_sha256": "e"*64}}
        self.assertEqual(len(validate_source_binding(self.config, target, proof)), 2)
        new["dependency_artifact_sha256"] = "b"*64
        with self.assertRaises(Conflict):
            validate_source_binding(self.config, target, proof)


def load_tests(loader, tests, pattern):
    names = [name for name in PreparationHoldRepairTests.__dict__ if name.startswith("test_")]
    return unittest.TestSuite(PreparationHoldRepairTests(name) for name in names)


if __name__ == "__main__":
    unittest.main()
