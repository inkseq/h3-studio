"""Engine-bound cold approval and queued-first evidence; isolated fake runtime only."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import threading
from unittest.mock import patch
import uuid

from sqlalchemy import select, update

from studio_platform.capacity import transfer_unsubmitted_capacity
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.queued_task_runner import _identity
from studio_platform.repository import (Conflict, Scope, capacity_waiters, instance_intents,
    jobs, request_hash)
import test_platform_capacity as capacity_fixture
import test_platform_queued_task_runner as queued_fixture
from test_platform_repository import LedgerCase


DIGEST = "a" * 64


class EngineCapacityTests(capacity_fixture.CapacityTests):
    def use_wangp(self):
        self.value.update(backend="wangp-worker", engine_manifest_digest=DIGEST)
        self.value["envelope"]["controls"].pop("ref_image_size")
        self.write()
        self.settings = replace(self.settings, execution_backend="wangp-worker")
        self.policies = ExecutionPolicies(self.settings, self.repo)
        self.controller.approval_guard = self.policies.capacity_approval_current
        self.controller.activation_guard = self.policies.activation_allowed

    def approve_wangp(self, **kwargs):
        return self.approve(backend="wangp-worker", engine_manifest_digest=DIGEST, **kwargs)

    def test_legacy_payload_hash_unchanged_with_explicit_defaults(self):
        approved = self.approve()
        self.assertNotIn("backend", approved["payload"])
        self.assertNotIn("engine_manifest_digest", approved["payload"])
        again = self.approve(backend="comfy-worker", engine_manifest_digest="")
        self.assertEqual(again["approval_hash"], approved["approval_hash"])
        self.assertEqual(approved["approval_hash"], request_hash(approved["payload"]))

    def test_approval_rejects_missing_malformed_or_unexpected_manifest(self):
        for backend, digest in (("wangp-worker", ""), ("wangp-worker", "bad"),
                ("wangp-worker", None), ("comfy-worker", DIGEST), ("mock", "")):
            with self.subTest(backend=backend, digest=digest), self.assertRaises(ValueError):
                self.approve(backend=backend, engine_manifest_digest=digest)

    def test_current_policy_requires_exact_engine_and_manifest(self):
        self.use_wangp()
        legacy = self.approve()
        self.assertFalse(self.policies.capacity_approval_current(legacy["payload"]))
        self.repo.set_capacity_approval_enabled(legacy["id"], enabled=False)
        approved = self.approve_wangp(approval_id="wangp-grant")
        self.assertTrue(self.policies.capacity_approval_current(approved["payload"]))
        for overrides in ({"backend": "comfy-worker"}, {"engine_manifest_digest": "b" * 64}):
            self.assertFalse(self.policies.capacity_approval_current({**approved["payload"], **overrides}))
        comfy = ExecutionPolicies(replace(self.settings, execution_backend="comfy-worker"), self.repo)
        self.assertFalse(comfy.capacity_approval_current(approved["payload"]))

    def test_native_delivery_cold_approval_requires_exact_exporter_capability(self):
        from studio_platform.inference.outputs import NATIVE_DELIVERY
        self.use_wangp()
        self.value.update(output_delivery=NATIVE_DELIVERY, recipe_ids=["h3-base-fl2va-v1"])
        self.write()
        old = self.approve_wangp()
        self.assertFalse(self.policies.capacity_approval_current(old["payload"]))
        self.assertFalse(self.policies.evaluate(self.compiled, self.scope, self.fingerprint).execution["enabled"])
        self.repo.set_capacity_approval_enabled(old["id"], enabled=False)
        self.value["configuration_id"] = "new-native-config"
        self.launch = replace(self.launch, configuration_id="new-native-config")
        self.write()
        new = self.approve_wangp(approval_id="native-grant", configuration_id="new-native-config",
            output_delivery=NATIVE_DELIVERY)
        self.assertTrue(self.policies.capacity_approval_current(new["payload"]))
        self.assertEqual(new["payload"]["output_delivery"], NATIVE_DELIVERY)
        admission = self.policies.evaluate(self.compiled, self.scope, self.fingerprint)
        self.assertTrue(admission.execution["enabled"], admission.execution)
        self.assertEqual(admission.execution["delivery_spec"]["frame_count"], 124)
        self.assertEqual(admission.execution["admission_state"], "waiting_capacity")
        malformed = {**admission.execution, "output_delivery": ""}
        plan = self.repo.create_plan(self.scope, self.compiled, malformed,
            expires_at=admission.expires_at, estimated_cost_microusd=admission.cost)
        with self.assertRaisesRegex(Conflict, "capacity_plan_approval_mismatch"):
            self.repo.create_job(self.scope, plan["id"], "wrong-exporter", initial_status="waiting_capacity",
                budget_account_ids=admission.execution["budget_account_ids"])

    def test_unregistered_cold_approval_permanently_binds_delivery_identity(self):
        from studio_platform.inference.outputs import NATIVE_DELIVERY
        self.use_wangp()
        old = self.approve_wangp()
        job = self.waiting()
        self.repo.set_capacity_approval_enabled(old["id"], enabled=False)
        self.now = old["expires_at"] + 1
        self.value["qualification"]["expires_at"] = self.now + 5000
        self.value["reservation"]["expires_at"] = self.now + 5000
        self.scale_policy = replace(self.scale_policy, hard_deadline=self.now+5000)
        self.value.update(output_delivery=NATIVE_DELIVERY, recipe_ids=["h3-base-fl2va-v1"])
        self.write()
        with self.assertRaisesRegex(Conflict, "configuration_output_delivery_conflict"):
            self.approve_wangp(approval_id="new-delivery", output_delivery=NATIVE_DELIVERY)
        self.assertFalse(self.policies.evaluate(self.compiled, self.scope, self.fingerprint).execution["enabled"])
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["execution_plan"], job["execution_plan"])
        self.assertEqual(self.provider.creates, [])

    def test_accepted_job_without_worker_or_approval_binds_delivery_identity(self):
        from studio_platform.inference.outputs import NATIVE_DELIVERY
        self.use_wangp()
        execution = {"pool": self.value["pool"], "backend": "wangp-worker", "enabled": True,
            "configuration_id": self.value["configuration_id"], "engine_manifest_digest": DIGEST}
        plan = self.repo.create_plan(self.scope, self.compiled, execution, expires_at=self.now+1000)
        job = self.repo.create_job(self.scope, plan["id"], "warm-accepted")
        with self.assertRaisesRegex(Conflict, "configuration_output_delivery_conflict"):
            self.approve_wangp(approval_id="native-after-job", output_delivery=NATIVE_DELIVERY)
        spec = WorkerSpec("native-worker", self.value["pool"], "test-only", "native-instance", ("native-gpu",),
            tuple(self.value["recipe_ids"]), self.value["model_id"], self.value["configuration_id"],
            "wangp-worker", DIGEST, output_delivery=NATIVE_DELIVERY)
        with self.assertRaisesRegex(Conflict, "configuration_output_delivery_conflict"):
            WorkerControl(self.repo).register(spec)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "queued")

    def test_old_unsubmitted_plan_cannot_cross_a_new_approval_identity(self):
        from studio_platform.inference.outputs import NATIVE_DELIVERY
        self.use_wangp()
        execution = {"pool": self.value["pool"], "backend": "wangp-worker", "enabled": True,
            "configuration_id": self.value["configuration_id"], "engine_manifest_digest": DIGEST}
        plan = self.repo.create_plan(self.scope, self.compiled, execution, expires_at=self.now+1000)
        self.approve_wangp(approval_id="first-native", output_delivery=NATIVE_DELIVERY)
        with self.assertRaisesRegex(Conflict, "configuration_output_delivery_conflict"):
            self.repo.create_job(self.scope, plan["id"], "stale-plan")
        self.assertEqual(self.repo.list_jobs(self.scope), [])

    def test_opposite_first_approvals_share_the_registration_capacity_lock(self):
        from studio_platform.control import require_delivery_configuration
        from studio_platform.inference.outputs import NATIVE_DELIVERY
        self.use_wangp()
        first_checked, release_first, second_checked = threading.Event(), threading.Event(), threading.Event()
        def guarded(connection, backend, configuration_id, delivery):
            require_delivery_configuration(connection, backend, configuration_id, delivery)
            if delivery == "":
                first_checked.set()
                if not release_first.wait(5):
                    raise AssertionError("test did not release the first transaction")
            else:
                second_checked.set()
        def approve(name, delivery):
            try:
                self.approve_wangp(approval_id=name, output_delivery=delivery)
                return "approved"
            except Conflict as exc:
                return str(exc)
        with patch("studio_platform.control.require_delivery_configuration", side_effect=guarded), \
                ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(approve, "parallel-old", "")
            self.assertTrue(first_checked.wait(5))
            second = executor.submit(approve, "parallel-new", NATIVE_DELIVERY)
            try:
                self.assertFalse(second_checked.wait(.2), "second approval read before first binding committed")
            finally:
                release_first.set()
            self.assertEqual(first.result(timeout=5), "approved")
            self.assertEqual(second.result(timeout=5), "configuration_output_delivery_conflict")

    def test_waiter_rejects_forged_plan_engine_and_manifest_without_reservation(self):
        self.use_wangp()
        self.approve_wangp()
        admission = self.policies.evaluate(self.compiled, self.scope, self.fingerprint)
        self.assertTrue(admission.execution["enabled"], admission.execution)
        for overrides in ({"backend": "comfy-worker"}, {"engine_manifest_digest": "b" * 64}):
            plan = self.repo.create_plan(self.scope, self.compiled, {**admission.execution, **overrides},
                expires_at=admission.expires_at, estimated_cost_microusd=admission.cost)
            with self.assertRaisesRegex(Conflict, "capacity_plan_approval_mismatch"):
                self.repo.create_job(self.scope, plan["id"], uuid.uuid4().hex,
                    initial_status="waiting_capacity", budget_account_ids=admission.execution["budget_account_ids"])
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 0)

    def test_only_matching_engine_manifest_worker_activates_original_waiter(self):
        self.use_wangp()
        approval = self.approve_wangp()
        job = self.waiting()
        control = WorkerControl(self.repo)
        for name, backend, digest in (("wrong-engine", "comfy-worker", ""),
                ("wrong-manifest", "wangp-worker", "b" * 64), ("matching", "wangp-worker", DIGEST)):
            spec = WorkerSpec(name, "cold-pool", "test-only", name + "-instance", (name + "-gpu",),
                tuple(self.value["recipe_ids"]), self.value["model_id"], "cold-config",
                backend=backend, engine_manifest_digest=digest)
            control.register(spec)
            control.mark_ready(name, upstream_idle_confirmed=True)
            activated, failed = self.controller._advance(approval)
            self.assertEqual(failed, [])
            self.assertEqual(activated, [job["id"]] if name == "matching" else [])
        current = self.repo.get_job(self.scope, job["id"])
        self.assertEqual((current["status"], current["attempt_no"], current["request_hash"]),
            ("queued", 0, job["request_hash"]))
        self.assertEqual(self.provider.creates, [])

    def test_rollover_explicit_generic_owner_and_tenant_preserves_job_deadline_and_cost(self):
        self.use_wangp()
        owner = "new-creator"
        self.repo.configure_budget("owner:" + owner, tenant_id=self.scope.tenant_id,
            owner_id=owner, limit_microusd=10_000_000)
        scope = Scope(self.scope.tenant_id, owner, self.scope.project_id)
        self.approve_wangp()
        job = self.waiting(scope=scope)
        intent = self.start()
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).where(instance_intents.c.id == intent["id"]).values(state="destroyed"))
            deadline = conn.execute(select(capacity_waiters.c.deadline).where(capacity_waiters.c.job_id == job["id"])).scalar_one()
        self.repo.set_capacity_approval_enabled("cold-approval", enabled=False)
        self.approve_wangp(approval_id="replacement")
        budget = self.repo.get_budget("owner:" + owner)
        for owners in ([], [owner, owner], "new-creator", ["bad owner"]):
            with self.assertRaises(Conflict):
                transfer_unsubmitted_capacity(self.repo, "cold-approval", "replacement",
                    allowed_owners=owners, children_done_confirmed=True)
        transferred = transfer_unsubmitted_capacity(self.repo, "cold-approval", "replacement",
            allowed_owners=[owner], children_done_confirmed=True)
        self.assertEqual(transferred, [job["id"]])
        current = self.repo.get_job(scope, job["id"])
        self.assertEqual(current["execution_plan"]["engine_manifest_digest"], DIGEST)
        self.assertEqual(current["request_hash"], job["request_hash"])
        self.assertEqual(self.repo.get_budget("owner:" + owner), budget)
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(capacity_waiters.c.deadline).where(
                capacity_waiters.c.job_id == job["id"])).scalar_one(), deadline)

    def test_rollover_cannot_move_accepted_jobs_to_different_engine_manifest(self):
        self.use_wangp()
        self.approve_wangp()
        job = self.waiting()
        intent = self.start()
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).where(instance_intents.c.id == intent["id"]).values(state="destroyed"))
        self.repo.set_capacity_approval_enabled("cold-approval", enabled=False)
        before = self.repo.get_job(self.scope, job["id"])
        for name, backend, digest in (("changed-engine", "comfy-worker", ""),
                ("changed-manifest", "wangp-worker", "b" * 64)):
            self.approve(approval_id=name, backend=backend, engine_manifest_digest=digest)
            with self.assertRaisesRegex(Conflict, "capacity_transfer_grant_identity_mismatch"):
                transfer_unsubmitted_capacity(self.repo, "cold-approval", name,
                    allowed_owners=[self.scope.owner_id], children_done_confirmed=True)
            self.assertEqual(self.repo.get_job(self.scope, job["id"]), before)


class WanGPQueuedEvidenceTests(LedgerCase):
    runner = queued_fixture.QueuedTaskRunnerTests.runner

    def setUp(self):
        super().setUp()
        self.work = Path(self.temp.name) / "worker"
        self.evidence_file = Path(self.temp.name) / "boot" / "queued-task-evidence.json"
        self.identity = {"intent_id": "synthetic-intent", "instance_id": "synthetic-instance",
            "configuration_id": "synthetic-config", "qualification_profile": queued_fixture.QUEUED_TASK_PROFILE,
            "sources": {"model_manifest.json": "b" * 64},
            "backend": "wangp-worker", "engine_manifest_digest": DIGEST}
        self.backend = queued_fixture.Backend()
        self.backend.kind = "wangp-worker"
        self.backend.manifest = SimpleNamespace(digest=DIGEST)
        self.control = WorkerControl(self.repo)
        self.control.register(WorkerSpec("worker", "finite-pool", "synthetic-provider", "synthetic-instance",
            ("synthetic-device",), ("synthetic-fl", "synthetic-ref"), "synthetic-model", "synthetic-config",
            backend="wangp-worker", engine_manifest_digest=DIGEST))
        self.control.mark_ready("worker", upstream_idle_confirmed=True)

    def job_for(self, *, recipe="synthetic-fl", steps=4):
        request = {"recipe_id": recipe, "request": {"model": "synthetic-model", "prompt": "PRIVATE-PROMPT",
            "mode": "fl", "duration": 5, "resolution": "480P", "steps": steps, "generate_audio": False},
            "assets": {"private-asset": {"metadata": {"kind": "image"}}}}
        plan = self.repo.create_plan(self.scope, request, {"pool": "finite-pool", "backend": "wangp-worker",
            "engine_manifest_digest": DIGEST, "enabled": True,
            "configuration_id": "synthetic-config", "expected_runtime_s": 10},
            expires_at=self.now + 3600, estimated_cost_microusd=100_000)
        job = self.repo.create_job(self.scope, plan["id"], uuid.uuid4().hex, budget_account_ids=("owner-budget",))
        self.now += .1
        return job

    def test_first_real_job_delivers_and_verifies_without_extra_smoke_job(self):
        queued_fixture.QueuedTaskRunnerTests.test_first_real_job_is_result_and_exact_scope_proof_one_submission(self)
        value = json.loads(self.evidence_file.read_text())
        self.assertEqual(value["identity"], self.identity)

    def test_unknown_retains_attempt_and_no_duplicate_on_recovery(self):
        queued_fixture.QueuedTaskRunnerTests.test_unknown_post_only_reconciles_original_attempt_and_can_return_success(self)

    def test_collection_retry_never_regenerates(self):
        queued_fixture.QueuedTaskRunnerTests.test_collection_failure_keeps_same_attempt_and_recovers_output_without_resubmit(self)

    def test_identity_rejects_missing_digest_wrong_backend_and_runtime(self):
        for overrides in ({"engine_manifest_digest": ""}, {"backend": "comfy-worker"}, {"backend": "mock"}):
            with self.assertRaises(ValueError):
                _identity({**self.identity, **overrides})
        self.backend.manifest = SimpleNamespace(digest="c" * 64)
        with self.assertRaisesRegex(ValueError, "backend_manifest_conflict"):
            self.runner()

    def test_registered_spec_must_match_evidence_manifest_before_claim(self):
        self.identity["engine_manifest_digest"] = "c" * 64
        self.backend.manifest = SimpleNamespace(digest="c" * 64)
        job = self.job_for()
        with self.assertRaisesRegex(ValueError, "worker_identity_conflict"):
            self.runner().run_once("worker", "finite-pool")
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 0)
        self.assertEqual(self.backend.submits, 0)

    def test_wrong_job_manifest_is_never_claimed_or_qualified(self):
        job = self.job_for()
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(execution_plan={
                **job["execution_plan"], "engine_manifest_digest": "c" * 64}))
        self.assertEqual(self.runner().run_once("worker", "finite-pool")["state"], "idle")
        self.assertFalse(self.evidence_file.exists())
        self.assertEqual(self.backend.submits, 0)


def load_tests(loader, tests, pattern):
    # Reuse fixture helpers without rerunning their inherited legacy test cases.
    return unittest.TestSuite(cls(name) for cls in (EngineCapacityTests, WanGPQueuedEvidenceTests)
        for name in cls.__dict__ if name.startswith("test_"))


if __name__ == "__main__":
    unittest.main()
