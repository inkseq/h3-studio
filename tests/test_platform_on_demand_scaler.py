"""Repeated real-job cold-start lifecycle using local ledger/fake cloud only."""
from dataclasses import asdict, replace
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch, PropertyMock

from sqlalchemy import delete, insert, select, update

from studio_platform.autoscale import ScalePolicy
from studio_platform.capabilities import compile_request
from studio_platform.capacity import transfer_unsubmitted_capacity
from studio_platform.control import WorkerControl
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.on_demand_scaler import (OnDemandConfig, OnDemandController, ServiceCycle, cycle_config,
    main, json_config, verified_service_receipt)
from studio_platform.production_scaler import MODEL, RECIPE, ScalerError, FiniteConfig
from studio_platform.queue import TaskQueue
from studio_platform.repository import Conflict, Scope, request_hash, instance_intents, attempts, capacity_waiters, jobs
from studio_platform.settings import Settings
from test_platform_api import generation_request
from test_platform_execution_policy import policy
from test_platform_production_scaler import configuration, FakeProvider, FakeBoot
from test_platform_repository import LedgerCase
from test_platform_service_policy import service_policy


class HeartbeatBoot(FakeBoot):
    def tick(self, *args, **kwargs):
        worker = self.control.get(self.worker)
        if worker["state"] != "retired" and worker["expires_at"] > self.repo.clock():
            self.control.heartbeat(self.worker, worker["fence"])
        return super().tick(*args, **kwargs)


class OnDemandTests(LedgerCase):
    def start_pending_provider(self):
        from studio_platform.scaler import ProviderFact
        self.config = replace(self.config, work_dir=self.root/'timed-provider', provider_preparation_timeout_s=120)
        original_create = self.provider.create
        def pending(tag, launch, **kwargs):
            result = original_create(tag, launch, **kwargs)
            self.provider.facts[tag] = ProviderFact('starting', result.instance_id,
                provider_status='PENDING', preparation_stage='configuring_ssh')
            return self.provider.facts[tag]
        self.provider.create = pending
        self.controller = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=HeartbeatBoot)
        self.controller.initialize()
        scope, job = self.submit()
        self.tick()
        self.tick()
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.controller.current.boots, {})
        intent = self.repo.list_instance_intents(pool=self.config.pool)[0]
        return scope, job, intent

    def test_provider_timeout_retains_job_and_bill_until_exact_removal_then_transfers_same_waiter(self):
        from studio_platform.scaler import ProviderFact
        scope, job, intent = self.start_pending_provider()
        with self.repo.engine.connect() as conn:
            waiter = dict(conn.execute(select(capacity_waiters).where(capacity_waiters.c.job_id == job['id'])).mappings().one())
        budget = self.repo.get_budget('job-budget')
        def unknown_delete(tag, instance):
            self.provider.destroys.append(tag)
            return ProviderFact('unknown', instance)
        self.provider.destroy = unknown_delete
        status = self.tick(121)
        self.assertEqual(self.provider.destroys, [intent['id']])
        self.assertEqual(status['instances'][0]['state'], 'destroying')
        self.assertEqual(self.repo.get_job(scope, job['id'])['status'], 'waiting_capacity')
        self.assertEqual(self.repo.get_job(scope, job['id'])['error_code'], 'capacity_provider_preparation_timeout')
        self.assertFalse(self.controller.stopping())
        # Restart reads the irreversible ledger phase, never repeats DELETE/start.
        self.controller = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=HeartbeatBoot)
        self.controller.initialize()
        self.tick(181)  # A restart waits for the original leader fence to expire.
        for _ in range(3): self.tick()
        self.assertEqual(self.provider.destroys, [intent['id']])
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.controller.current.boots, {})
        self.provider.facts[intent['id']] = ProviderFact('destroyed', intent['provider_instance_id'])
        self.tick()
        self.assertEqual(self.controller.sequence, 1)  # Physical removal is not settlement.
        self.assertEqual(self.repo.get_budget('finite-budget')['reserved_microusd'], 2_000_000)
        self.provider.billing = lambda *args: 100_000
        self.tick()
        self.tick()
        self.assertEqual(self.controller.sequence, 2)
        after = self.repo.get_job(scope, job['id'])
        self.assertEqual(after['request'], job['request'])
        self.assertEqual(after['id'], job['id'])
        self.assertEqual(self.repo.get_budget('job-budget'), budget)
        with self.repo.engine.connect() as conn:
            changed = conn.execute(select(capacity_waiters).where(capacity_waiters.c.job_id == job['id'])).mappings().one()
        self.assertEqual(changed['deadline'], waiter['deadline'])
        self.assertNotEqual(changed['approval_id'], waiter['approval_id'])

    def test_provider_running_race_commits_start_barrier_before_boot_and_never_pending_retires(self):
        from studio_platform.scaler import ProviderFact
        scope, job, intent = self.start_pending_provider()
        original_factory = self.controller.current.boot_factory
        def check_phase(*args, **kwargs):
            with self.repo.engine.connect() as conn:
                phase = self.controller.current.scaler.preparation(conn, intent)
            self.assertEqual(phase['phase'], 'bootstrap_started')
            return original_factory(*args, **kwargs)
        self.controller.current.boot_factory = check_phase
        self.provider.facts[intent['id']] = ProviderFact('running', intent['provider_instance_id'])
        self.tick(121)
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(len(self.controller.current.boots), 1)
        self.provider.facts[intent['id']] = ProviderFact('starting', intent['provider_instance_id'], provider_status='STOPPED')
        self.tick()
        self.assertEqual(self.provider.destroys, [])

    def test_preparation_policy_does_not_block_normal_worker_drain_after_real_job(self):
        from studio_platform.scaler import ProviderFact
        scope, job, intent = self.start_pending_provider()
        self.provider.facts[intent['id']] = ProviderFact('running', intent['provider_instance_id'])
        self.tick()
        self.tick()
        self.finish(scope, job)
        self.tick()
        for _ in range(40): self.tick()
        self.assertEqual(self.provider.destroys, [intent['id']])
        self.assertEqual(self.controller.sequence, 2)
        self.assertEqual(self.repo.get_job(scope, job['id'])['status'], 'succeeded')

    def test_provider_retirement_respects_original_waiter_deadline_while_deletion_is_unknown(self):
        from studio_platform.scaler import ProviderFact
        scope, job, intent = self.start_pending_provider()
        self.provider.destroy = lambda *args: ProviderFact('unknown', intent['provider_instance_id'])
        with self.repo.transaction() as conn:
            conn.execute(update(capacity_waiters).where(capacity_waiters.c.job_id == job['id'])
                .values(deadline=self.now+140))
        self.tick(121)
        self.assertEqual(self.repo.get_job(scope, job['id'])['status'], 'waiting_capacity')
        self.tick(20)
        current = self.repo.get_job(scope, job['id'])
        self.assertEqual((current['status'], current['error_code']), ('failed', 'capacity_wait_deadline_expired'))
        self.assertEqual(self.repo.get_budget('finite-budget')['reserved_microusd'], 2_000_000)
        self.assertEqual(len(self.provider.creates), 1)

    def test_two_paid_preparation_failures_hold_without_rerent_across_restart_and_waiter_expiry(self):
        scope, job, first = self.start_pending_provider()
        self.provider.billing = lambda tag, instance: 100_000 if instance else 0
        with self.repo.engine.connect() as conn:
            deadline = conn.execute(select(capacity_waiters.c.deadline).where(capacity_waiters.c.job_id == job['id'])).scalar_one()
        self.tick(121)
        self.assertEqual(self.controller.sequence, 2)
        self.tick()
        self.tick()
        self.assertEqual(len(self.provider.creates), 2)
        status = self.tick(121)
        self.assertEqual(status['phase'], 'awaiting_provider_repair')
        self.assertEqual(status['recovery'], {'code': 'provider_preparation_retry_limit',
            'consecutive_failures': 2, 'failure_limit': 2, 'automatic_rerent_allowed': False})
        self.assertEqual(len(self.provider.destroys), 2)
        original = self.repo.get_job(scope, job['id'])
        self.assertEqual((original['status'], original['error_code']),
            ('waiting_capacity', 'capacity_provider_preparation_retry_limit'))
        self.controller = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=HeartbeatBoot)
        self.controller.initialize()
        self.tick(181)
        for _ in range(3): self.tick()
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(self.controller.sequence, 2)
        after = self.repo.get_job(scope, job['id'])
        self.assertEqual(after['request'], job['request'])
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(capacity_waiters.c.deadline).where(
                capacity_waiters.c.job_id == job['id'])).scalar_one(), deadline)
        self.assertEqual(self.repo.get_budget('finite-budget')['spent_microusd'], 200_000)
        self.assertEqual(self.repo.get_budget('finite-budget')['reserved_microusd'], 0)
        # Test original expiry while the service still has time; no new wait window.
        with self.repo.transaction() as conn:
            conn.execute(update(capacity_waiters).where(capacity_waiters.c.job_id == job['id']).values(deadline=self.now+5))
        self.tick(6)
        self.assertEqual(self.repo.get_job(scope, job['id'])['error_code'], 'capacity_wait_deadline_expired')
        self.assertEqual(len(self.provider.creates), 2)

    def test_positive_bootstrap_transition_resets_provider_failure_streak(self):
        from studio_platform.scaler import ProviderFact
        scope, job, first = self.start_pending_provider()
        self.provider.billing = lambda tag, instance: 100_000 if instance else 0
        self.tick(121)
        self.tick()
        self.tick()
        second = self.repo.list_instance_intents(pool=self.config.pool)[-1]
        self.assertEqual(self.controller.provider_failure_count(), 1)
        self.provider.facts[second['id']] = ProviderFact('running', second['provider_instance_id'])
        self.tick()
        self.assertEqual(self.controller.provider_failure_count(), 0)
        self.assertFalse(self.controller.provider_retry_limited())

    def test_authoritative_no_rent_does_not_count_or_reset_paid_preparation_failures(self):
        from studio_platform.scaler import ProviderFact
        self.repo.configure_budget('finite-budget', tenant_id='sixnine', limit_microusd=30_000_000)
        self.configure_continuing_service()
        scope, job, first = self.start_pending_provider()
        self.provider.billing = lambda tag, instance: 100_000 if instance else 0
        self.tick(121)
        pending_create = self.provider.create
        def not_created(tag, launch, **kwargs):
            self.provider.creates.append((tag, launch))
            self.provider.facts[tag] = ProviderFact('not_created', actual_cost_microusd=0, absence_confirmed=True)
            self.provider.create = pending_create
            return self.provider.facts[tag]
        self.provider.create = not_created
        self.tick()
        self.tick()
        self.assertEqual(self.controller.provider_failure_count(), 1)
        self.tick(61)
        self.assertEqual(self.controller.sequence, 3)
        self.assertEqual(self.controller.provider_failure_count(), 1)
        self.tick()
        self.tick()
        status = self.tick(121)
        self.assertEqual(status['phase'], 'awaiting_provider_repair')
        self.assertEqual(status['recovery']['consecutive_failures'], 2)
        self.assertEqual(len(self.provider.creates), 3)  # Two paid pods plus one proved no-rent call.
        self.assertEqual(len(self.provider.destroys), 2)
        self.assertEqual(self.repo.get_job(scope, job['id'])['status'], 'waiting_capacity')

    def test_provider_removal_before_timeout_retains_job_and_cannot_bypass_failure_cap(self):
        from studio_platform.scaler import ProviderFact
        scope, job, first = self.start_pending_provider()
        self.provider.billing = lambda tag, instance: 100_000 if instance else 0
        self.provider.facts[first['id']] = ProviderFact('destroyed', first['provider_instance_id'], actual_cost_microusd=100_000)
        self.tick()  # Provider removal is earlier than the configured timeout.
        self.assertEqual(self.controller.sequence, 2)
        self.assertEqual(self.repo.get_job(scope, job['id'])['status'], 'waiting_capacity')
        self.tick()
        self.tick()
        second = self.repo.list_instance_intents(pool=self.config.pool)[-1]
        self.provider.facts[second['id']] = ProviderFact('destroyed', second['provider_instance_id'], actual_cost_microusd=100_000)
        status = self.tick()
        self.assertEqual(status['phase'], 'awaiting_provider_repair')
        self.assertEqual(status['recovery']['consecutive_failures'], 2)
        self.assertEqual(self.repo.get_job(scope, job['id'])['error_code'], 'capacity_provider_preparation_retry_limit')
        self.assertEqual(self.provider.destroys, [])  # Already removed: no DELETE is emitted.
        for _ in range(3): self.tick()
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(self.controller.current.boots, {})

    def test_provider_removal_with_conflicting_boot_evidence_holds_job_instead_of_failing_or_rerenting(self):
        from studio_platform.scaler import ProviderFact
        scope, job, first = self.start_pending_provider()
        (self.controller.current.config.work_dir/'boot'/first['id']).mkdir(parents=True)
        self.provider.facts[first['id']] = ProviderFact('destroyed', first['provider_instance_id'], actual_cost_microusd=100_000)
        for _ in range(3): status = self.tick()
        self.assertEqual(status['phase'], 'awaiting_provider_repair')
        self.assertEqual(status['recovery']['code'], 'provider_preparation_evidence_unconfirmed')
        self.assertEqual(self.repo.get_job(scope, job['id'])['status'], 'waiting_capacity')
        self.assertEqual(self.repo.get_job(scope, job['id'])['error_code'], 'capacity_rental_reconciliation')
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(len(self.provider.creates), 1)

    def test_pending_retirement_refuses_conflicting_runtime_evidence_and_legacy_without_receipt(self):
        from studio_platform.repository import scaler_receipts
        scope, job, intent = self.start_pending_provider()
        path = self.controller.current.config.work_dir/'boot'/intent['id']
        path.mkdir(parents=True)
        self.tick(121)
        self.assertEqual(self.provider.destroys, [])
        # A legacy intent with no durable start barrier remains unbound even if
        # its provider says PENDING and its current local boot directory is empty.
        with self.repo.transaction() as conn:
            conn.execute(delete(scaler_receipts).where(scaler_receipts.c.intent_id == intent['id'],
                scaler_receipts.c.operation == 'provider_preparation'))
        status = self.tick()
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(status['boot'][intent['id']]['state'], 'provider_preparation_legacy_adoption_required')
        self.assertEqual(self.repo.get_job(scope, job['id'])['status'], 'waiting_capacity')

    def test_failed_provider_can_retire_unused_but_unknown_status_cannot(self):
        from studio_platform.scaler import ProviderFact
        scope, job, intent = self.start_pending_provider()
        self.provider.facts[intent['id']] = ProviderFact('starting', intent['provider_instance_id'])
        self.tick(121)
        self.assertEqual(self.provider.destroys, [])
        self.provider.facts[intent['id']] = ProviderFact('starting', intent['provider_instance_id'], provider_status='FAILED')
        self.tick()
        self.assertEqual(self.provider.destroys, [intent['id']])
        self.assertEqual(self.repo.get_job(scope, job['id'])['error_code'], 'capacity_provider_preparation_failed')

    def test_wangp_one_use_approval_forwards_exact_engine_binding(self):
        from studio_platform.qualification_profiles import QUEUED_TASK_PROFILE
        # This tests only the coordinator seam, with no WanGP bootstrap import
        # or provider call. Source/recipe qualification is validated elsewhere.
        with patch.object(FiniteConfig, "source_names", new_callable=PropertyMock,
                          return_value={"bootstrap_cloud.py", "model_manifest.json"}):
            config = replace(cycle_config(self.config, 2), execution_backend="wangp-worker",
                engine_manifest_digest="a"*64, qualification_profile=QUEUED_TASK_PROFILE)
        cycle = ServiceCycle(self.repo, self.settings, config, provider=None)
        with patch.object(self.repo, "approve_capacity") as approve:
            cycle.initialize()
        self.assertEqual(approve.call_args.kwargs["backend"], "wangp-worker")
        self.assertEqual(approve.call_args.kwargs["engine_manifest_digest"], "a"*64)
        self.assertEqual(approve.call_args.args, (config.capacity_approval_id,))
        self.assertEqual(self.provider.creates, [])

    def configure_continuing_service(self, *, ceiling=30_000_000, idle=60, cycles=None):
        """Fresh explicit service using the same existing fake ledger accounts."""
        end = self.now+30*86400
        selected = service_policy(self.now, end=end, ceiling=ceiling, idle=idle, cycles=cycles)
        scale = {**self.config.scale_policy, "hard_deadline": end,
            "approved_remaining_microusd": ceiling, "idle_before_drain_s": idle}
        value = json.loads(json.dumps(self.value))
        value["pool"] = "continuing-pool"
        value["qualification"]["expires_at"] = end-100
        value["reservation"]["expires_at"] = end-100
        self.path.write_text(json.dumps(value))
        self.config = replace(self.config, pool=value["pool"], cycle_id="continuing-service",
            capacity_approval_id="continuing-approval", created_at=self.now, hard_deadline=end,
            work_dir=self.root/"continuing-service", scale_policy=scale, service_policy=selected,
            max_cycles=cycles, execution_policy_sha256=request_hash(value),
            manifests=[{**manifest, "approved_until": end} for manifest in self.config.manifests])
        self.value = value
        self.repo.configure_pool(self.config.pool, max_instances=1, max_physical_gpus=1)
        self.controller = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=HeartbeatBoot)
        self.controller.initialize()

    def test_continuing_service_runs_more_than_eight_rentals_without_resetting_pending_bills(self):
        self.repo.configure_budget("finite-budget", tenant_id="sixnine", limit_microusd=30_000_000)
        self.configure_continuing_service()
        original_deadline = self.config.hard_deadline
        self.assertEqual(self.provider.creates, [])
        for cycle in range(1, 10):
            scope, job = self.start_job(story="service-story-"+str(cycle))
            self.finish(scope, job)
            self.tick()
            for _ in range(6):
                status = self.tick()
            self.assertEqual(self.controller.sequence, cycle+1)
            self.assertEqual(len(self.provider.creates), cycle)
            self.assertEqual(len(self.provider.destroys), cycle)
            self.assertEqual(self.repo.get_budget("finite-budget")["reserved_microusd"], cycle*2_000_000)
            self.assertEqual(self.config.hard_deadline, original_deadline)
        self.assertIsNone(status["max_cycles"])
        self.assertEqual(status["idle_shutdown_seconds"], 60)
        before = self.repo.get_budget("finite-budget")
        self.assertTrue(verified_service_receipt(self.config))
        resumed = OnDemandController(self.repo, self.settings, self.config, provider=self.provider, boot_factory=HeartbeatBoot)
        resumed.initialize()
        self.assertEqual(resumed.sequence, 10)
        self.assertEqual(self.repo.get_budget("finite-budget"), before)
        self.assertEqual(len(self.provider.creates), 9)

    def test_continuing_ceiling_subtracts_existing_spend_and_reservations_even_with_larger_db_limit(self):
        from studio_platform.repository import budget_accounts
        self.repo.configure_budget("finite-budget", tenant_id="sixnine", limit_microusd=30_000_000)
        with self.repo.transaction() as conn:
            conn.execute(update(budget_accounts).where(budget_accounts.c.id == "finite-budget")
                .values(spent_microusd=1_000_000, reserved_microusd=500_000))
        before = self.repo.get_budget("finite-budget")
        self.configure_continuing_service(ceiling=4_000_000)
        self.assertEqual(self.controller.current.remaining_budget(), 2_500_000)
        self.assertEqual(self.repo.get_budget("finite-budget"), before)
        scope, job = self.start_job()
        self.finish(scope, job)
        self.tick()
        for _ in range(6):
            status = self.tick()
        self.assertTrue(status["drained"])
        self.assertEqual(self.controller.sequence, 1)
        self.assertEqual(len(self.provider.creates), 1)
        account = self.repo.get_budget("finite-budget")
        self.assertEqual(account["limit_microusd"], 30_000_000)
        self.assertEqual(account["spent_microusd"], 1_000_000)
        self.assertEqual(account["reserved_microusd"], 2_500_000)

    def test_continuing_unknown_rental_never_rotates_and_keeps_its_reservation(self):
        self.configure_continuing_service()
        self.provider.uncertain = self.provider.unknown = True
        self.submit()
        for _ in range(15):
            self.tick()
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.controller.sequence, 1)
        self.assertEqual(self.provider.destroys, [])
        rows = self.repo.list_instance_intents(pool=self.config.pool)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "creation_unknown")
        self.assertEqual(self.repo.get_budget("finite-budget")["reserved_microusd"], 2_000_000)

    def test_continuing_window_and_existing_wait_deadline_do_not_renew_at_restart(self):
        self.configure_continuing_service()
        scope, job = self.submit()
        with self.repo.engine.connect() as conn:
            before = dict(conn.execute(select(capacity_waiters).where(capacity_waiters.c.job_id == job["id"])).mappings().one())
        self.now = self.config.hard_deadline
        self.controller = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=HeartbeatBoot)
        self.controller.initialize()
        status = self.controller.tick()
        self.assertTrue(status["drained"])
        self.assertEqual(self.provider.creates, [])
        with self.repo.engine.connect() as conn:
            after = dict(conn.execute(select(capacity_waiters).where(capacity_waiters.c.job_id == job["id"])).mappings().one())
        self.assertEqual(after["deadline"], before["deadline"])
        self.assertEqual(after["approval_id"], before["approval_id"])
        self.assertEqual(self.repo.get_job(scope, job["id"])["id"], job["id"])

    def test_service_json_roundtrip_and_changed_authority_cannot_adopt_old_receipt(self):
        from studio_platform.on_demand_scaler import read_config
        self.configure_continuing_service()
        raw = json_config(self.config)
        self.assertEqual(request_hash(raw), self.config.fingerprint())
        path = self.root/"service-roundtrip.json"
        path.write_text(json.dumps(raw))
        path.chmod(0o600)
        self.assertEqual(read_config(path).fingerprint(), self.config.fingerprint())
        self.assertEqual(json_config(cycle_config(self.config, 1000))["service_policy"], self.config.service_policy)
        changed = replace(self.config, service_policy={**self.config.service_policy, "authorization_id": "changed"})
        with self.assertRaisesRegex(ScalerError, "configuration_changed"):
            verified_service_receipt(changed)

    def test_legacy_json_omits_new_defaults_and_keeps_same_historical_hash(self):
        raw = json_config(self.config)
        for field in ("service_policy", "execution_backend", "engine_manifest_digest"):
            self.assertNotIn(field, raw)
        self.assertEqual(request_hash(raw), self.config.fingerprint())
        with self.assertRaises(ScalerError):
            replace(self.config, max_cycles=None)

    def test_multimodal_one_use_approval_uses_both_recipes_without_renting_before_user_job(self):
        from studio_platform.qualification_profiles import MULTIMODAL_PROFILE, MULTIMODAL_INPUT_LIMITS
        value = json.loads(json.dumps(self.value))
        value["recipe_ids"] = [RECIPE, "h3-base-ref2va-v1"]
        value["qualification"].update(status="runtime_required", profile=MULTIMODAL_PROFILE)
        value["reservation"]["expected_runtime_s"] = 1800
        value["envelope"].update(max_reference_files=3, max_guides=1, allow_first_last=True,
                                 input_limits=dict(MULTIMODAL_INPUT_LIMITS))
        self.path.write_text(json.dumps(value))
        config = replace(self.config, qualification_profile=MULTIMODAL_PROFILE,
            execution_policy_sha256=request_hash(value), work_dir=self.root/"multimodal-service",
            capacity_approval_id="multimodal-approval", cycle_id="multimodal-service")
        # A separately approved empty pool avoids mutating the active old policy.
        config = replace(config, pool="multimodal-pool")
        value["pool"] = config.pool
        self.path.write_text(json.dumps(value))
        config = replace(config, execution_policy_sha256=request_hash(value))
        self.repo.configure_pool(config.pool, max_instances=1, max_physical_gpus=1)
        controller = OnDemandController(self.repo, self.settings, config, provider=self.provider, boot_factory=HeartbeatBoot)
        controller.initialize()
        from studio_platform.repository import capacity_approvals
        with self.repo.engine.connect() as conn:
            approval = conn.execute(select(capacity_approvals).where(
                capacity_approvals.c.id == controller.current.config.capacity_approval_id)).mappings().one()
        self.assertEqual(approval["payload"]["recipe_ids"], list(config.recipe_ids))
        controller.tick()
        self.assertEqual(self.provider.creates, [])
        scope = Scope("sixnine", "supervan", "reference-story")
        request = generation_request()
        request["recipe_id"] = "h3-base-ref2va-v1"
        request["inputs"] = {"images": ["image"]}
        request["controls"].update(duration=5, steps=50, resolution="768P", video_decode="tiled", encoder_device="cpu")
        compiled, fingerprint = compile_request(request, lambda _: {"metadata": {
            "kind": "image", "width": 2048, "height": 2048, "duration": None, "has_audio": False}})
        admission = ExecutionPolicies(self.settings, self.repo).evaluate(compiled, scope, fingerprint)
        self.assertTrue(admission.execution["enabled"], admission.execution)
        plan = self.repo.create_plan(scope, compiled, admission.execution, expires_at=admission.expires_at,
            estimated_cost_microusd=admission.cost)
        job = self.repo.create_job(scope, plan["id"], "reference-job", initial_status=admission.execution["admission_state"],
            budget_account_ids=admission.execution["budget_account_ids"])
        # Demand SQL sees Ref2VA queued work too; no paid submission involved.
        with self.repo.transaction() as conn:
            from studio_platform.repository import jobs
            conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="queued"))
        demands, _ = controller.current._observations([])
        self.assertEqual([d.job_id for d in demands], [job["id"]])

    def test_qualification_failure_fails_waiter_and_never_starts_another_cycle(self):
        class RejectBoot(HeartbeatBoot):
            def tick(self, *args, **kwargs):
                if kwargs.get("stopping"):
                    return super().tick(*args, **kwargs)
                return {"state": "qualification_failed"}
        self.controller.boot_factory = RejectBoot
        self.controller.current.boot_factory = RejectBoot
        scope, job = self.submit()
        for _ in range(7):
            self.tick()
        self.assertTrue(self.controller.stopping())
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.controller.sequence, 1)
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "failed")
        self.assertEqual(self.repo.get_budget("job-budget")["reserved_microusd"], 0)

    def test_default_and_multimodal_json_roundtrip_preserve_exact_fingerprint(self):
        from studio_platform.on_demand_scaler import read_config
        from studio_platform.qualification_profiles import MULTIMODAL_PROFILE
        for config in (self.config, replace(self.config, qualification_profile=MULTIMODAL_PROFILE)):
            raw = json_config(config)
            self.assertEqual(request_hash(raw), config.fingerprint())
            path = self.root/"roundtrip.json"
            path.write_text(json.dumps(raw))
            path.chmod(0o600)
            self.assertEqual(read_config(path).fingerprint(), config.fingerprint())

    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        base = configuration(self.root, self.now)
        scale = {**base.scale_policy, "max_instances": 1, "max_physical_gpus": 1, "idle_before_drain_s": 600}
        self.config = OnDemandConfig(**{**asdict(base), "work_dir": self.root/"service",
            "allowed_owners": ["superdan", "supervan"], "scale_policy": scale,
            "launches": base.launches[:1], "manifests": base.manifests[:1], "max_cycles": 2})
        self.value = policy(self.now)
        self.value.update(pool=self.config.pool, configuration_id=self.config.configuration_id,
            recipe_ids=[RECIPE], budget_accounts=["job-budget"])
        self.value["qualification"].update(evidence_id=self.config.qualification_evidence_id, expires_at=self.now+7000)
        self.value["reservation"].update(expected_runtime_s=300, expires_at=self.now+7000)
        self.value["envelope"].update(max_duration_seconds=6, max_reference_files=0, max_guides=0, allow_first_last=False)
        self.value["envelope"]["controls"].update(video_decode=["tiled"], encoder_device=["cpu"], ref_image_size=["max"])
        self.path = self.root/"policy.json"
        self.path.write_text(json.dumps(self.value))
        self.path.chmod(0o600)
        self.config = replace(self.config, execution_policy_sha256=request_hash(self.value))
        self.settings = Settings(self.config.data_dir, database_url=self.url, auth_mode="password",
            public_origin="https://www.sixnine.art", generation_enabled=True, execution_backend="comfy-worker",
            execution_policy_file=self.path)
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.repo.configure_pool(self.config.pool, max_instances=1, max_physical_gpus=1)
        self.repo.configure_budget("finite-budget", tenant_id="sixnine", limit_microusd=6_000_000)
        self.repo.configure_budget("job-budget", tenant_id="sixnine", limit_microusd=30_000_000)
        self.provider = FakeProvider(lambda: self.now)
        self.controller = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=HeartbeatBoot)
        self.controller.initialize()
        self.policies = ExecutionPolicies(self.settings, self.repo)

    def tick(self, seconds=16):
        self.now += seconds
        return self.controller.tick()

    def submit(self, owner="superdan", story="story-one"):
        scope = Scope("sixnine", owner, story)
        request = generation_request()
        request["controls"].update(duration=5, steps=50, resolution="768P", video_decode="tiled", encoder_device="cpu")
        compiled, fingerprint = compile_request(request, lambda _: None)
        admission = self.policies.evaluate(compiled, scope, fingerprint)
        self.assertTrue(admission.execution["enabled"], admission.execution)
        plan = self.repo.create_plan(scope, compiled, admission.execution, expires_at=admission.expires_at,
            estimated_cost_microusd=admission.cost)
        job = self.repo.create_job(scope, plan["id"], owner+story, initial_status=admission.execution["admission_state"],
            budget_account_ids=admission.execution["budget_account_ids"])
        return scope, job

    def finish(self, scope, job):
        boot = next(iter(self.controller.current.boots.values()))
        claim = boot.control.claim(boot.worker, self.config.pool)
        self.assertEqual(claim.job["id"], job["id"])
        queue = TaskQueue(self.repo)
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "synthetic-task")
        queue.begin_collection(claim.lease)
        queue.complete(claim.lease, [{"kind": "video", "object_key": "synthetic/result.mp4", "size_bytes": 1,
            "sha256": "a"*64, "validated": True}], actual_cost_microusd=0)
        boot.control.observe(boot.worker, job["id"])
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "succeeded")

    def start_job(self, owner="superdan", story="story-one"):
        scope, job = self.submit(owner, story)
        self.assertEqual(job["status"], "waiting_capacity")
        for _ in range(3):
            self.tick()
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "queued")
        return scope, job

    def test_no_user_job_never_creates_or_resets_budget(self):
        budget = self.repo.get_budget("finite-budget")
        for _ in range(4):
            self.assertEqual(self.tick()["phase"], "awaiting_jobs")
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.repo.get_budget("finite-budget"), budget)

    def test_inventory_wait_keeps_same_job_without_reservation_or_cycle_consumption(self):
        self.provider.preflight_availability = lambda _: "provider_inventory_unavailable"
        scope, job = self.submit()
        reserved = self.repo.get_budget("job-budget")["reserved_microusd"]
        for _ in range(8):
            value = self.tick()
        self.assertEqual(value["reason"], "provider_inventory_unavailable")
        self.assertEqual(value["phase"], "waiting_capacity")
        self.assertEqual(self.repo.get_job(scope, job["id"])["error_code"], "capacity_no_matching_gpu")
        self.assertEqual(self.controller.sequence, 1)
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.repo.list_instance_intents(), [])
        self.assertEqual(self.repo.get_budget("finite-budget")["reserved_microusd"], 0)
        self.assertEqual(self.repo.get_budget("job-budget")["reserved_microusd"], reserved)
        self.provider.preflight_availability = lambda _: None
        for _ in range(4):
            self.tick()
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "queued")
        self.assertEqual(len(self.provider.creates), 1)
        self.assertIsNone(self.repo.get_job(scope, job["id"])["error_code"])

    def test_wait_reason_never_rewrites_cancelled_job_or_accepts_raw_provider_text(self):
        scope, job = self.submit()
        cold = self.controller.current.cold
        approval = self.controller.current.config.capacity_approval_id
        cold.record_wait_reason(approval, "raw provider response must not be public")
        self.assertIsNone(self.repo.get_job(scope, job["id"])["error_code"])
        self.repo.request_cancel(scope, job["id"])
        before = self.repo.get_job(scope, job["id"])
        cold.record_wait_reason(approval, "provider_inventory_unavailable")
        after = self.repo.get_job(scope, job["id"])
        self.assertEqual(after["error_code"], before["error_code"])
        self.assertEqual(after["status"], "cancelled")

    def test_on_demand_configuration_accepts_filter_without_machine_id(self):
        manifests = json.loads(json.dumps(self.config.manifests))
        manifests[0].update(executor_id="", compatible_gpu_names=[
            "NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
            "NVIDIA RTX PRO 6000 Blackwell Server Edition"], minimum_vram_mib=95000)
        launches = [{**self.config.launches[0], "offer_id": ""}]
        config = replace(self.config, manifests=manifests, launches=launches)
        self.assertEqual(config.launches[0]["offer_id"], "")
        self.assertEqual(config.scale_policy, self.config.scale_policy)

    def test_pre_post_inventory_race_releases_rent_and_preserves_job_for_next_cycle(self):
        from studio_platform.scaler import CreationNotSubmitted
        original = self.provider.create
        def no_rent(*args, **kwargs):
            raise CreationNotSubmitted()
        self.provider.create = no_rent
        scope, job = self.submit()
        for _ in range(3):
            self.tick()
        self.assertEqual(self.controller.sequence, 1)
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "waiting_capacity")
        self.assertEqual(self.repo.get_budget("finite-budget")["reserved_microusd"], 0)
        self.assertEqual(self.repo.list_instance_intents()[0]["state"], "destroyed")
        self.provider.create = original
        for _ in range(8):
            self.tick()
        self.assertEqual(self.controller.sequence, 2)
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "queued")
        self.assertEqual(len(self.provider.creates), 1)

    def test_idle_600_seconds_removes_then_other_owner_can_start_next_cycle(self):
        scope, job = self.start_job()
        self.assertEqual(len(self.provider.creates), 1)
        self.finish(scope, job)
        self.tick()  # First whole-business idle observation starts the timer.
        for _ in range(37):
            self.tick()  # 592 seconds since idle began.
        self.assertEqual(self.provider.destroys, [])
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.provider.destroys), 1)
        self.assertEqual(self.controller.sequence, 2)
        before = self.repo.get_budget("finite-budget")
        self.assertEqual(before["reserved_microusd"], 2_000_000)  # invoice pending
        scope2, job2 = self.start_job("supervan", "another-story")
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(self.repo.get_budget("finite-budget")["reserved_microusd"], 4_000_000)
        self.assertEqual(self.repo.get_budget("finite-budget")["limit_microusd"], before["limit_microusd"])
        self.assertEqual(self.repo.get_job(scope2, job2["id"])["status"], "queued")

    def assert_idle_draft_allows_next_real_job(self, status):
        scope, finished = self.start_job()
        self.finish(scope, finished)
        draft = self.repo.create_job(scope, finished["plan_id"], "unaccepted-"+status, initial_status=status)
        original = self.repo.get_job(scope, draft["id"])
        budget = self.repo.get_budget("finite-budget")
        job_budget = self.repo.get_budget("job-budget")
        config_hash = self.config.fingerprint()
        self.tick()
        for _ in range(40):
            self.tick()
        self.assertEqual(len(self.provider.destroys), 1)
        self.assertEqual(self.controller.sequence, 2)
        self.assertEqual(len(self.provider.creates), 1)  # No demand from a draft.
        self.assertFalse(self.controller.stopping())
        self.assertEqual(self.repo.get_job(scope, draft["id"]), original)
        self.assertEqual(self.repo.get_budget("finite-budget"), budget)
        self.assertEqual(self.repo.get_budget("job-budget"), job_budget)
        self.assertEqual(self.controller.config.fingerprint(), config_hash)
        next_scope, next_job = self.start_job("supervan", "after-"+status)
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(self.repo.get_job(next_scope, next_job["id"])["status"], "queued")
        self.assertEqual(self.repo.get_job(scope, draft["id"]), original)
        self.assertEqual(self.repo.get_budget("finite-budget")["limit_microusd"], budget["limit_microusd"])
        self.assertEqual(self.controller.config.hard_deadline, self.config.hard_deadline)

    def test_unaccepted_planned_draft_does_not_block_idle_cycle_or_next_job(self):
        self.assert_idle_draft_allows_next_real_job("planned")

    def test_unaccepted_blocked_draft_does_not_block_idle_cycle_or_next_job(self):
        self.assert_idle_draft_allows_next_real_job("blocked")

    def test_draft_label_cannot_hide_any_lease_current_attempt_or_attempt_history(self):
        scope, finished = self.start_job()
        self.finish(scope, finished)
        worker = next(iter(self.controller.current.boots.values())).worker
        draft = self.repo.create_job(scope, finished["plan_id"], "unsafe-draft", initial_status="planned")
        # Keep the old cycle around after its real idle shutdown, so each proof
        # checks an authoritative destroyed instance and retired child.
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == draft["id"]).values(lease_worker_id=worker))
        self.tick()
        for _ in range(40):
            self.tick()
        self.assertEqual(len(self.provider.destroys), 1)
        self.assertEqual(self.controller.sequence, 1)
        next_cycle = ServiceCycle(self.repo, self.settings, cycle_config(self.config, 2), provider=self.provider)
        next_cycle.initialize()
        previous_id = self.controller.current.config.capacity_approval_id
        self.repo.set_capacity_approval_enabled(previous_id, enabled=False)
        budget = self.repo.get_budget("finite-budget")
        job_budget = self.repo.get_budget("job-budget")
        attempt_id = "00000000-0000-0000-0000-000000000123"
        cases = (
            ("attempt-counter", {"attempt_no": 1}, None),
            ("current-attempt", {"current_attempt_id": attempt_id}, None),
            ("lease-worker", {"lease_worker_id": worker}, None),
            ("expired-lease", {"lease_expires_at": self.now-1}, None),
            ("historical-attempt", {}, {"status": "deferred", "upstream_stopped": 1}),
            ("historical-submission", {}, {"status": "unknown", "submission_started_at": self.now,
                "upstream_task_id": "original-paid-task", "upstream_stopped": 0}),
            ("historical-waiter", {}, None),
        )
        for status in ("planned", "blocked"):
            for name, overrides, history in cases:
                with self.subTest(status=status, evidence=name):
                    with self.repo.transaction() as conn:
                        conn.execute(delete(attempts).where(attempts.c.job_id == draft["id"]))
                        conn.execute(delete(capacity_waiters).where(capacity_waiters.c.job_id == draft["id"]))
                        conn.execute(update(jobs).where(jobs.c.id == draft["id"]).values(
                            **{"status": status, "attempt_no": 0, "current_attempt_id": None,
                               "lease_worker_id": None, "lease_expires_at": None, **overrides}))
                        if history:
                            conn.execute(insert(attempts).values(id=attempt_id, job_id=draft["id"],
                                number=1, fence=0, worker_id=worker, created_at=self.now, updated_at=self.now, **history))
                        if name == "historical-waiter":
                            old_waiter = conn.execute(select(capacity_waiters).where(
                                capacity_waiters.c.job_id == finished["id"])).mappings().one()
                            conn.execute(insert(capacity_waiters).values(**{**dict(old_waiter),
                                "job_id": draft["id"], "state": "failed"}))
                    value = self.controller.current.status()
                    self.assertTrue(value["all_destroyed"])
                    self.assertIn(draft["id"], value["active_job_ids"])
                    self.assertFalse(self.controller._can_rotate(value))
                    with self.assertRaisesRegex(Conflict, "capacity_transfer_job_requires_reconciliation"):
                        transfer_unsubmitted_capacity(self.repo, previous_id, next_cycle.config.capacity_approval_id,
                            allowed_owners=self.config.allowed_owners, children_done_confirmed=True)
                    self.assertEqual(self.controller.sequence, 1)
                    self.assertEqual(len(self.provider.creates), 1)
                    self.assertEqual(self.repo.get_budget("finite-budget"), budget)
                    self.assertEqual(self.repo.get_budget("job-budget"), job_budget)

    def test_terminal_job_with_unresolved_submission_still_blocks_rotation(self):
        scope, finished = self.start_job()
        self.finish(scope, finished)
        worker = next(iter(self.controller.current.boots.values())).worker
        draft = self.repo.create_job(scope, finished["plan_id"], "hold-cycle-for-audit", initial_status="planned")
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == draft["id"]).values(lease_worker_id=worker))
        self.tick()
        for _ in range(40):
            self.tick()
        self.assertEqual(len(self.provider.destroys), 1)
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == draft["id"]).values(lease_worker_id=None))
            conn.execute(update(jobs).where(jobs.c.id == finished["id"]).values(status="failed"))
            conn.execute(update(attempts).where(attempts.c.job_id == finished["id"]).values(
                status="unknown", upstream_stopped=0))
        value = self.controller.current.status()
        self.assertTrue(value["all_destroyed"])
        self.assertIn(finished["id"], value["active_job_ids"])
        self.assertFalse(self.controller._can_rotate(value))
        self.assertEqual(self.controller.sequence, 1)
        self.assertEqual(len(self.provider.creates), 1)

    def test_warm_admitted_job_cannot_hide_reservation_with_a_draft_label(self):
        first = self.start_job()
        self.finish(*first)
        scope, accepted = self.submit("supervan", "accepted-on-warm-gpu")
        self.assertEqual(accepted["status"], "queued")
        # Warm admission reserves budget without a cold waiter or attempt.
        with self.repo.transaction() as conn:
            self.assertIsNone(conn.execute(select(capacity_waiters.c.job_id).where(
                capacity_waiters.c.job_id == accepted["id"])).first())
            self.assertIsNone(conn.execute(select(attempts.c.id).where(
                attempts.c.job_id == accepted["id"])).first())
            conn.execute(update(jobs).where(jobs.c.id == accepted["id"]).values(status="planned"))
        budget = self.repo.get_budget("job-budget")
        self.assertGreater(budget["reserved_microusd"], 0)
        self.tick()
        for _ in range(40):
            self.tick()
        self.assertEqual(len(self.provider.destroys), 1)
        self.assertEqual(self.controller.sequence, 1)
        next_cycle = ServiceCycle(self.repo, self.settings, cycle_config(self.config, 2), provider=self.provider)
        next_cycle.initialize()
        previous_id = self.controller.current.config.capacity_approval_id
        self.repo.set_capacity_approval_enabled(previous_id, enabled=False)
        for status in ("planned", "blocked"):
            with self.subTest(status=status):
                with self.repo.transaction() as conn:
                    conn.execute(update(jobs).where(jobs.c.id == accepted["id"]).values(status=status))
                value = self.controller.current.status()
                self.assertFalse(self.controller._can_rotate(value))
                with self.assertRaisesRegex(Conflict, "capacity_transfer_job_requires_reconciliation"):
                    transfer_unsubmitted_capacity(self.repo, previous_id, next_cycle.config.capacity_approval_id,
                        allowed_owners=self.config.allowed_owners, children_done_confirmed=True)
                self.assertEqual(self.repo.get_job(scope, accepted["id"])["status"], status)
                self.assertEqual(self.repo.get_budget("job-budget"), budget)
                self.assertEqual(len(self.provider.creates), 1)

    def test_unknown_creation_does_not_rotate_or_rerent(self):
        self.provider.uncertain = self.provider.unknown = True
        self.submit()
        for _ in range(8):
            self.tick()
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.controller.sequence, 1)
        self.assertEqual(self.repo.list_instance_intents()[0]["state"], "creation_unknown")
        self.assertEqual(self.provider.destroys, [])
        status = self.controller.status(fresh_ledger_only=True)
        self.assertEqual(status["reason"], "creation_needs_reconciliation")
        self.assertFalse(status["recovery"]["automatic_rerent_allowed"])

    def test_next_job_resets_idle_timer_and_reuses_same_gpu(self):
        first = self.start_job()
        self.finish(*first)
        self.tick()
        for _ in range(37):
            self.tick()
        second = self.submit("supervan", "second-story")
        self.assertEqual(second[1]["status"], "queued")
        self.tick()  # New queued work resets the whole-business idle timer.
        self.assertEqual(self.provider.destroys, [])
        self.finish(*second)
        self.tick()
        for _ in range(37):
            self.tick()
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(len(self.provider.creates), 1)
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.provider.destroys), 1)

    def test_other_leader_does_not_turn_compact_status_into_drain_or_creation(self):
        lease = self.controller.current.scaler.acquire(self.config.pool, "another-controller")
        self.assertIsNotNone(lease)
        value = self.tick()
        self.assertFalse(self.controller.stopping())
        self.assertFalse(value["drained"])
        self.assertEqual(self.provider.creates, [])

    def shorten_current_ttl(self, seconds=160):
        row = self.repo.list_instance_intents(pool=self.config.pool)[-1]
        with self.repo.transaction() as conn:
            conn.execute(update(instance_intents).where(instance_intents.c.id == row["id"])
                .values(hard_deadline=self.now+seconds))
        return row

    def test_ttl_rollover_preserves_queued_job_id_budget_and_running_result(self):
        first = self.start_job()
        second_scope, second = self.submit("supervan", "queued-at-ttl")
        boot = next(iter(self.controller.current.boots.values()))
        claim = boot.control.claim(boot.worker, self.config.pool)
        queue = TaskQueue(self.repo)
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "original-paid-task")
        old = self.shorten_current_ttl()
        lease = claim.lease
        for _ in range(12):
            self.tick()
            lease = queue.heartbeat(lease)
        self.assertEqual(self.controller.sequence, 1)
        self.assertEqual(self.provider.destroys, [])
        self.assertEqual(self.repo.get_job(second_scope, second["id"])["status"], "queued")
        self.assertEqual(self.repo.get_job(first[0], first[1]["id"])["status"], "running")
        queue.begin_collection(lease)
        queue.complete(lease, [{"kind": "video", "object_key": "synthetic/original.mp4",
            "size_bytes": 1, "sha256": "b"*64, "validated": True}], actual_cost_microusd=0)
        boot.control.observe(boot.worker, first[1]["id"])
        reserved_before = self.repo.get_budget("job-budget")["reserved_microusd"]
        self.tick()
        self.assertEqual(self.controller.sequence, 2)
        self.assertEqual(len(self.provider.creates), 1)
        moved = self.repo.get_job(second_scope, second["id"])
        self.assertEqual(moved["status"], "waiting_capacity")
        self.assertEqual(moved["attempt_no"], 0)
        self.assertEqual(self.repo.get_budget("job-budget")["reserved_microusd"], reserved_before)
        self.assertEqual(moved["execution_plan"]["capacity_approval_id"], self.controller.current.config.capacity_approval_id)
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(self.repo.get_job(second_scope, second["id"])["status"], "queued")
        with self.repo.engine.connect() as conn:
            paid = list(conn.execute(select(attempts).where(attempts.c.job_id == first[1]["id"])).mappings())
        self.assertEqual(len(paid), 1)
        self.assertEqual(paid[0]["upstream_task_id"], "original-paid-task")

    def test_ttl_rollover_preserves_deferred_unsubmitted_attempt_history(self):
        scope, job = self.start_job()
        boot = next(iter(self.controller.current.boots.values()))
        claim = boot.control.claim(boot.worker, self.config.pool)
        TaskQueue(self.repo).defer_unsubmitted(claim.lease, retry_after_s=0)
        boot.control.observe(boot.worker, job["id"])
        self.shorten_current_ttl()
        for _ in range(12):
            self.tick()
        self.assertEqual(self.controller.sequence, 2)
        moved = self.repo.get_job(scope, job["id"])
        self.assertEqual(moved["current_attempt_id"], claim.lease.attempt_id)
        self.assertEqual(moved["attempt_no"], 1)
        self.assertEqual(moved["status"], "queued")
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.provider.creates), 2)
        with self.repo.engine.connect() as conn:
            history = list(conn.execute(select(attempts).where(attempts.c.job_id == job["id"])).mappings())
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0]["submission_started_at"])
        self.assertEqual(history[0]["status"], "deferred")

    def test_ttl_rollover_budget_exhaustion_cancels_pending_without_second_rental(self):
        self.repo.configure_budget("finite-budget", tenant_id="sixnine", limit_microusd=2_000_000)
        first = self.start_job()
        second_scope, second = self.submit("supervan", "budget-limited")
        self.finish(*first)
        self.shorten_current_ttl()
        for _ in range(13):
            value = self.tick()
        self.assertTrue(self.controller.stopping())
        self.assertTrue(value["drained"])
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.repo.get_job(second_scope, second["id"])["status"], "cancelled")
        self.assertEqual(self.repo.get_budget("job-budget")["reserved_microusd"], 0)

    def test_revoked_grant_is_service_stop_not_new_automatic_grant(self):
        job = self.start_job()
        self.repo.set_capacity_approval_enabled(self.controller.current.config.capacity_approval_id, enabled=False)
        value = self.tick()
        self.assertTrue(self.controller.stopping())
        self.assertEqual(self.controller.sequence, 1)
        self.assertEqual(len(self.provider.creates), 1)

    def run_cli_offline(self):
        """Probe CLI boundaries without a database/SSH/provider connection."""
        config_path = self.root/"cli-operator.json"
        config_path.write_text(json.dumps(json_config(self.config)))
        settings = replace(self.settings, database_url="postgresql+psycopg://localhost/synthetic")
        from studio_platform.production_scaler import validate_settings
        controller = SimpleNamespace(initialize=lambda: None,
            tick=lambda: {"phase": "drained", "drained": True, "sequence": 1, "billing_pending": 0})
        output = io.StringIO()
        with patch("studio_platform.on_demand_scaler.Settings.from_environment", return_value=settings), \
                patch("studio_platform.on_demand_scaler.validate_settings", wraps=validate_settings) as validate, \
                patch("studio_platform.on_demand_scaler.Repository") as repo, \
                patch("studio_platform.on_demand_scaler.verify_sources") as sources, \
                patch("studio_platform.on_demand_scaler.verify_identity_files") as identity, \
                patch("studio_platform.on_demand_scaler.AwsLiumLoader") as loader, \
                patch("studio_platform.on_demand_scaler.LiumProvider") as provider, \
                patch("studio_platform.on_demand_scaler.OnDemandController", return_value=controller), \
                contextlib.redirect_stdout(output):
            code = main(["--config", str(config_path), "--enabled"])
        return code, validate, repo, sources, identity, loader, provider

    def test_exact_prior_receipt_allows_cleanup_entry_after_policy_changed(self):
        self.path.write_text(json.dumps({**self.value, "enabled": False}))
        self.assertTrue(verified_service_receipt(self.config))
        code, validate, repo, sources, identity, loader, provider = self.run_cli_offline()
        self.assertEqual(code, 0)
        self.assertFalse(validate.call_args.kwargs["require_policy"])
        for check in (repo, sources, identity, loader, provider):
            self.assertEqual(check.call_count, 1)

    def test_new_start_policy_changed_fails_before_provider_or_database(self):
        self.controller.receipt_path.unlink()
        self.path.write_text(json.dumps({**self.value, "enabled": False}))
        code, validate, repo, sources, identity, loader, provider = self.run_cli_offline()
        self.assertEqual(code, 1)
        self.assertTrue(validate.call_args.kwargs["require_policy"])
        for boundary in (repo, sources, identity, loader, provider):
            self.assertEqual(boundary.call_count, 0)

    def test_wrong_receipt_hash_never_bypasses_policy_or_loads_runtime(self):
        value = json.loads(self.controller.receipt_path.read_text())
        value["config_hash"] = "0"*64
        self.controller.receipt_path.write_text(json.dumps(value))
        code, validate, repo, sources, identity, loader, provider = self.run_cli_offline()
        self.assertEqual(code, 1)
        for boundary in (validate, repo, sources, identity, loader, provider):
            self.assertEqual(boundary.call_count, 0)

    def test_restart_with_missing_policy_only_drains_existing_rental(self):
        scope, job = self.start_job()
        self.path.unlink()
        restart = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=HeartbeatBoot)
        # Synthetic process handoff uses the same leader; no elapsed fake-time
        # lease expiry is mistaken for a dead worker or an idle proof.
        restart.leader_id = self.controller.leader_id
        restart.initialize()
        result = restart.tick()
        self.assertTrue(restart.stopping())
        self.assertTrue(result["drained"])
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(len(self.provider.destroys), 1)
        self.assertEqual(self.repo.get_job(scope, job["id"])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
