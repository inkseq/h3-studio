"""E1 pool admission only: fake identities, SQLite / explicitly isolated PG."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import copy
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select, update

from studio_platform.capacity import ColdStartCoordinator
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.autoscale import ScalePolicy
from studio_platform.capabilities import MODEL, compile_request
from studio_platform.scaler import LaunchSpec, ScaleCoordinator
from studio_platform.settings import Settings
from studio_platform.repository import (BudgetExceeded, Conflict, Scope, attempts,
    capacity_cycles, capacity_pool_members, capacity_waiters, instance_intents)
from test_platform_repository import LedgerCase
from test_platform_api import generation_request
from test_platform_execution_policy import policy
from test_platform_scaler import FakeProvider
import test_platform_capacity as fixture


DIGEST = "a" * 64
MEMBERS = {"version": 1, "member_ids": ["a", "b"]}


class PoolMemberTests(LedgerCase):
    write = fixture.CapacityTests.write
    approve = fixture.CapacityTests.approve

    def setUp(self):
        super().setUp()
        self.value = policy(self.now)
        self.value.update(pool="cold-pool", configuration_id="cold-config", budget_accounts=["owner:{owner_id}"])
        self.path = Path(self.temp.name)/"synthetic-policy.json"
        self.write()
        self.settings = Settings(Path(self.temp.name)/"data", generation_enabled=True,
            execution_backend="comfy-worker", execution_policy_file=self.path)
        self.compiled, self.fingerprint = compile_request(generation_request(), lambda _: None)
        for owner in ("superdan", "supervan"):
            self.repo.configure_budget("owner:"+owner, tenant_id=self.scope.tenant_id, owner_id=owner, limit_microusd=10_000_000)
        self.repo.configure_budget("capacity-budget", tenant_id=self.scope.tenant_id, limit_microusd=10_000_000)
        self.billing_scope = Scope(self.scope.tenant_id, "operator", "platform-capacity")
        self.launch = LaunchSpec("test-only", "cold-config", MODEL)
        self.scale_policy = ScalePolicy(dry_run=False, max_instances=2, max_physical_gpus=2,
            new_instance_physical_gpus=1, new_instance_slots=1, cold_start_s=30,
            queue_target_s=60, min_improvement_s=1, cooldown_s=0, idle_before_drain_s=900,
            approved_remaining_microusd=10_000_000, instance_reservation_microusd=200_000, hard_deadline=self.now+3600)
        self.provider = FakeProvider()
        self.scaler = ScaleCoordinator(self.repo, provider=self.provider, enabled=True)
        self.controller = ColdStartCoordinator(self.repo, scaler=self.scaler, enabled=True)
        self.value.update(backend="wangp-worker", engine_manifest_digest=DIGEST)
        self.value["envelope"]["controls"].pop("ref_image_size")
        self.write()
        self.settings = replace(self.settings, execution_backend="wangp-worker")
        self.policies = ExecutionPolicies(self.settings, self.repo)
        self.controller.approval_guard = self.policies.capacity_approval_current
        self.controller.activation_guard = self.policies.activation_allowed
        self.scale_policy = replace(self.scale_policy, max_instances=2,
            max_physical_gpus=2, new_instance_physical_gpus=1)
        self.repo.configure_pool("cold-pool", max_instances=2, max_physical_gpus=2)
        self.grant = self.approve(backend="wangp-worker", engine_manifest_digest=DIGEST, pool_members=MEMBERS)
        self.admission = self.policies.evaluate(self.compiled, self.scope, self.fingerprint)
        self.assertTrue(self.admission.execution["enabled"], self.admission.execution)
        self.assertEqual(self.admission.execution["capacity_binding"], "pool-members-v1")

    def waiter(self, key="job", scope=None):
        scope = scope or self.scope
        execution = copy.deepcopy(self.admission.execution)
        execution["budget_account_ids"] = ["owner:"+scope.owner_id]
        plan = self.repo.create_plan(scope, self.compiled, execution,
            expires_at=self.admission.expires_at, estimated_cost_microusd=self.admission.cost)
        return self.repo.create_job(scope, plan["id"], key, initial_status="waiting_capacity",
            budget_account_ids=execution["budget_account_ids"])

    def member(self, name):
        value = self.repo.reserve_capacity_member("cold-approval", name)
        self.repo.update_instance(value["id"], "creating")
        return self.repo.update_instance(value["id"], "starting", provider_instance_id="node-"+name)

    def worker(self, name, *, instance=None, digest=DIGEST, config="cold-config", backend="wangp-worker"):
        control = WorkerControl(self.repo)
        spec = WorkerSpec("worker-"+name, "cold-pool", "test-only", instance or "node-"+name,
            ("gpu-"+name,), tuple(self.value["recipe_ids"]), self.value["model_id"], config,
            backend, digest if backend == "wangp-worker" else "")
        control.register(spec)
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        return control, spec

    def rows(self, table):
        with self.repo.engine.connect() as conn:
            return list(conn.execute(select(table)).mappings())

    def test_pool_opt_in_is_immutable_and_legacy_hash_and_controller_remain_unchanged(self):
        self.assertEqual(self.controller.tick("leader", "cold-approval"),
            {"state": "capacity_pool_controller_not_implemented"})
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.rows(instance_intents), [])
        with self.assertRaisesRegex(Conflict, "immutable"):
            self.approve(backend="wangp-worker", engine_manifest_digest=DIGEST)
        legacy = self.approve(approval_id="legacy")
        again = self.approve(approval_id="legacy", pool_members=None)
        self.assertEqual(again["approval_hash"], legacy["approval_hash"])
        self.assertNotIn("pool_members", legacy["payload"])
        with self.assertRaisesRegex(Conflict, "not_approved"):
            self.repo.reserve_capacity_member("legacy", "a")
        for value in ({"version": True, "member_ids": ["a", "b"]},
                {"version": 2, "member_ids": ["a", "b"]},
                {"version": 1, "member_ids": ["a", "a"]},
                {"version": 1, "member_ids": ["a", "b", "c"]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.approve(approval_id="bad", pool_members=value)
        with self.assertRaisesRegex(ValueError, "pool_budget_not_approved"):
            self.approve(approval_id="underfunded", pool_members=MEMBERS,
                scale_policy=replace(self.scale_policy, approved_remaining_microusd=300_000))

    def test_finite_controller_rejects_opt_in_even_when_all_legacy_identity_checks_match(self):
        from studio_platform.production_scaler import FiniteController
        p = self.grant["payload"]
        owner = SimpleNamespace(config=SimpleNamespace(tenant=p["tenant_id"], pool=p["pool"],
            configuration_id=p["configuration_id"], recipe_ids=p["recipe_ids"], execution_policy_sha256=p["policy_hash"],
            scope=Scope(**p["budget_scope"]), budget_account_ids=p["budget_account_ids"],
            scale_policy=p["scale_policy"], launches=[p["launch"]]), stopping=lambda: False,
            policies=SimpleNamespace(capacity_approval_current=lambda _: True))
        self.assertTrue(FiniteController.approval_current(owner, {k:v for k,v in p.items() if k != "pool_members"}))
        self.assertFalse(FiniteController.approval_current(owner, p))

    def test_warm_admission_cannot_drop_pool_binding_even_after_revocation(self):
        self.member("a")
        self.worker("a")
        for enabled in (True, False):
            self.repo.set_capacity_approval_enabled("cold-approval", enabled=enabled)
            result = self.policies.evaluate(self.compiled, self.scope, self.fingerprint)
            self.assertFalse(result.execution["enabled"])
            self.assertEqual(result.execution["admission_state"], "blocked")
            self.assertTrue(any("双节点" in b for b in result.execution["blockers"]))

    def test_concurrent_reservations_bind_each_member_once_and_never_double_charge(self):
        results = self.parallel(lambda n: self.repo.reserve_capacity_member("cold-approval", "ab"[n % 2]))
        self.assertEqual(sum(row["created"] for row in results), 2)
        self.assertEqual(len({row["id"] for row in results}), 2)
        self.assertEqual(len(self.rows(capacity_pool_members)), 2)
        self.assertEqual(len(self.rows(instance_intents)), 2)
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 400_000)
        with self.assertRaisesRegex(Conflict, "not_approved"):
            self.repo.reserve_capacity_member("cold-approval", "c")
        self.assertEqual(self.provider.creates, [])

    def test_member_binding_and_existing_ledger_reservation_rollback_together(self):
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.repo.transaction() as conn:
                self.repo.reserve_capacity_member("cold-approval", "a", connection=conn)
                raise RuntimeError("rollback")
        self.assertEqual(self.rows(capacity_pool_members), [])
        self.assertEqual(self.rows(instance_intents), [])
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 0)
        self.repo.configure_budget("capacity-budget", tenant_id=self.scope.tenant_id, limit_microusd=300_000)
        self.repo.reserve_capacity_member("cold-approval", "a")
        with self.assertRaises(BudgetExceeded):
            self.repo.reserve_capacity_member("cold-approval", "b")
        self.assertEqual(len(self.rows(capacity_pool_members)), 1)

    def test_second_member_ready_first_activates_original_without_rebinding_request(self):
        original = self.waiter()
        first, second = self.member("a"), self.member("b")
        self.worker("b")
        self.assertEqual(self.controller.advance_once("cold-approval")["activated"], 1)
        after = self.repo.get_job(self.scope, original["id"])
        for field in ("id", "request", "request_hash", "execution_plan", "estimated_cost_microusd"):
            self.assertEqual(after[field], original[field])
        self.assertEqual(after["status"], "queued")
        waiter = self.rows(capacity_waiters)[0]
        self.assertIsNone(waiter["intent_id"])
        self.assertEqual(waiter["state"], "activated")
        self.assertEqual(self.rows(capacity_cycles), [])
        self.assertEqual({r["id"] for r in self.rows(instance_intents)}, {first["id"], second["id"]})

    def test_wrong_or_unbound_worker_cannot_activate_or_claim_pool_work(self):
        job = self.waiter()
        self.member("a")
        self.worker("wrong-manifest", instance="node-a", digest="b"*64)
        rogue, rogue_spec = self.worker("unbound")
        self.assertEqual(self.controller.advance_once("cold-approval")["activated"], 0)
        self.member("b")
        valid, spec = self.worker("b")
        self.assertEqual(self.controller.advance_once("cold-approval")["activated"], 1)
        self.assertIsNone(rogue.claim(rogue_spec.worker_id, "cold-pool"))
        claimed = valid.claim(spec.worker_id, "cold-pool")
        self.assertEqual(claimed.job["id"], job["id"])

    def test_two_ready_workers_race_for_one_job_without_duplicate_attempt(self):
        original = self.waiter()
        self.member("a"), self.member("b")
        control, a = self.worker("a")
        _, b = self.worker("b")
        self.controller.advance_once("cold-approval")
        barrier = threading.Barrier(2)
        def claim(worker):
            barrier.wait(timeout=5)
            return control.claim(worker, "cold-pool")
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, [a.worker_id, b.worker_id]))
        self.assertEqual(sum(c is not None for c in claims), 1)
        self.assertEqual(len(self.rows(attempts)), 1)
        after = self.repo.get_job(self.scope, original["id"])
        self.assertEqual(after["attempt_no"], 1)
        self.assertEqual(after["estimated_cost_microusd"], original["estimated_cost_microusd"])

    def test_unknown_attempt_stays_on_member_a_while_b_serves_different_work(self):
        one, two = self.waiter("one"), self.waiter("two", self.other)
        self.member("a"), self.member("b")
        control, a = self.worker("a")
        _, b = self.worker("b")
        self.controller.advance_once("cold-approval")
        claim = control.claim(a.worker_id, "cold-pool")
        control.queue.begin_submission(claim.lease)
        control.queue.mark_submission_unknown(claim.lease)
        control.observe(a.worker_id, claim.job["id"])
        other = control.claim(b.worker_id, "cold-pool")
        self.assertIsNotNone(other)
        self.assertNotEqual(other.job["id"], claim.job["id"])
        self.assertEqual(control.get(a.worker_id)["state"], "unknown")
        self.assertEqual(control.get(a.worker_id)["current_job_id"], claim.job["id"])
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 400_000)
        self.assertEqual(len(self.rows(attempts)), 2)
        self.repo.set_capacity_approval_enabled("cold-approval", enabled=False)
        recovered = control.claim(a.worker_id, "cold-pool", purpose="reconcile")
        self.assertEqual(recovered.lease.attempt_id, claim.lease.attempt_id)
        self.assertEqual(recovered.job["id"], claim.job["id"])

    def test_revocation_after_activation_blocks_new_claim_without_rerouting_job(self):
        original = self.waiter()
        self.member("b")
        control, spec = self.worker("b")
        self.controller.advance_once("cold-approval")
        self.repo.set_capacity_approval_enabled("cold-approval", enabled=False)
        self.assertIsNone(control.claim(spec.worker_id, "cold-pool"))
        self.assertEqual(self.rows(attempts), [])
        self.assertEqual(self.repo.get_job(self.scope, original["id"])["execution_plan"], original["execution_plan"])

    def test_destroyed_member_cannot_be_rebound_or_cause_the_other_waiter_to_fail(self):
        original = self.waiter()
        a, b = self.member("a"), self.member("b")
        self.repo.update_instance(a["id"], "destroying")
        self.repo.update_instance(a["id"], "destroyed", destruction_confirmed=True)
        again = self.repo.reserve_capacity_member("cold-approval", "a")
        self.assertFalse(again["created"])
        self.assertEqual(again["id"], a["id"])
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 400_000)
        self.worker("b")
        self.assertEqual(self.controller.advance_once("cold-approval")["activated"], 1)
        self.assertEqual(self.repo.get_job(self.scope, original["id"])["status"], "queued")
        self.assertEqual(len(self.rows(instance_intents)), 2)

    def test_first_member_removal_does_not_exhaust_unreserved_second_member(self):
        original = self.waiter()
        a = self.member("a")
        self.repo.update_instance(a["id"], "destroying")
        self.repo.update_instance(a["id"], "destroyed", destruction_confirmed=True)
        self.assertEqual(self.controller.advance_once("cold-approval")["failed"], 0)
        b = self.member("b")
        self.assertNotEqual(b["id"], a["id"])
        self.worker("b")
        self.assertEqual(self.controller.advance_once("cold-approval")["activated"], 1)
        self.assertEqual(self.repo.get_job(self.scope, original["id"])["status"], "queued")
        self.repo.update_instance(b["id"], "destroying")
        self.repo.update_instance(b["id"], "destroyed", destruction_confirmed=True)
        for member in ("a", "b"):
            with self.assertRaisesRegex(Conflict, "pool_members_unavailable"):
                self.repo.reserve_capacity_member("cold-approval", member)
        self.assertEqual(len(self.rows(instance_intents)), 2)
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 400_000)

    def test_wrong_configuration_backend_and_explicit_member_binding_do_not_activate(self):
        original = self.waiter()
        self.member("a")
        self.worker("wrong-config", instance="node-a", config="different-config")
        self.worker("old-engine", instance="node-a", backend="comfy-worker", config="old-comfy-config")
        self.assertEqual(self.controller.advance_once("cold-approval")["activated"], 0)
        self.member("b")
        self.worker("b")
        with self.repo.transaction() as conn:
            conn.execute(update(capacity_waiters).where(capacity_waiters.c.job_id == original["id"])
                .values(approval_hash="0"*64))
        result = self.controller.advance_once("cold-approval")
        self.assertEqual(result["activated"], 0)
        self.assertEqual(self.repo.get_job(self.scope, original["id"])["error_code"], "capacity_pool_waiter_identity_mismatch")

    def test_expired_member_or_revoked_grant_cannot_activate_and_cancel_is_preserved(self):
        original = self.waiter()
        member = self.member("a")
        self.worker("a")
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).where(instance_intents.c.id == member["id"]).values(
                hard_deadline=self.now+original["expected_runtime_s"]))
        self.assertEqual(self.controller.advance_once("cold-approval")["activated"], 0)
        self.repo.request_cancel(self.scope, original["id"])
        self.controller.advance_once("cold-approval")
        self.assertEqual(self.repo.get_job(self.scope, original["id"])["status"], "cancelled")
        self.repo.set_capacity_approval_enabled("cold-approval", enabled=False)
        with self.assertRaises(Conflict):
            self.repo.reserve_capacity_member("cold-approval", "b")

    def test_pool_plan_marker_hash_and_tenant_must_match_at_admission(self):
        for delta in ({"capacity_binding": None}, {"capacity_binding": "unknown-v2"},
                {"capacity_approval_hash": "0"*64}):
            execution = {**self.admission.execution, **delta}
            plan = self.repo.create_plan(self.scope, self.compiled, execution, expires_at=self.now+100,
                estimated_cost_microusd=self.admission.cost)
            with self.subTest(delta=delta), self.assertRaisesRegex(Conflict, "approval_mismatch"):
                self.repo.create_job(self.scope, plan["id"], "bad-marker", initial_status="waiting_capacity",
                    budget_account_ids=["owner:superdan"])
        foreign = Scope("different-tenant", "superdan", "project-1")
        plan = self.repo.create_plan(foreign, self.compiled, self.admission.execution, expires_at=self.now+100)
        with self.assertRaisesRegex(Conflict, "approval_mismatch"):
            self.repo.create_job(foreign, plan["id"], "bad-owner", initial_status="waiting_capacity",
                budget_account_ids=[])
