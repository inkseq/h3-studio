"""Temporary SQLite/isolated local PG; all provider calls are injected fake facts."""
from dataclasses import replace
import threading
from unittest.mock import patch

from sqlalchemy import select, update

from studio_platform.autoscale import Demand, ScalePolicy, Slot
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.repository import (
    Conflict, instance_intents, jobs, registered_workers, scaler_actions, scaler_observations,
)
from studio_platform.scaler import DisabledProvider, LaunchSpec, ProviderFact, ScaleCoordinator
from test_platform_repository import LedgerCase


class FakeProvider:
    enabled = True
    provider_id = "test-only"

    def __init__(self):
        self.creates, self.lookups, self.destroys = [], [], []
        self.facts, self.invoices = {}, {}
        self.on_create = None
        self.create_uncertain = False

    def create(self, tag, launch, *, hard_deadline):
        self.creates.append((tag, launch, hard_deadline))
        fact = ProviderFact("running", "fake-"+tag)
        self.facts[tag] = fact
        if self.on_create:
            self.on_create(tag)
        if self.create_uncertain:
            raise TimeoutError("fake-response-lost")
        return fact

    def reconcile(self, tag, instance_id):
        self.lookups.append((tag, instance_id))
        return self.facts.get(tag, ProviderFact("unknown"))

    def destroy(self, tag, instance_id):
        self.destroys.append((tag, instance_id))
        self.facts[tag] = ProviderFact("destroyed", instance_id)
        return self.facts[tag]

    def billing(self, tag, instance_id):
        return self.invoices.get(tag)


class ScalerTests(LedgerCase):
    def test_pending_removal_checks_each_minute_across_restart_and_failed_reads(self):
        row = self.create()
        started = self.now
        with self.repo.transaction() as connection:
            connection.execute(update(instance_intents).where(instance_intents.c.id == row['id']).values(state='destroying'))
            connection.execute(update(scaler_actions).where(scaler_actions.c.intent_id == row['id']).values(destroy_started_at=started))
        budget = self.repo.get_budget('owner-budget')
        self.provider.facts[row['id']] = ProviderFact('unknown', row['provider_instance_id'])
        def observe():
            self.scaler.observe_manual_instance('leader', row['id'], policy=self.policy, stop=True)
        for elapsed in (0, 3, 30, 59):
            self.now = started + elapsed
            observe()
        self.assertEqual(self.provider.lookups, [])
        self.now = started + 60
        with patch.object(self.provider, 'reconcile', side_effect=TimeoutError) as lookup:
            observe()
            self.assertEqual(lookup.call_count, 1)
        self.scaler = ScaleCoordinator(self.repo, provider=self.provider, enabled=True)
        self.now = started + 119
        observe()
        self.assertEqual(self.provider.lookups, [])
        self.now = started + 120
        observe()
        self.assertEqual(len(self.provider.lookups), 1)
        self.assertEqual(self.repo.list_instance_intents()[0]['state'], 'destroying')
        self.assertEqual(self.repo.get_budget('owner-budget'), budget)
        self.assertEqual(self.provider.destroys, [])

    def test_pending_removal_crash_retains_cadence_but_late_terminal_receipt_wins(self):
        row = self.create()
        started = self.now
        with self.repo.transaction() as connection:
            connection.execute(update(instance_intents).where(instance_intents.c.id == row['id']).values(state='destroying'))
            connection.execute(update(scaler_actions).where(scaler_actions.c.intent_id == row['id']).values(destroy_started_at=started))
        class Crash(BaseException): pass
        self.now += 60
        with patch.object(self.scaler, '_call', side_effect=Crash), self.assertRaises(Crash):
            self.scaler.observe_manual_instance('leader', row['id'], policy=self.policy)
        self.scaler = ScaleCoordinator(self.repo, provider=self.provider, enabled=True)
        self.now += 30
        self.scaler.observe_manual_instance('leader', row['id'], policy=self.policy)
        self.assertEqual(self.provider.lookups, [])
        self.scaler._receipt(row['id'], 'destroy', ProviderFact('destroyed', row['provider_instance_id']))
        self.scaler.observe_manual_instance('leader', row['id'], policy=self.policy)
        self.assertEqual(self.repo.list_instance_intents()[0]['state'], 'destroyed')
        self.assertEqual(self.provider.lookups, [])
        self.assertEqual(self.repo.get_budget('owner-budget')['reserved_microusd'], 100_000)

    def test_disabled_preparation_preserves_historical_receipt_and_action_payloads(self):
        from studio_platform.repository import scaler_receipts
        row = self.create()
        self.provider.facts[row['id']] = ProviderFact('starting', row['provider_instance_id'],
            provider_status='PENDING', preparation_stage='configuring_ssh')
        self.now += 121
        self.tick(demands=[])
        with self.repo.engine.connect() as conn:
            payloads = list(conn.execute(select(scaler_receipts.c.facts).where(
                scaler_receipts.c.intent_id == row['id'])).scalars())
            payloads.append(conn.execute(select(scaler_actions.c.last_observation).where(
                scaler_actions.c.intent_id == row['id'])).scalar_one())
        for payload in payloads:
            self.assertNotIn('provider_status', payload)
            self.assertNotIn('preparation_stage', payload)
        self.assertEqual(self.provider.destroys, [])

    def timed_preparation(self):
        self.scaler = ScaleCoordinator(self.repo, provider=self.provider, enabled=True,
            preparation_timeout_s=120, preparation_binding='a'*64,
            unused_preparation_guard=lambda conn, intent: True)
        def committed_before_post(tag):
            from studio_platform.repository import scaler_receipts
            with self.repo.engine.connect() as conn:
                phase = conn.execute(select(scaler_receipts.c.facts).where(
                    scaler_receipts.c.intent_id == tag,
                    scaler_receipts.c.operation == 'provider_preparation')).scalar_one()
            self.assertEqual(phase['phase'], 'awaiting_provider')
        self.provider.on_create = committed_before_post
        row = self.create()
        self.provider.facts[row['id']] = ProviderFact('starting', row['provider_instance_id'], provider_status='PENDING')
        return row

    def test_preparation_guard_checks_age_freshness_instance_worker_and_default_opt_out(self):
        row = self.timed_preparation()
        self.now += 119
        self.tick()
        self.assertEqual(self.provider.destroys, [])
        control, spec = self.ready_instance(row)
        # A worker record contradicts unused proof, even after it is retired.
        control.retire(spec.worker_id, upstream_idle_confirmed=True)
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).where(instance_intents.c.id == row['id']).values(state='starting'))
        self.now += 2
        self.tick()
        self.assertEqual(self.provider.destroys, [])

    def test_crash_after_retirement_marker_before_delete_never_replays_or_starts(self):
        row = self.timed_preparation()
        self.now += 121
        class Crash(BaseException): pass
        original = self.scaler._call
        def call(intent, operation, **kwargs):
            if operation == 'destroy': raise Crash()
            return original(intent, operation, **kwargs)
        with patch.object(self.scaler, '_call', side_effect=call), self.assertRaises(Crash):
            self.tick()
        self.assertEqual(self.repo.list_instance_intents()[0]['state'], 'destroying')
        self.now += 61
        self.tick(leader='replacement')
        lease = self.scaler.acquire('scale-test', 'replacement')
        self.assertFalse(self.scaler.claim_bootstrap(lease, row['id']))
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.repo.get_budget('owner-budget')['reserved_microusd'], 100_000)

    def test_committed_bootstrap_claim_refuses_later_failed_status_retirement(self):
        row = self.timed_preparation()
        self.provider.facts[row['id']] = ProviderFact('running', row['provider_instance_id'])
        self.now += 121
        self.tick()
        lease = self.scaler.acquire('scale-test', 'leader')
        self.assertTrue(self.scaler.claim_bootstrap(lease, row['id']))
        self.provider.facts[row['id']] = ProviderFact('starting', row['provider_instance_id'], provider_status='FAILED')
        self.tick(demands=[])
        self.assertEqual(self.provider.destroys, [])

    def setUp(self):
        super().setUp()
        self.provider = FakeProvider()
        self.scaler = ScaleCoordinator(self.repo, provider=self.provider, enabled=True)
        self.launch = LaunchSpec("test-only", "test-manifest", "test-model", region="local-test")
        self.policy = ScalePolicy(dry_run=False, max_instances=4, max_physical_gpus=4, cold_start_s=30,
            min_improvement_s=1, cooldown_s=0, queue_target_s=60, approved_remaining_microusd=10_000_000,
            instance_reservation_microusd=100_000, hard_deadline=5000)
        self.demands = [Demand("job-"+str(i), "superdan", 900, 120) for i in range(12)]
        self.repo.configure_pool("scale-test", max_instances=4, max_physical_gpus=4)

    def tick(self, *, leader="leader", pool="scale-test", demands=None, slots=(), policy=None, scaler=None):
        return (scaler or self.scaler).tick(leader, self.scope, pool, self.demands if demands is None else demands,
            slots, policy=policy or self.policy, launch=self.launch, budget_account_ids=["owner-budget"])

    def create(self):
        self.assertEqual(self.tick()["reason"], "observe_again")
        self.now += 16
        result = self.tick()
        self.assertEqual(result["state"], "creation_observed", result)
        return self.repo.list_instance_intents(pool="scale-test")[0]

    def ready_instance(self, instance):
        self.repo.update_instance(instance["id"], "ready")
        control = WorkerControl(self.repo)
        spec = WorkerSpec("fake-worker", "scale-test", "test-only", instance["provider_instance_id"], ("fake-gpu",),
            ("test-recipe",), "test-model", "test-manifest")
        control.register(spec)
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        return control, spec

    def test_default_disabled_and_disabled_provider_do_not_create(self):
        with patch.object(self.repo, "transaction", side_effect=AssertionError("default makes no writes")):
            self.assertEqual(ScaleCoordinator(self.repo).tick("leader", self.scope, "scale-test", [], [])["state"], "disabled")
        disabled = ScaleCoordinator(self.repo, enabled=True)
        self.tick(scaler=disabled)
        self.now += 16
        self.assertEqual(self.tick(scaler=disabled)["reason"], "provider_or_launch_not_configured")
        self.assertEqual(self.repo.list_instance_intents(), [])

    def test_dryrun_persists_observations_only_no_capacity_or_budget_reservation(self):
        policy = replace(self.policy, dry_run=True)
        self.tick(policy=policy)
        self.now += 16
        self.assertEqual(self.tick(policy=policy)["state"], "dry_run")
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.repo.list_instance_intents(), [])
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)
        with self.repo.engine.connect() as connection:
            self.assertEqual(len(list(connection.execute(select(scaler_observations)))), 2)

    def test_observations_are_throttled_and_policy_change_resets_breach_count(self):
        self.tick()
        self.assertEqual(self.tick()["state"], "observation_throttled")
        self.now += 16
        self.assertEqual(self.tick(policy=replace(self.policy, cold_start_s=31))["reason"], "observe_again")
        self.assertEqual(self.provider.creates, [])

    def test_intent_budget_and_single_creation_start_commit_before_provider_call(self):
        def check(tag):
            row = self.repo.list_instance_intents()[0]
            self.assertEqual(row["id"], tag)
            self.assertEqual(row["state"], "creating")
            self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000)
            with self.repo.engine.connect() as connection:
                self.assertIsNotNone(connection.execute(select(scaler_actions.c.create_started_at)).scalar_one())
        self.provider.on_create = check
        instance = self.create()
        self.assertEqual(instance["state"], "starting")
        self.assertEqual(len(self.provider.creates), 1)
        # A VM response cannot silently qualify/register a model execution slot.
        self.assertEqual(WorkerControl(self.repo).pool_status("scale-test", model_id="test-model",
            configuration_id="test-manifest")["matched_slots"], 0)

    def test_timeout_unknown_does_not_automatically_resend_or_rent_second_instance(self):
        self.provider.create_uncertain = True
        instance = self.create()
        self.assertEqual(instance["state"], "creation_unknown")
        self.provider.facts[instance["id"]] = ProviderFact("unknown")
        for _ in range(3):
            self.now += 16
            self.assertEqual(self.tick()["reason"], "creation_needs_reconciliation")
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000)

    def test_leader_handoff_recovers_late_success_receipt_before_empty_lookup(self):
        self.tick()
        self.now += 16
        def handoff(tag):
            self.now += 61
            self.assertIsNotNone(self.scaler.acquire("scale-test", "new-leader"))
        self.provider.on_create = handoff
        result = self.tick()
        self.assertEqual(result["state"], "leader_changed_reconcile_required")
        instance = self.repo.list_instance_intents()[0]
        self.assertEqual(instance["state"], "creating")
        self.provider.facts[instance["id"]] = ProviderFact("unknown")
        result = self.tick(leader="new-leader", demands=[], slots=())
        self.assertEqual(result["state"], "none")
        known = self.repo.list_instance_intents()[0]
        self.assertEqual(known["provider_instance_id"], "fake-"+instance["id"])
        self.assertEqual(known["state"], "starting")
        self.assertEqual(len(self.provider.creates), 1)

    def test_two_controller_leaders_compete_without_duplicate_create(self):
        self.tick()
        self.now += 16
        results = self.parallel(lambda i: self.tick(leader="leader" if i % 2 == 0 else "other-leader"))
        self.assertEqual(sum(r["state"] == "creation_observed" for r in results), 1)
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(len(self.repo.list_instance_intents()), 1)

    def test_cross_pool_global_capacity_and_shared_budget_reuse_existing_ledger(self):
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.create()
        self.repo.configure_pool("other-pool", max_instances=4, max_physical_gpus=4)
        self.tick(pool="other-pool")
        self.now += 16
        self.assertEqual(self.tick(pool="other-pool")["reason"], "ledger_capacity_or_budget_limit")
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000)

    def test_ttl_drains_but_running_or_unknown_jobs_cannot_be_terminated(self):
        instance = self.create()
        control, spec = self.ready_instance(instance)
        plan = self.repo.create_plan(self.scope, {"recipe_id": "test-recipe", "request": {"model": "test-model"}},
            {"pool": "scale-test", "backend": "comfy-worker", "configuration_id": "test-manifest", "enabled": True},
            expires_at=9000, estimated_cost_microusd=0)
        job = self.repo.create_job(self.scope, plan["id"], "running-job")
        claim = control.claim(spec.worker_id, spec.pool, lease_seconds=900)
        control.queue.begin_submission(claim.lease)
        control.queue.record_submitted(claim.lease, "fake-task")
        control.queue.release(claim.lease, retry_after_s=0)
        control.observe(spec.worker_id, job["id"])
        self.now = 5001
        self.provider.facts[instance["id"]] = ProviderFact("running", instance["provider_instance_id"], idle_confirmed=True, idle_since=0)
        self.tick(demands=[], slots=())
        self.assertEqual(self.repo.list_instance_intents()[0]["state"], "draining")
        self.assertEqual(control.get(spec.worker_id)["drain_requested"], 1)
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "running")

    def test_idle_destroy_releases_capacity_but_holds_unknown_invoice_then_idempotent_settle(self):
        instance = self.create()
        control, spec = self.ready_instance(instance)
        self.provider.facts[instance["id"]] = ProviderFact("running", instance["provider_instance_id"], idle_confirmed=True, idle_since=0)
        self.now += 16
        self.tick(demands=[])
        self.assertEqual(self.provider.destroys, [])
        self.now += self.policy.idle_before_drain_s
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        self.tick(demands=[])
        gone = self.repo.list_instance_intents()[0]
        self.assertEqual(gone["state"], "destroyed")
        self.assertEqual(gone["billing_status"], "pending")
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000)
        self.assertEqual(control.get(spec.worker_id)["state"], "retired")
        self.assertEqual(control.capacity(), {"instances": 0, "physical_gpus": 0})
        self.assertEqual(self.scaler.settle(instance["id"])["billing_status"], "pending")
        self.provider.invoices[instance["id"]] = 75_000
        self.assertEqual(self.scaler.settle(instance["id"])["billing_status"], "settled")
        self.assertEqual(self.scaler.settle(instance["id"])["billing_status"], "settled")
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 75_000)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)
        self.provider.invoices[instance["id"]] = 76_000
        with self.assertRaises(Conflict):
            self.scaler.settle(instance["id"])

    def test_instance_destruction_rejects_requalifying_registered_slot(self):
        instance = self.create()
        control, spec = self.ready_instance(instance)
        self.repo.update_instance(instance["id"], "draining")
        self.repo.update_instance(instance["id"], "destroying")
        with self.assertRaisesRegex(Conflict, "worker_instance_not_admitting"):
            control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)

    def test_vm_running_is_not_idle_proof_and_lost_worker_heartbeat_is_not_free(self):
        instance = self.create()
        control, spec = self.ready_instance(instance)
        self.now = 5001
        self.tick(demands=[])
        self.assertEqual(self.provider.destroys, [])
        self.provider.facts[instance["id"]] = ProviderFact("running", instance["provider_instance_id"], idle_confirmed=True, idle_since=0)
        self.now += 16
        self.tick(demands=[])
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(control.capacity(), {"instances": 1, "physical_gpus": 1})

    def test_authoritative_absence_proof_only_and_zero_invoice_releases_reservation(self):
        with self.assertRaises(ValueError):
            ProviderFact("not_created")
        self.provider.create_uncertain = True
        instance = self.create()
        self.provider.facts[instance["id"]] = ProviderFact("not_created", actual_cost_microusd=0, absence_confirmed=True)
        self.now += 16
        self.tick(demands=[])
        gone = self.repo.list_instance_intents()[0]
        self.assertEqual((gone["state"], gone["billing_status"], gone["actual_cost_microusd"]), ("destroyed", "settled", 0))

    def test_operator_launch_manifest_cannot_contain_auth_urls_or_credentials(self):
        for value in ("https://example.invalid?token=fake", "line\nvalue", "bad/value"):
            with self.assertRaises(ValueError):
                replace(self.launch, offer_id=value)
        self.assertFalse(DisabledProvider().enabled)

    def test_crash_between_committed_create_marker_and_provider_call_never_replays_create(self):
        self.tick()
        self.now += 16
        class ProcessCrash(BaseException):
            pass
        with patch.object(self.scaler, "_call", side_effect=ProcessCrash):
            with self.assertRaises(ProcessCrash):
                self.tick()
        self.assertEqual(self.repo.list_instance_intents()[0]["state"], "creating")
        self.now += 61
        self.assertEqual(self.tick(leader="replacement")["reason"], "creation_needs_reconciliation")
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(len(self.repo.list_instance_intents()), 1)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000)

    def test_destroy_response_lost_requires_confirmation_and_is_not_reissued(self):
        instance = self.create()
        self.ready_instance(instance)
        self.provider.facts[instance["id"]] = ProviderFact("running", instance["provider_instance_id"], idle_confirmed=True, idle_since=0)
        def uncertain(tag, instance_id):
            self.provider.destroys.append((tag, instance_id))
            raise TimeoutError("fake-destroy-response-lost")
        self.provider.destroy = uncertain
        self.now += 16
        self.tick(demands=[], policy=replace(self.policy, idle_before_drain_s=0))
        self.assertEqual(self.repo.list_instance_intents()[0]["state"], "destroying")
        self.now += 16
        self.tick(demands=[])
        self.assertEqual(len(self.provider.destroys), 1)
        self.provider.facts[instance["id"]] = ProviderFact("destroyed", instance["provider_instance_id"])
        self.now += 44
        self.tick(demands=[])
        self.assertEqual(self.repo.list_instance_intents()[0]["state"], "destroyed")
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000)

    def test_pending_invoice_blocks_new_budget_even_after_physical_capacity_is_free(self):
        self.repo.configure_budget("owner-budget", tenant_id=self.scope.tenant_id,
            owner_id=self.scope.owner_id, limit_microusd=100_000)
        instance = self.create()
        self.ready_instance(instance)
        self.provider.facts[instance["id"]] = ProviderFact("running", instance["provider_instance_id"], idle_confirmed=True, idle_since=0)
        self.now += 16
        self.tick(demands=[], policy=replace(self.policy, idle_before_drain_s=0))
        self.now += 16
        self.tick()
        self.now += 16
        self.assertEqual(self.tick()["reason"], "ledger_capacity_or_budget_limit")
        self.assertEqual(len(self.provider.creates), 1)

    def test_provider_ttl_fact_does_not_mark_unresolved_job_succeeded_or_release_devices(self):
        instance = self.create()
        control, spec = self.ready_instance(instance)
        plan = self.repo.create_plan(self.scope, {"recipe_id": "test-recipe", "request": {"model": "test-model"}},
            {"pool": "scale-test", "backend": "comfy-worker", "configuration_id": "test-manifest", "enabled": True},
            expires_at=9000, estimated_cost_microusd=0)
        job = self.repo.create_job(self.scope, plan["id"], "unknown-job")
        claim = control.claim(spec.worker_id, spec.pool)
        control.queue.begin_submission(claim.lease)
        control.queue.mark_submission_unknown(claim.lease)
        control.observe(spec.worker_id, job["id"])
        self.provider.facts[instance["id"]] = ProviderFact("destroyed", instance["provider_instance_id"])
        self.now += 16
        self.tick(demands=[])
        self.assertEqual(self.repo.list_instance_intents()[0]["state"], "destroyed")
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "submission_unknown")
        self.assertEqual(control.get(spec.worker_id)["current_job_id"], job["id"])
        self.assertEqual(control.capacity(), {"instances": 1, "physical_gpus": 1})

    def test_wrong_provider_adapter_never_creates_reconciles_destroys_or_bills(self):
        self.provider.provider_id = "other-service"
        self.tick()
        self.now += 16
        self.assertEqual(self.tick()["reason"], "provider_identity_mismatch")
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.repo.list_instance_intents(), [])
        self.provider.provider_id = "test-only"
        self.now += 16
        result = self.tick()
        self.assertEqual(result["state"], "creation_observed")
        before = len(self.provider.lookups)
        self.provider.provider_id = "other-service"
        self.now += 16
        self.assertEqual(self.tick(demands=[])["reason"], "provider_identity_mismatch")
        self.assertEqual(len(self.provider.lookups), before)
        self.assertEqual(self.provider.destroys, [])

    def business_plan(self):
        return self.repo.create_plan(self.scope,
            {"recipe_id": "test-recipe", "request": {"model": "test-model"}},
            {"pool": "scale-test", "backend": "comfy-worker", "configuration_id": "test-manifest", "enabled": True},
            expires_at=9000, estimated_cost_microusd=0)

    def application_idle_since(self, instance):
        with self.repo.engine.connect() as connection:
            return connection.execute(select(scaler_actions.c.application_idle_since)
                .where(scaler_actions.c.intent_id == instance["id"])).scalar_one()

    def test_idle_600_starts_after_collection_finished_not_after_upstream_empty(self):
        instance = self.create()
        control, spec = self.ready_instance(instance)
        job = self.repo.create_job(self.scope, self.business_plan()["id"], "collecting-real-result")
        claim = control.claim(spec.worker_id, spec.pool, lease_seconds=3600)
        control.queue.begin_submission(claim.lease)
        control.queue.record_submitted(claim.lease, "fake-complete-upstream")
        control.queue.begin_collection(claim.lease)
        self.provider.facts[instance["id"]] = ProviderFact("running", instance["provider_instance_id"],
            idle_confirmed=True, idle_since=0)
        policy = replace(self.policy, idle_before_drain_s=600)
        self.now += 700
        self.tick(demands=[], policy=policy)
        self.assertIsNone(self.application_idle_since(instance))
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "collecting")
        # Finish the synthetic collection with a terminal fixture failure;
        # production success reaches the same terminal ledger/slot barrier.
        control.queue.fail(claim.lease, "synthetic-fixture-finished", actual_cost_microusd=0, upstream_stopped=True)
        control.observe(spec.worker_id, job["id"])
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        self.tick(demands=[], policy=policy)
        self.assertEqual(self.application_idle_since(instance), self.now)
        self.now += 599
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        self.tick(demands=[], policy=policy)
        self.assertEqual(self.provider.destroys, [])
        self.now += 1
        self.tick(demands=[], policy=policy)
        self.assertEqual(len(self.provider.destroys), 1)

    def test_new_job_resets_persisted_idle_timer_even_after_controller_replacement(self):
        instance = self.create()
        control, spec = self.ready_instance(instance)
        self.provider.facts[instance["id"]] = ProviderFact("running", instance["provider_instance_id"],
            idle_confirmed=True, idle_since=0)
        policy = replace(self.policy, idle_before_drain_s=600)
        self.tick(demands=[], policy=policy)
        self.now += 500
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        job = self.repo.create_job(self.scope, self.business_plan()["id"], "new-user-demand")
        self.tick(demands=[], policy=policy)
        self.assertIsNone(self.application_idle_since(instance))
        self.repo.request_cancel(self.scope, job["id"])
        self.now += 16
        self.tick(demands=[], policy=policy)
        self.assertEqual(self.application_idle_since(instance), self.now)
        replacement = ScaleCoordinator(self.repo, provider=self.provider, enabled=True)
        self.now += 599
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        self.tick(leader="replacement", demands=[], policy=policy, scaler=replacement)
        self.assertEqual(self.provider.destroys, [])
        self.now += 1
        self.tick(leader="replacement", demands=[], policy=policy, scaler=replacement)
        self.assertEqual(len(self.provider.destroys), 1)

    def test_stale_preflight_cannot_enqueue_after_idle_destroy_and_replay_is_preserved(self):
        instance = self.create()
        self.ready_instance(instance)
        plan = self.business_plan()
        accepted = self.repo.create_job(self.scope, plan["id"], "accepted-before-shutdown")
        self.repo.request_cancel(self.scope, accepted["id"])
        draft = self.repo.create_job(self.scope, plan["id"], "planned-before-shutdown", initial_status="planned")
        self.provider.facts[instance["id"]] = ProviderFact("running", instance["provider_instance_id"],
            idle_confirmed=True, idle_since=0)
        self.tick(demands=[], policy=replace(self.policy, idle_before_drain_s=0))
        self.assertEqual(len(self.provider.destroys), 1)
        with self.assertRaisesRegex(Conflict, "capacity_drain_repreflight"):
            self.repo.create_job(self.scope, plan["id"], "arrived-after-shutdown")
        with self.assertRaisesRegex(Conflict, "capacity_drain_repreflight"):
            self.repo.enqueue(self.scope, draft["id"])
        replay = self.repo.create_job(self.scope, plan["id"], "accepted-before-shutdown")
        self.assertEqual(replay["id"], accepted["id"])
        self.assertEqual(replay["status"], "cancelled")

    def test_existing_scaler_ledger_migration_does_not_inherit_provider_idle_time(self):
        instance = self.create()
        with self.repo.transaction() as connection:
            connection.exec_driver_sql("ALTER TABLE platform_scaler_actions DROP COLUMN application_idle_since")
        self.repo.create_schema()
        self.assertIsNone(self.application_idle_since(instance))
        self.assertEqual(self.repo.list_instance_intents()[0]["state"], "starting")

    def test_concurrent_user_submit_and_idle_shutdown_never_strand_accepted_job(self):
        instance = self.create()
        self.ready_instance(instance)
        plan = self.business_plan()
        self.provider.facts[instance["id"]] = ProviderFact("running", instance["provider_instance_id"],
            idle_confirmed=True, idle_since=0)
        barrier = threading.Barrier(2)
        def race(index):
            barrier.wait(timeout=10)
            if index == 0:
                return self.tick(demands=[], policy=replace(self.policy, idle_before_drain_s=0))
            try:
                return self.repo.create_job(self.scope, plan["id"], "concurrent-user-submit")
            except Conflict as exc:
                return {"conflict": str(exc)}
        _, submitted = self.parallel(race, count=2)
        if submitted.get("conflict"):
            self.assertEqual(submitted["conflict"], "capacity_drain_repreflight")
            self.assertEqual(len(self.provider.destroys), 1)
            self.assertIsNone(self.repo.lookup_job_by_idempotency(self.scope, "concurrent-user-submit"))
        else:
            self.assertEqual(submitted["status"], "queued")
            self.assertEqual(self.provider.destroys, [])
            self.assertEqual(self.repo.get_job(self.scope, submitted["id"])["status"], "queued")

    def test_demand_created_and_cancelled_between_polls_still_resets_idle_timer(self):
        instance = self.create()
        control, spec = self.ready_instance(instance)
        self.provider.facts[instance["id"]] = ProviderFact("running", instance["provider_instance_id"],
            idle_confirmed=True, idle_since=0)
        policy = replace(self.policy, idle_before_drain_s=600)
        self.tick(demands=[], policy=policy)
        self.now += 590
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        job = self.repo.create_job(self.scope, self.business_plan()["id"], "fast-cancel-between-polls")
        self.repo.request_cancel(self.scope, job["id"])
        self.now += 10
        self.tick(demands=[], policy=policy)
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(self.application_idle_since(instance), self.now)

    def test_every_admitted_nonterminal_state_resets_idle_but_another_ready_pool_can_serve(self):
        instance = self.create()
        control, spec = self.ready_instance(instance)
        self.provider.facts[instance["id"]] = ProviderFact("running", instance["provider_instance_id"],
            idle_confirmed=True, idle_since=0)
        plan = self.business_plan()
        job = self.repo.create_job(self.scope, plan["id"], "pending-business-demand")
        for status in ("waiting_capacity", "queued", "claimed", "submitting", "running", "collecting",
                       "checking", "submission_unknown", "cancel_requested", "recovery_hold"):
            with self.subTest(status=status):
                with self.repo.transaction() as connection:
                    connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(status=status))
                self.now += 16
                control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
                self.tick(demands=[], policy=replace(self.policy, idle_before_drain_s=0))
                self.assertIsNone(self.application_idle_since(instance))
                self.assertEqual(self.provider.destroys, [])
        with self.repo.transaction() as connection:
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="cancelled"))
        self.tick(demands=[], policy=replace(self.policy, idle_before_drain_s=0))
        self.assertEqual(len(self.provider.destroys), 1)
        replacement = replace(spec, worker_id="another-worker", instance_id="another-instance", physical_gpu_ids=("another-gpu",))
        control.register(replacement)
        control.mark_ready(replacement.worker_id, upstream_idle_confirmed=True)
        queued = self.repo.create_job(self.scope, plan["id"], "replacement-now-serves")
        claim = control.claim(replacement.worker_id, replacement.pool)
        control.observe(replacement.worker_id, claim.job["id"])
        self.assertEqual(control.get(replacement.worker_id)["state"], "busy")
        newer = self.repo.create_job(self.scope, plan["id"], "queue-while-replacement-busy")
        self.assertEqual(newer["status"], "queued")


if __name__ == "__main__":
    import unittest
    unittest.main()
