"""Offline durable admission/drain and real OS collection-lock tests; no GPU."""
import multiprocessing
from pathlib import Path
import threading
from unittest.mock import patch

from sqlalchemy import and_

from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.drain_safe_runner import DrainSafeRunner, collection_slot
from studio_platform.repository import LeaseLost, Scope, jobs
from studio_platform.worker import Outcome, WorkerRunner, _slot_lock
from test_platform_repository import LedgerCase


def hold_collection_lock(directory, acquired, release):
    with collection_slot(Path(directory), lambda: None):
        acquired.set()
        release.wait(10)


class Backend:
    enabled, kind, slot_key = True, 'comfy-worker', 'synthetic-gpu-endpoint'

    def __init__(self):
        self.submits = 0
        self.unknown = False
        self.state = 'running'

    def prepare(self, job, tag, store, heartbeat):
        heartbeat()
        return {}

    def submit(self, prepared, tag):
        self.submits += 1
        if self.unknown:
            raise TimeoutError('synthetic ambiguous POST')
        return 'synthetic-upstream'

    def reconcile(self, tag):
        return Outcome('running', None if self.unknown else 'synthetic-upstream')

    def poll(self, tag, task):
        return Outcome(self.state, task)


class DrainSafeRunnerTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.work = Path(self.temp.name)/'worker'
        self.media_lock = Path(self.temp.name)/'shared-collection'
        self.backend = Backend()
        self.control = WorkerControl(self.repo)
        self.control.register(WorkerSpec('worker', 'finite-pool', 'synthetic-provider', 'synthetic-instance',
            ('synthetic-device',), ('synthetic-recipe',), 'synthetic-model', 'synthetic-config'))
        self.control.mark_ready('worker', upstream_idle_confirmed=True)
        self.stopping = False

    def job_for(self, *, scope=None, pool='finite-pool', configuration='synthetic-config', runtime=10):
        import uuid
        scope = scope or self.scope
        plan = self.repo.create_plan(scope, {'recipe_id': 'synthetic-recipe', 'request': {'model': 'synthetic-model'}},
            {'pool': pool, 'backend': 'comfy-worker', 'enabled': True,
             'configuration_id': configuration, 'expected_runtime_s': runtime},
            expires_at=self.now+3600)
        return self.repo.create_job(scope, plan['id'], uuid.uuid4().hex)

    def allowed(self, job):
        return (job['tenant_id'] == self.scope.tenant_id and job['owner_id'] == self.scope.owner_id
            and job['project_id'] == self.scope.project_id and job['pool'] == 'finite-pool'
            and job['execution_plan']['configuration_id'] == 'synthetic-config'
            and job['expected_runtime_s'] < 100)

    def runner(self):
        return DrainSafeRunner(self.repo, None, self.work, control=self.control, backend=self.backend,
            retry_after_s=0, submission_guard=lambda _: True, stop_new=lambda: self.stopping,
            job_allowed=self.allowed, collection_lock_dir=self.media_lock,
            job_filter=and_(jobs.c.tenant_id == self.scope.tenant_id, jobs.c.owner_id == self.scope.owner_id,
                jobs.c.project_id == self.scope.project_id, jobs.c.pool == 'finite-pool',
                jobs.c.execution_plan['configuration_id'].as_string() == 'synthetic-config'))

    def test_precise_scope_and_callback_filter_before_creating_attempt(self):
        excluded = [self.job_for(scope=Scope('other-tenant', 'superdan', 'project-1')),
            self.job_for(scope=self.other), self.job_for(scope=Scope('test-tenant', 'superdan', 'other-project')),
            self.job_for(pool='other-pool'), self.job_for(configuration='other-config'), self.job_for(runtime=500)]
        allowed = self.job_for()
        result = self.runner().run_once('worker', 'finite-pool')
        self.assertEqual(result['job_id'], allowed['id'])
        self.assertEqual(self.backend.submits, 1)
        for job in excluded:
            row = self.repo.get_job_for_owner(job['tenant_id'], job['owner_id'], job['id'])
            self.assertEqual((row['status'], row['attempt_no'], row['current_attempt_id']), ('queued', 0, None))

    def test_drain_after_unknown_submission_keeps_original_attempt_reconciliation(self):
        job = self.job_for()
        self.backend.unknown = True
        runner = self.runner()
        self.assertEqual(runner.run_once('worker', 'finite-pool')['state'], 'submission_unknown')
        runner.drain()
        self.now += 1
        result = runner.run_once('worker', 'finite-pool')
        self.assertEqual(result['state'], 'submission_unknown')
        self.assertFalse(runner._drain.is_set())
        self.assertEqual(self.backend.submits, 1)
        row = self.repo.get_job(self.scope, job['id'])
        self.assertEqual(row['attempt_no'], 1)
        self.assertEqual(self.control.get('worker')['current_job_id'], job['id'])
        self.assertEqual(self.control.get('worker')['drain_requested'], 1)

    def test_expired_new_scope_still_collects_bound_job_then_exits(self):
        job, next_job = self.job_for(), self.job_for()
        runner = self.runner()
        result = runner.run_once('worker', 'finite-pool')
        # Stable ordering can choose either same-owner job; preserve exact chosen identity.
        current = result['job_id']
        other = next_job['id'] if current == job['id'] else job['id']
        self.stopping, self.backend.state = True, 'succeeded'
        runner._job_allowed_callback = lambda _: False
        collected = []
        def complete(self_runner, bound, lease, tag, task, heartbeat):
            heartbeat()
            collected.append(bound['id'])
            value = self_runner.queue.complete(lease, [{'kind': 'video', 'validated': True,
                'object_key': 'owners/superdan/assets/synthetic/video.mp4', 'size_bytes': 1, 'sha256': '0'*64}],
                actual_cost_microusd=0)
            return self_runner._summary(value)
        with patch.object(WorkerRunner, '_collect', complete), patch('studio_platform.drain_safe_runner.time.sleep'):
            runner.run_forever('worker', 'finite-pool')
        self.assertEqual(collected, [current])
        self.assertEqual(self.backend.submits, 1)
        self.assertEqual(self.repo.get_job(self.scope, other)['attempt_no'], 0)
        self.assertIsNone(self.control.get('worker')['current_job_id'])
        self.assertEqual(self.control.get('worker')['state'], 'draining')

    def test_stop_callback_error_fails_closed_before_claim(self):
        job = self.job_for()
        runner = self.runner()
        def unavailable():
            raise OSError('synthetic operator flag unavailable')
        runner._stop_new_callback = unavailable
        self.assertEqual(runner.run_once('worker', 'finite-pool')['state'], 'idle')
        self.assertEqual(self.repo.get_job(self.scope, job['id'])['attempt_no'], 0)
        self.assertEqual(self.backend.submits, 0)
        self.assertEqual(self.control.get('worker')['drain_requested'], 1)
        runner._stop_new_callback = lambda: None
        self.assertTrue(runner.stopped())

    def test_collection_lock_across_processes_waits_with_heartbeat_and_releases(self):
        context = multiprocessing.get_context('spawn')
        acquired, release = context.Event(), context.Event()
        process = context.Process(target=hold_collection_lock, args=(str(self.media_lock), acquired, release))
        process.start()
        self.addCleanup(lambda: process.terminate() if process.is_alive() else None)
        entered, renewed = threading.Event(), threading.Event()
        failures = []
        def collect():
            try:
                with collection_slot(self.media_lock, renewed.set, poll_interval_s=.02):
                    entered.set()
            except Exception as exc:
                failures.append(type(exc).__name__)
        thread = threading.Thread(target=collect)
        try:
            self.assertTrue(acquired.wait(5))
            thread.start()
            self.assertTrue(renewed.wait(2))
            self.assertFalse(entered.is_set())
            # A second GPU's own execution lock is independent of this CPU gate.
            with _slot_lock(self.media_lock, 'different-gpu-execution') as available:
                self.assertTrue(available)
        finally:
            release.set()
            if thread.ident:
                thread.join(5)
            process.join(5)
        self.assertEqual(process.exitcode, 0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(entered.is_set())
        self.assertEqual(failures, [])

    def test_collection_lease_loss_never_enters_stage(self):
        def lost():
            raise LeaseLost('synthetic lost lease')
        with self.assertRaises(LeaseLost):
            with collection_slot(self.media_lock, lost):
                self.fail('must not collect after lease loss')


if __name__ == '__main__':
    import unittest
    unittest.main()
