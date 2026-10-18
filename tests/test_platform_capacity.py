"""Cold start contracts: temporary SQLite / isolated local PG and fake provider only."""
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select, update

from studio_platform.autoscale import ScalePolicy
from studio_platform.capacity import ColdStartCoordinator
from studio_platform.capabilities import MODEL, compile_request
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.queue import TaskQueue
from studio_platform.repository import (BudgetExceeded, Conflict, Scope, capacity_approvals,
    capacity_cycles, capacity_waiters, instance_intents, jobs, request_hash)
from studio_platform.scaler import LaunchSpec, ProviderFact, ScaleCoordinator
from studio_platform.settings import Settings
from test_platform_api import generation_request
from test_platform_execution_policy import policy
from test_platform_repository import LedgerCase
from test_platform_scaler import FakeProvider


class CapacityTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.value = policy(self.now)
        self.value.update(pool="cold-pool", configuration_id="cold-config", budget_accounts=["owner:{owner_id}"])
        self.path = Path(self.temp.name)/"synthetic-policy.json"
        self.write()
        self.settings = Settings(Path(self.temp.name)/"data", generation_enabled=True,
            execution_backend="comfy-worker", execution_policy_file=self.path)
        self.policies = ExecutionPolicies(self.settings, self.repo)
        self.compiled, self.fingerprint = compile_request(generation_request(), lambda _: None)
        for owner in ("superdan", "supervan"):
            self.repo.configure_budget("owner:"+owner, tenant_id=self.scope.tenant_id, owner_id=owner, limit_microusd=10_000_000)
        self.repo.configure_budget("capacity-budget", tenant_id=self.scope.tenant_id, limit_microusd=10_000_000)
        self.billing_scope = Scope(self.scope.tenant_id, "operator", "platform-capacity")
        self.repo.configure_pool("cold-pool", max_instances=2, max_physical_gpus=4)
        self.launch = LaunchSpec("test-only", "cold-config", MODEL)
        self.scale_policy = ScalePolicy(dry_run=False, max_instances=2, max_physical_gpus=4,
            new_instance_physical_gpus=2, new_instance_slots=1, cold_start_s=30,
            queue_target_s=60, min_improvement_s=1, cooldown_s=0, idle_before_drain_s=900,
            approved_remaining_microusd=10_000_000, instance_reservation_microusd=200_000, hard_deadline=self.now+3600)
        self.provider = FakeProvider()
        self.scaler = ScaleCoordinator(self.repo, provider=self.provider, enabled=True)
        self.controller = ColdStartCoordinator(self.repo, scaler=self.scaler, enabled=True,
            approval_guard=self.policies.capacity_approval_current, activation_guard=self.policies.activation_allowed)

    def write(self):
        self.path.write_text(json.dumps(self.value), encoding="utf-8")
        self.path.chmod(0o600)

    def approve(self, approval_id="cold-approval", **kwargs):
        values = dict(tenant_id=self.scope.tenant_id, pool="cold-pool", model_id=MODEL, configuration_id="cold-config",
            recipe_ids=self.value["recipe_ids"], policy_hash=request_hash(self.value),
            qualification_evidence_id=self.value["qualification"]["evidence_id"],
            qualification_expires_at=self.value["qualification"]["expires_at"],
            quote_expires_at=self.value["reservation"]["expires_at"], expires_at=self.now+800,
            launch=self.launch, scale_policy=self.scale_policy, budget_scope=self.billing_scope,
            budget_account_ids=["capacity-budget"], enabled=True)
        values.update(kwargs)
        return self.repo.approve_capacity(approval_id, **values)

    def waiting(self, key="one", scope=None, *, initial_status="waiting_capacity"):
        scope = scope or self.scope
        admission = self.policies.evaluate(self.compiled, scope, self.fingerprint)
        self.assertTrue(admission.execution["enabled"], admission.execution)
        self.assertEqual(admission.execution["admission_state"], "waiting_capacity")
        plan = self.repo.create_plan(scope, self.compiled, admission.execution,
            expires_at=admission.expires_at, estimated_cost_microusd=admission.cost)
        return self.repo.create_job(scope, plan["id"], key, initial_status=initial_status,
            budget_account_ids=admission.execution["budget_account_ids"])

    def tick(self, leader="leader", approval="cold-approval"):
        return self.controller.tick(leader, approval)

    def start(self):
        self.assertEqual(self.tick()["reason"], "observe_again")
        self.now += 16
        result = self.tick()
        self.assertEqual(result["state"], "creation_observed", result)
        return self.repo.list_instance_intents(pool="cold-pool")[0]

    def worker(self, intent, *, name="cold-worker", ready=True, config="cold-config", model=MODEL, instance=None, recipes=None):
        control = WorkerControl(self.repo)
        spec = WorkerSpec(name, "cold-pool", "test-only", instance or intent["provider_instance_id"],
            (name+"-gpu-0", name+"-gpu-1"), tuple(recipes or self.value["recipe_ids"]), model, config)
        control.register(spec)
        if ready:
            control.mark_ready(name, upstream_idle_confirmed=True)
        return control, spec

    def test_default_disabled_writes_nothing_and_unapproved_zero_worker_is_blocked(self):
        self.assertFalse(self.policies.evaluate(self.compiled, self.scope, self.fingerprint).execution["enabled"])
        with patch.object(self.repo, "transaction", side_effect=AssertionError("no default write")):
            self.assertEqual(ColdStartCoordinator(self.repo).tick("leader", "nonexistent"), {"state": "disabled"})
        self.assertEqual(self.provider.creates, [])

    def test_cold_plan_expires_before_boot_plus_runtime_cannot_fit_and_confirmation_rechecks(self):
        self.scale_policy = replace(self.scale_policy, cold_start_s=400, hard_deadline=self.now+1000)
        self.approve(expires_at=self.now+1000)
        result = self.policies.evaluate(self.compiled, self.scope, self.fingerprint)
        self.assertTrue(result.execution["enabled"])
        self.assertEqual(result.expires_at, self.now+300)
        plan = self.repo.create_plan(self.scope, self.compiled, result.execution,
            expires_at=result.expires_at, estimated_cost_microusd=result.cost)
        self.now += 300
        blocked = self.policies.evaluate(self.compiled, self.scope, self.fingerprint)
        self.assertFalse(blocked.execution["enabled"])
        self.assertIn("剩余运行窗口不足以启动并完成", " ".join(blocked.execution["blockers"]))
        self.assertNotIn("无有效的独立冷启动审批", " ".join(blocked.execution["blockers"]))
        with self.assertRaises(Conflict):
            self.policies.ensure_current(plan, self.scope)
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 0)

    def test_quote_or_qualification_expiry_limits_cold_admission_but_ready_worker_needs_no_boot(self):
        initial = self.now
        self.value["qualification"]["expires_at"] = initial+1400
        self.value["reservation"]["expires_at"] = initial+1100
        self.write()
        self.scale_policy = replace(self.scale_policy, cold_start_s=600, hard_deadline=initial+1600)
        self.approve(expires_at=initial+1000)
        result = self.policies.evaluate(self.compiled, self.scope, self.fingerprint)
        self.assertEqual(result.expires_at, initial+100)
        # No live worker: even an approved policy cannot start at this boundary.
        self.now = initial+100
        self.assertFalse(self.policies.evaluate(self.compiled, self.scope, self.fingerprint).execution["enabled"])
        control = WorkerControl(self.repo)
        control.register(WorkerSpec("already-ready", "cold-pool", "test-only", "already-started", ("ready-gpu",),
            tuple(self.value["recipe_ids"]), MODEL, "cold-config"))
        control.mark_ready("already-ready", upstream_idle_confirmed=True)
        warm = self.policies.evaluate(self.compiled, self.scope, self.fingerprint)
        self.assertTrue(warm.execution["enabled"], warm.execution["blockers"])
        self.assertEqual(warm.execution["admission_state"], "queued")
        self.assertNotIn("capacity_approval_id", warm.execution)
        self.assertEqual(warm.expires_at, initial+800)

    def test_starting_provider_without_qualified_worker_keeps_conservative_boot_window(self):
        self.scale_policy = replace(self.scale_policy, cold_start_s=400, hard_deadline=self.now+1000)
        self.approve(expires_at=self.now+1000)
        job = self.waiting()
        intent = self.start()
        # Provider-confirmed TTL may be shorter than the approval's estimate.
        with self.repo.transaction() as connection:
            connection.execute(update(instance_intents).where(instance_intents.c.id == intent["id"])
                .values(hard_deadline=self.now+650))
        shortened = self.policies.evaluate(self.compiled, self.scope, self.fingerprint)
        self.assertFalse(shortened.execution["enabled"])
        self.assertIn("剩余运行窗口不足以启动并完成", " ".join(shortened.execution["blockers"]))
        with self.repo.transaction() as connection:
            connection.execute(update(instance_intents).where(instance_intents.c.id == intent["id"])
                .values(hard_deadline=intent["hard_deadline"]))
        self.now += 284  # reaches the same latest cold admission boundary
        result = self.policies.evaluate(self.compiled, self.scope, self.fingerprint)
        self.assertFalse(result.execution["enabled"])
        self.assertIn("剩余运行窗口不足以启动并完成", " ".join(result.execution["blockers"]))
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "waiting_capacity")
        self.assertEqual(len(self.provider.creates), 1)

    def test_approval_immutable_revocation_cannot_be_silently_reenabled_by_retry(self):
        approved = self.approve()
        self.repo.set_capacity_approval_enabled(approved["id"], enabled=False)
        self.assertEqual(self.approve()["enabled"], 0)
        with self.assertRaisesRegex(Conflict, "immutable"):
            self.approve(expires_at=self.now+700)
        self.assertFalse(self.policies.evaluate(self.compiled, self.scope, self.fingerprint).execution["enabled"])

    def test_waiting_reserves_once_is_not_claimable_and_idempotency_returns_original(self):
        self.approve()
        job = self.waiting()
        self.assertEqual(job["attempt_no"], 0)
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 500_000)
        self.assertIsNone(TaskQueue(self.repo).claim("arbitrary-worker", "cold-pool"))
        self.repo.set_capacity_approval_enabled("cold-approval", enabled=False)
        retry = self.repo.create_job(self.scope, job["plan_id"], "one", initial_status="waiting_capacity", budget_account_ids=["owner:superdan"])
        self.assertEqual(retry["id"], job["id"])
        self.assertFalse(retry["created"])
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 500_000)
        self.assertEqual(self.tick()["failed"], 1)
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 0)

    def test_two_owner_waiters_share_committed_intent_before_single_post(self):
        self.approve()
        one, two = self.waiting(), self.waiting("two", self.other)
        def before_post(tag):
            with self.repo.engine.connect() as connection:
                cycle = connection.execute(select(capacity_cycles)).mappings().one()
                waiters = list(connection.execute(select(capacity_waiters)).mappings())
            self.assertEqual(cycle["intent_id"], tag)
            self.assertEqual({w["intent_id"] for w in waiters}, {tag})
            self.assertEqual({w["job_id"] for w in waiters}, {one["id"], two["id"]})
            self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 200_000)
        self.provider.on_create = before_post
        instance = self.start()
        self.assertEqual(instance["physical_gpus"], 2)
        self.assertEqual(instance["slots"], 1)
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.repo.get_job(self.scope, one["id"])["status"], "waiting_capacity")

    def test_two_controllers_only_one_intent_and_creation(self):
        self.approve()
        self.waiting()
        self.tick()
        self.now += 16
        results = self.parallel(lambda i: self.tick("leader" if i%2 == 0 else "other-leader"))
        self.assertEqual(sum(r["state"] == "creation_observed" for r in results), 1)
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(len(self.repo.list_instance_intents()), 1)

    def test_vm_running_registered_wrong_model_config_recipe_instance_never_activate(self):
        self.approve()
        job = self.waiting()
        instance = self.start()
        control, spec = self.worker(instance, ready=False)
        self.now += 16
        self.assertEqual(self.tick()["activated"], 0)
        with self.repo.transaction() as connection:
            connection.execute(update(registered_workers).where(registered_workers.c.id == spec.worker_id)
                .values(state="ready", expires_at=self.now+120))
        # Test exact identities without pretending wrong worker is qualified.
        for field, wrong in (("model_id", "different-model"), ("configuration_id", "different-config"),
                ("recipe_ids", ["different-recipe"])):
            with self.repo.transaction() as connection:
                original = dict(connection.execute(select(registered_workers.c.spec).where(registered_workers.c.id == spec.worker_id)).scalar_one())
                changed = dict(original, **{field: wrong})
                connection.execute(update(registered_workers).where(registered_workers.c.id == spec.worker_id).values(spec=changed))
            self.assertEqual(self.tick()["activated"], 0, field)
            with self.repo.transaction() as connection:
                connection.execute(update(registered_workers).where(registered_workers.c.id == spec.worker_id).values(spec=original))
        with self.repo.transaction() as connection:
            connection.execute(update(registered_workers).where(registered_workers.c.id == spec.worker_id).values(instance_id="unrelated-instance"))
        self.assertEqual(self.tick()["activated"], 0)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "waiting_capacity")

    def test_ready_activates_same_jobs_once_preserves_budgets_and_owner_fairness(self):
        self.approve()
        dan = [self.waiting("dan-"+str(i)) for i in range(3)]
        van = self.waiting("van", self.other)
        instance = self.start()
        control, spec = self.worker(instance)
        self.assertEqual(self.tick()["activated"], 4)
        self.assertEqual(self.tick()["activated"], 0)
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 1_500_000)
        self.assertEqual(self.repo.get_budget("owner:supervan")["reserved_microusd"], 500_000)
        self.assertEqual({j["id"] for j in self.repo.list_jobs(self.scope)}, {j["id"] for j in dan})
        claim1 = control.claim(spec.worker_id, spec.pool)
        self.repo.request_cancel(Scope(claim1.job["tenant_id"], claim1.job["owner_id"], claim1.job["project_id"]), claim1.job["id"])
        control.observe(spec.worker_id, claim1.job["id"])
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        claim2 = control.claim(spec.worker_id, spec.pool)
        self.assertNotEqual(claim1.job["owner_id"], claim2.job["owner_id"])

    def test_cancel_one_waiter_keeps_instance_and_other_owner_job_reservation(self):
        self.approve()
        dan, van = self.waiting(), self.waiting("two", self.other)
        instance = self.start()
        self.repo.request_cancel(self.scope, dan["id"])
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 0)
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 200_000)
        self.worker(instance)
        self.assertEqual(self.tick()["activated"], 1)
        self.assertEqual(self.repo.get_job(self.scope, dan["id"])["status"], "cancelled")
        self.assertEqual(self.repo.get_job(self.other, van["id"])["status"], "queued")
        self.assertEqual(self.provider.destroys, [])

    def test_expired_or_changed_operator_policy_fails_wait_without_releasing_instance(self):
        self.approve()
        job = self.waiting()
        self.start()
        self.value["revision"] = "changed-after-submit"
        self.write()
        self.assertEqual(self.tick()["failed"], 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "failed")
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 0)
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 200_000)
        self.assertEqual(len(self.provider.creates), 1)

    def test_wait_deadline_and_ttl_do_not_recycle_finished_cycle(self):
        self.approve()
        job = self.waiting()
        instance = self.start()
        self.now = 1801
        self.assertEqual(self.tick()["failed"], 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "failed")
        self.provider.facts[instance["id"]] = ProviderFact("destroyed", instance["provider_instance_id"])
        self.now += 16
        self.tick()
        self.assertEqual(self.repo.list_instance_intents()[0]["state"], "destroyed")
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 200_000)
        self.now += 16
        self.tick()
        self.assertEqual(len(self.provider.creates), 1)

    def test_unknown_create_and_empty_reconcile_never_second_rent_or_release(self):
        self.approve()
        self.waiting()
        self.provider.create_uncertain = True
        instance = self.start()
        self.provider.facts[instance["id"]] = ProviderFact("unknown")
        for _ in range(3):
            self.now += 16
            self.tick()
        self.assertEqual(self.repo.list_instance_intents()[0]["state"], "creation_unknown")
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 200_000)

    def test_cancel_all_before_create_proves_no_rent_and_no_reserved_instance(self):
        self.approve()
        job = self.waiting()
        self.repo.request_cancel(self.scope, job["id"])
        self.assertEqual(self.tick()["state"], "no_waiters")
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.repo.list_instance_intents(), [])

    def test_revocation_between_commit_and_create_prevents_post_and_releases_only_instance_zero(self):
        self.approve()
        self.waiting()
        self.tick()
        self.now += 16
        real_call = self.scaler._call
        def revoke_then_call(intent, operation, **kwargs):
            if operation == "create":
                self.repo.set_capacity_approval_enabled("cold-approval", enabled=False)
            return real_call(intent, operation, **kwargs)
        with patch.object(self.scaler, "_call", side_effect=revoke_then_call):
            result = self.tick()
        self.assertEqual(result["provider_state"], "not_created")
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 0)
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 0)

    def test_gate_zero_missing_instance_budget_ambiguous_approval_or_wrong_tenant_blocks(self):
        self.approve()
        self.repo.configure_capacity(max_instances=0, max_physical_gpus=0)
        self.assertFalse(self.policies.evaluate(self.compiled, self.scope, self.fingerprint).execution["enabled"])
        self.repo.configure_capacity(max_instances=10, max_physical_gpus=10)
        self.repo.configure_budget("capacity-budget", tenant_id=self.scope.tenant_id, limit_microusd=100_000)
        self.assertFalse(self.policies.evaluate(self.compiled, self.scope, self.fingerprint).execution["enabled"])
        self.repo.configure_budget("capacity-budget", tenant_id=self.scope.tenant_id, limit_microusd=10_000_000)
        self.approve("conflicting-approval")
        self.assertFalse(self.policies.evaluate(self.compiled, self.scope, self.fingerprint).execution["enabled"])
        self.assertIsNone(self.repo.find_capacity_approval(Scope("other-tenant", "superdan", "project-1"),
            pool="cold-pool", model_id=MODEL, configuration_id="cold-config", recipe_id=self.value["recipe_ids"][0], policy_hash=request_hash(self.value)))

    def test_planned_enqueue_becomes_waiting_and_direct_queued_bypass_is_rejected(self):
        self.approve()
        job = self.waiting(initial_status="planned")
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 0)
        with self.assertRaisesRegex(Conflict, "requires_waiting"):
            self.repo.create_job(self.scope, job["plan_id"], "bypass", budget_account_ids=["owner:superdan"])
        queued = self.repo.enqueue(self.scope, job["id"], budget_account_ids=["owner:superdan"])
        self.assertEqual(queued["status"], "waiting_capacity")
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 500_000)

    def test_restored_hold_approval_disabled_and_waiter_hold_cannot_activate_or_rent(self):
        self.approve()
        job = self.waiting()
        instance = self.start()
        self.worker(instance)
        with self.repo.transaction() as connection:
            connection.execute(update(capacity_approvals).values(enabled=0))
            connection.execute(update(capacity_waiters).values(state="recovery_hold"))
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="recovery_hold"))
        self.repo.request_cancel(self.scope, job["id"])
        self.assertEqual(self.tick()["activated"], 0)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "recovery_hold")
        self.assertIsNone(TaskQueue(self.repo).claim("worker", "cold-pool"))
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 500_000)

    def test_expired_registration_and_old_qualification_cannot_activate(self):
        self.approve()
        job = self.waiting()
        instance = self.start()
        control, spec = self.worker(instance)
        self.now += 121
        self.assertEqual(self.tick()["activated"], 0)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "waiting_capacity")
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        self.value["qualification"]["expires_at"] = self.now-1
        self.write()
        self.assertEqual(self.tick()["failed"], 1)
        self.assertEqual(self.provider.destroys, [])

    def test_waiter_joining_existing_cycle_uses_same_intent_not_new_budget(self):
        self.approve()
        self.waiting()
        instance = self.start()
        other = self.waiting("late-join", self.other)
        with self.repo.engine.connect() as connection:
            linked = connection.execute(select(capacity_waiters.c.intent_id).where(capacity_waiters.c.job_id == other["id"])).scalar_one()
        self.assertEqual(linked, instance["id"])
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 200_000)
        self.now += 16
        self.tick()
        self.assertEqual(len(self.provider.creates), 1)

    def test_revocation_after_activation_blocks_new_submission_but_not_existing_attempt_reconcile(self):
        self.approve()
        job = self.waiting()
        instance = self.start()
        control, spec = self.worker(instance)
        self.tick()
        current = self.repo.get_job(self.scope, job["id"])
        self.assertTrue(self.policies.submission_allowed(current))
        claim = control.claim(spec.worker_id, spec.pool)
        control.queue.begin_submission(claim.lease)
        control.queue.mark_submission_unknown(claim.lease)
        self.repo.set_capacity_approval_enabled("cold-approval", enabled=False)
        self.assertFalse(self.policies.submission_allowed(current))
        reconciler = control.claim(spec.worker_id, spec.pool, purpose="reconcile")
        self.assertIsNotNone(reconciler)
        self.assertEqual(reconciler.lease.attempt_id, claim.lease.attempt_id)
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 500_000)

    def test_unsafe_remaining_instance_ttl_fails_wait_without_claim_or_rerent(self):
        self.approve()
        job = self.waiting()
        instance = self.start()
        with self.repo.transaction() as connection:
            connection.execute(update(instance_intents).where(instance_intents.c.id == instance["id"])
                .values(hard_deadline=self.now+100))
        self.worker(instance)
        self.assertEqual(self.tick()["failed"], 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["error_code"], "capacity_instance_deadline_unsafe")
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 200_000)

    def test_tp2_physical_global_gate_blocks_single_gpu_approval(self):
        self.approve()
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.assertFalse(self.policies.evaluate(self.compiled, self.scope, self.fingerprint).execution["enabled"])
        self.assertEqual(self.repo.list_instance_intents(), [])

    def test_idle_proof_cannot_drain_instance_with_unclaimed_waiting_capacity(self):
        self.approve()
        job = self.waiting()
        instance = self.start()
        self.provider.facts[instance["id"]] = ProviderFact("running", instance["provider_instance_id"],
            idle_confirmed=True, idle_since=0)
        self.now += 16
        self.tick()
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "waiting_capacity")

    def test_failure_and_cancel_with_shared_task_instance_account_do_not_deadlock(self):
        self.value["budget_accounts"] = ["capacity-budget"]
        self.write()
        self.approve()
        one, two = self.waiting(), self.waiting("other", self.other)
        self.tick()
        self.now += 16
        outcomes = self.parallel(lambda i: self.repo.request_cancel(self.scope, one["id"]) if i%2
            else self.tick("leader"))
        self.assertTrue(outcomes)
        self.assertEqual(self.repo.get_job(self.scope, one["id"])["status"], "cancelled")
        self.assertEqual(self.repo.get_job(self.other, two["id"])["status"], "waiting_capacity")
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 700_000)

    def test_confirmed_wait_survives_plan_confirmation_window_but_new_submission_does_not(self):
        self.approve(expires_at=self.now+2000)
        job = self.waiting()
        plan = self.repo.get_plan(self.scope, job["plan_id"])
        self.assertEqual(plan["expires_at"], 1900)
        instance = self.start()
        self.now = 1901  # >15-minute confirmation window, still approved boot time
        with self.assertRaisesRegex(Conflict, "plan_expired"):
            self.repo.create_job(self.scope, plan["id"], "new-after-expiry", initial_status="waiting_capacity",
                budget_account_ids=["owner:superdan"])
        retry = self.repo.create_job(self.scope, plan["id"], "one", initial_status="waiting_capacity",
            budget_account_ids=["owner:superdan"])
        self.assertEqual(retry["id"], job["id"])
        self.worker(instance)
        self.assertEqual(self.tick()["activated"], 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "queued")
        self.assertEqual(self.repo.get_budget("owner:superdan")["reserved_microusd"], 500_000)
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 200_000)


# Import explicit schema used by qualification-corruption fixtures above.
from studio_platform.repository import registered_workers

if __name__ == "__main__":
    import unittest
    unittest.main()
