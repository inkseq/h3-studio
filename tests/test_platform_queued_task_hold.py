"""Real-queue failure isolation with local SQL and a fake cloud; never rents."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import unittest

from sqlalchemy import select, update

from studio_platform.capabilities import compile_request
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.on_demand_scaler import OnDemandConfig, OnDemandController
from studio_platform.production_scaler import FiniteController, RECIPE, save
from studio_platform.qualification_profiles import QUEUED_TASK_PROFILE, MULTIMODAL_INPUT_LIMITS
from studio_platform.queue import TaskQueue
from studio_platform.repository import Scope, attempts, capacity_waiters, jobs, request_hash
from studio_platform.settings import Settings
from test_platform_api import generation_request
from test_platform_production_scaler import configuration, FakeBoot, FakeProvider
from test_platform_repository import LedgerCase


class QueuedBoot(FakeBoot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        directory = self.config.work_dir/'boot'/self.intent['id']
        directory.mkdir(parents=True, exist_ok=True)
        save(directory/'bootstrap-state.json', {
            'identity': {'intent_id': self.intent['id'], 'instance_id': self.intent['provider_instance_id'],
                'configuration_id': self.config.configuration_id, 'sources': self.config.source_sha256},
            'phase': 'fleet_started', 'qualification_profile': QUEUED_TASK_PROFILE,
            'runtime_validation': {'profile': QUEUED_TASK_PROFILE, 'state': 'runtime_ready', 'generation_verified': False}})

    def tick(self, *args, **kwargs):
        result = super().tick(*args, **kwargs)
        if self.control.get(self.worker)['drain_requested'] and not kwargs.get('stopping'):
            return {'state': 'fleet_attention_required'}
        return result


class QueuedTaskHoldTests(LedgerCase):
    def setUp(self):
        super().setUp()
        root = Path(self.temp.name)
        base = configuration(root, self.now)
        scale = {**base.scale_policy, 'max_instances': 1, 'max_physical_gpus': 1, 'idle_before_drain_s': 600}
        self.config = OnDemandConfig(**{**asdict(base), 'work_dir': root/'service',
            'qualification_profile': QUEUED_TASK_PROFILE, 'allowed_owners': ['superdan', 'supervan'],
            'scale_policy': scale, 'launches': base.launches[:1], 'manifests': base.manifests[:1], 'max_cycles': 2})
        from test_platform_execution_policy import policy
        value = policy(self.now)
        value.update(pool=self.config.pool, configuration_id=self.config.configuration_id,
            recipe_ids=list(self.config.recipe_ids), budget_accounts=['job-budget'])
        value['qualification'].update(status='runtime_required', profile=QUEUED_TASK_PROFILE,
            evidence_id=self.config.qualification_evidence_id, expires_at=self.now+7000)
        value['reservation'].update(expected_runtime_s=1800, expires_at=self.now+7000)
        value['envelope'].update(max_duration_seconds=6, max_reference_files=3, max_guides=1,
            allow_first_last=True, input_limits=dict(MULTIMODAL_INPUT_LIMITS))
        value['envelope']['controls'].update(video_decode=['tiled'], encoder_device=['cpu'], ref_image_size=['max'])
        policy_path = root/'policy.json'
        policy_path.write_text(json.dumps(value)); policy_path.chmod(0o600)
        self.config = replace(self.config, execution_policy_sha256=request_hash(value))
        self.settings = Settings(self.config.data_dir, database_url=self.url, auth_mode='password',
            public_origin='https://www.sixnine.art', generation_enabled=True, execution_backend='comfy-worker',
            execution_policy_file=policy_path)
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.repo.configure_pool(self.config.pool, max_instances=1, max_physical_gpus=1)
        self.repo.configure_budget('finite-budget', tenant_id='sixnine', limit_microusd=6_000_000)
        self.repo.configure_budget('job-budget', tenant_id='sixnine', limit_microusd=30_000_000)
        self.provider = FakeProvider(lambda: self.now)
        self.controller = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=QueuedBoot)
        self.controller.initialize()

    def submit(self, owner, story):
        scope = Scope('sixnine', owner, story)
        request = generation_request()
        request['controls'].update(duration=5, steps=50, resolution='768P', video_decode='tiled', encoder_device='cpu')
        compiled, fingerprint = compile_request(request, lambda _: None)
        admission = ExecutionPolicies(self.settings, self.repo).evaluate(compiled, scope, fingerprint)
        self.assertTrue(admission.execution['enabled'], admission.execution)
        plan = self.repo.create_plan(scope, compiled, admission.execution, expires_at=admission.expires_at,
            estimated_cost_microusd=admission.cost)
        job = self.repo.create_job(scope, plan['id'], owner+story, initial_status=admission.execution['admission_state'],
            budget_account_ids=admission.execution['budget_account_ids'])
        return scope, job

    def tick(self):
        self.now += 16
        return self.controller.tick()

    def start(self):
        first = self.submit('superdan', 'first')
        self.now += 1  # Distinct acceptance order, independent of random UUIDs.
        second = self.submit('supervan', 'other-accepted-story')
        for _ in range(3):
            self.tick()
        self.boot = next(iter(self.controller.current.boots.values()))
        claim = self.boot.control.claim(self.boot.worker, self.config.pool)
        self.assertEqual(claim.job['id'], first[1]['id'])
        return first, second, claim

    def fail_first(self, claim, *, preparation=False):
        queue = TaskQueue(self.repo)
        if preparation:
            queue.defer_unsubmitted(claim.lease, error_code='worker_preparation_failed')
        else:
            queue.begin_submission(claim.lease)
            queue.record_submitted(claim.lease, 'one-original-upstream-task')
            queue.fail(claim.lease, 'upstream_generation_failed', actual_cost_microusd=None, upstream_stopped=True)
        self.boot.control.observe(self.boot.worker, claim.job['id'], quarantine_failures=True)

    def test_real_failure_keeps_other_accepted_job_and_budget_without_rerent(self):
        first, second, claim = self.start()
        self.fail_first(claim)
        before = self.repo.get_job(second[0], second[1]['id'])
        budget = self.repo.get_budget('job-budget')
        config_hash = self.config.fingerprint()
        for _ in range(8):
            value = self.tick()
        after = self.repo.get_job(second[0], second[1]['id'])
        self.assertEqual(after['status'], before['status'])
        self.assertEqual(after['request_hash'], before['request_hash'])
        self.assertEqual(after['attempt_no'], 0)
        self.assertEqual(after['error_code'], 'capacity_queued_task_repair_required')
        self.assertEqual(self.repo.get_budget('job-budget'), budget)
        self.assertEqual(value['phase'], 'awaiting_repair')
        self.assertFalse(self.controller.stopping())
        self.assertEqual(self.controller.sequence, 1)
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(len(self.provider.destroys), 1)
        self.assertEqual(self.config.fingerprint(), config_hash)
        self.assertEqual(self.repo.get_job(first[0], first[1]['id'])['status'], 'failed')

    def test_preparation_failure_has_no_submission_and_keeps_both_jobs(self):
        first, second, claim = self.start()
        self.fail_first(claim, preparation=True)
        budget = self.repo.get_budget('job-budget')
        for _ in range(4):
            self.tick()
        self.assertEqual(self.repo.get_job(first[0], first[1]['id'])['status'], 'queued')
        self.assertEqual(self.repo.get_job(second[0], second[1]['id'])['status'], 'queued')
        self.assertEqual(self.repo.get_budget('job-budget'), budget)
        with self.repo.engine.connect() as conn:
            attempt = conn.execute(select(attempts).where(attempts.c.id == claim.lease.attempt_id)).mappings().one()
        self.assertIsNone(attempt['submission_started_at'])
        self.assertIsNone(attempt['upstream_task_id'])
        self.assertEqual(len(self.provider.creates), 1)

    def test_hold_retains_original_wait_deadline_and_cancellation(self):
        _, second, claim = self.start()
        self.fail_first(claim)
        self.tick()
        with self.repo.engine.connect() as conn:
            deadline = conn.execute(select(capacity_waiters.c.deadline).where(
                capacity_waiters.c.job_id == second[1]['id'])).scalar_one()
        self.now = deadline
        self.tick()
        self.assertEqual(self.repo.get_job(second[0], second[1]['id'])['status'], 'failed')
        self.assertEqual(self.repo.get_job(second[0], second[1]['id'])['error_code'], 'capacity_wait_deadline_expired')
        self.assertEqual(len(self.provider.creates), 1)

    def test_user_can_cancel_held_other_job(self):
        _, second, claim = self.start()
        self.fail_first(claim)
        self.tick()
        self.repo.request_cancel(second[0], second[1]['id'])
        for _ in range(3):
            self.tick()
        self.assertEqual(self.repo.get_job(second[0], second[1]['id'])['status'], 'cancelled')
        self.assertEqual(len(self.provider.creates), 1)

    def test_restart_missing_success_proof_does_not_clear_hold_or_rent_again(self):
        _, second, claim = self.start()
        self.fail_first(claim)
        for _ in range(5):
            self.tick()
        restarted = OnDemandController(self.repo, self.settings, self.config,
            provider=self.provider, boot_factory=QueuedBoot)
        restarted.leader_id = self.controller.leader_id
        restarted.initialize()
        value = restarted.tick()
        self.assertEqual(value['phase'], 'awaiting_repair')
        self.assertEqual(restarted.sequence, 1)
        self.assertEqual(self.repo.get_job(second[0], second[1]['id'])['status'], 'queued')
        self.assertEqual(len(self.provider.creates), 1)

    def test_unknown_submission_or_wrong_boot_identity_is_not_failed_worker_proof(self):
        _, second, claim = self.start()
        queue = TaskQueue(self.repo)
        queue.begin_submission(claim.lease)
        queue.mark_submission_unknown(claim.lease)
        self.boot.control.drain(self.boot.worker)
        current = self.controller.current
        self.assertFalse(current._queued_task_failure_hold(self.boot.intent))
        self.assertIsNone(current.preparation_hold())
        self.assertEqual(self.repo.get_job(second[0], second[1]['id'])['status'], 'queued')
        self.assertEqual(len(self.provider.creates), 1)
        path = current.config.work_dir/'boot'/self.boot.intent['id']/'bootstrap-state.json'
        value = json.loads(path.read_text()); value['identity']['instance_id'] = 'wrong-instance'
        path.write_text(json.dumps(value))
        self.assertFalse(current._queued_task_failure_hold(self.boot.intent))

    def test_unconfirmed_evidence_holds_unknown_without_reclassifying_or_destroying_it(self):
        first, second, claim = self.start()
        queue = TaskQueue(self.repo)
        queue.begin_submission(claim.lease)
        queue.mark_submission_unknown(claim.lease)
        self.boot.control.drain(self.boot.worker)
        current = self.controller.current
        current._boot_failure(self.boot.intent, {'state': 'fleet_attention_required',
            'error_code': 'finite_real_task_evidence_unconfirmed'})
        self.assertEqual(current.preparation_hold()['verification_state'], 'evidence_unconfirmed_not_upstream_stopped')
        budget = self.repo.get_budget('job-budget')
        for _ in range(4):
            self.tick()
        self.assertEqual(self.repo.get_job(first[0], first[1]['id'])['status'], 'submission_unknown')
        self.assertEqual(self.boot.control.get(self.boot.worker)['current_job_id'], first[1]['id'])
        self.assertEqual(self.repo.get_job(second[0], second[1]['id'])['status'], 'queued')
        with self.repo.engine.connect() as conn:
            attempt = conn.execute(select(attempts).where(attempts.c.id == claim.lease.attempt_id)).mappings().one()
        self.assertEqual(attempt['upstream_stopped'], 0)
        self.assertEqual(self.repo.get_budget('job-budget'), budget)
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.provider.destroys, [])

    def test_verified_user_result_survives_unconfirmed_control_receipt_hold(self):
        first, second, claim = self.start()
        queue = TaskQueue(self.repo)
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, 'one-original-upstream-task')
        queue.begin_collection(claim.lease)
        queue.complete(claim.lease, [{'kind': 'video', 'object_key': 'synthetic/result.mp4',
            'size_bytes': 1, 'sha256': 'a'*64, 'validated': True}], actual_cost_microusd=None)
        self.boot.control.observe(self.boot.worker, first[1]['id'])
        self.boot.control.drain(self.boot.worker)
        self.controller.current._boot_failure(self.boot.intent, {'state': 'fleet_attention_required',
            'error_code': 'finite_real_task_evidence_unconfirmed'})
        result = self.repo.get_job(first[0], first[1]['id'])['result']
        for _ in range(4):
            self.tick()
        self.assertEqual(self.repo.get_job(first[0], first[1]['id'])['status'], 'succeeded')
        self.assertEqual(self.repo.get_job(first[0], first[1]['id'])['result'], result)
        self.assertEqual(self.repo.get_job(second[0], second[1]['id'])['status'], 'queued')
        self.assertEqual(len(self.provider.creates), 1)

    def test_finite_profile_also_preserves_unsubmitted_queue_and_honors_saved_deadline(self):
        _, second, claim = self.start()
        self.fail_first(claim)
        current = self.controller.current
        before = self.repo.get_job(second[0], second[1]['id'])
        budget = self.repo.get_budget('job-budget')
        FiniteController._close_unsubmitted(current)
        after = self.repo.get_job(second[0], second[1]['id'])
        self.assertEqual(after['status'], 'queued')
        self.assertEqual(after['request_hash'], before['request_hash'])
        self.assertEqual(self.repo.get_budget('job-budget'), budget)
        with self.repo.engine.connect() as conn:
            deadline = conn.execute(select(capacity_waiters.c.deadline).where(
                capacity_waiters.c.job_id == second[1]['id'])).scalar_one()
        self.now = deadline
        FiniteController._close_unsubmitted(current)
        self.assertEqual(self.repo.get_job(second[0], second[1]['id'])['error_code'], 'capacity_wait_deadline_expired')

    def test_warm_queue_without_waiter_expires_at_saved_execution_window(self):
        first = self.submit('superdan', 'first')
        for _ in range(3):
            self.tick()
        warm = self.submit('supervan', 'warm-accepted')
        with self.repo.engine.connect() as conn:
            self.assertIsNone(conn.execute(select(capacity_waiters.c.job_id).where(
                capacity_waiters.c.job_id == warm[1]['id'])).first())
        self.boot = next(iter(self.controller.current.boots.values()))
        claim = self.boot.control.claim(self.boot.worker, self.config.pool)
        self.assertEqual(claim.job['id'], first[1]['id'])
        self.fail_first(claim)
        self.tick()
        self.assertEqual(self.repo.get_job(warm[0], warm[1]['id'])['status'], 'queued')
        self.now = self.config.hard_deadline - warm[1]['expected_runtime_s']
        self.controller.current.hold_queued_task_backlog()
        self.assertEqual(self.repo.get_job(warm[0], warm[1]['id'])['error_code'], 'capacity_wait_deadline_expired')
        self.assertEqual(len(self.provider.creates), 1)


if __name__ == '__main__':
    unittest.main()
