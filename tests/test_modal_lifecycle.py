"""Original Modal hosts and job fences; all provider operations are fake."""

from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy import select, update

from studio_platform.modal_lifecycle import ModalLifecycle
from studio_platform.queue import TaskQueue
from studio_platform.repository import attempts, budget_reservations, instance_intents
from studio_platform.dstack_test_budget import ACCOUNT
from test_modal_budget import ModalBudgetCase


class ModalLifecycleTests(ModalBudgetCase):
    def setUp(self):
        super().setUp()
        self.identity = self.reserve()
        self.hosts.start(self.identity)
        self.bindings, self.slots = Mock(), Mock()
        self.bindings.slot.return_value = SimpleNamespace(confirmed_idle=True)
        self.report = Mock(return_value=True)
        self.lifecycle = ModalLifecycle(
            self.repo,
            self.hosts,
            self.bindings,
            self.slots,
            SimpleNamespace(capacity_startup_timeout_seconds=1200),
            report=self.report,
        )

    def job(self):
        plan = self.plan(
            execution={
                'pool': self.pool,
                'expected_runtime_s': 120,
                'model_id': 'test-model-only',
            }
        )
        return self.repo.create_job(
            self.scope, plan['id'], 'modal-test-job', budget_account_ids=(ACCOUNT,)
        )

    def assert_pending_cost(self):
        with self.repo.engine.connect() as c:
            row = (
                c.execute(
                    select(budget_reservations).where(
                        budget_reservations.c.reference_type == 'instance',
                        budget_reservations.c.reference_id == self.identity,
                    )
                )
                .mappings()
                .one()
            )
        self.assertIn(row['state'], {'reserved', 'invoice_pending'})
        self.assertIsNone(row['actual_cost_microusd'])

    def test_ready_host_registers_original_slot_even_with_admission_off(self):
        self.hosts.enabled = False
        self.lifecycle.sweep()
        self.slots.ensure.assert_called_once_with(
            self.identity, self.bindings.slot.return_value
        )
        self.assertEqual(self.row()['state'], 'ready')
        self.assertEqual(len(self.client.created), 1)

    def row(self):
        with self.repo.engine.connect() as c:
            return dict(
                c.execute(
                    select(instance_intents).where(
                        instance_intents.c.id == self.identity
                    )
                )
                .mappings()
                .one()
            )

    def test_unknown_create_is_only_observed_never_created_again(self):
        other = self.reserve('unknown')
        self.client.drop_create = True
        self.hosts.start(other)
        self.lifecycle.sweep()
        self.assertEqual(len(self.client.created), 2)
        self.assertEqual(self.slots.ensure.call_count, 2)

    def test_unready_original_is_stopped_once_at_startup_timeout(self):
        with self.repo.transaction() as c:
            c.execute(
                update(instance_intents)
                .where(instance_intents.c.id == self.identity)
                .values(hard_deadline=self.now + 3600)
            )
        self.bindings.slot.side_effect = ValueError('private diagnostic')
        self.now += 1200
        self.lifecycle.sweep()
        self.lifecycle.sweep()
        self.assertEqual(self.client.stopped, ['sb-1'])
        self.assertEqual(self.row()['state'], 'destroyed')
        self.assert_pending_cost()

    def test_exit_withdraws_cpu_slot_without_settling_supplier_cost(self):
        self.client.hosts['inkseq-' + self.identity]['exit_code'] = 0
        self.lifecycle.sweep()
        self.slots.stop.assert_called_with(self.identity)
        self.slots.ensure.assert_not_called()
        self.assert_pending_cost()

    def test_silent_attempt_is_fenced_before_stop_and_alerted_once(self):
        queue = TaskQueue(self.repo)
        job = self.job()
        claim = queue.claim(
            'modal-' + self.identity.replace('-', ''), self.pool, job_ids=[job['id']]
        )
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, 'original-upstream')
        before = self.repo.get_job(self.scope, job['id'])
        original_stop = self.client.terminate

        def stopped(identity):
            current = self.repo.get_job(self.scope, job['id'])
            self.assertEqual(current['status'], 'queued')
            self.assertGreater(current['fence'], before['fence'])
            original_stop(identity)

        self.client.terminate = stopped
        self.now += 601
        self.lifecycle.sweep()
        self.lifecycle.sweep()
        self.assertEqual(self.client.stopped, ['sb-1'])
        self.assertEqual(self.report.call_count, 1)
        after = self.repo.get_job(self.scope, job['id'])
        self.assertEqual(after['request_hash'], before['request_hash'])
        with self.repo.engine.connect() as c:
            old = (
                c.execute(
                    select(attempts).where(attempts.c.id == claim.lease.attempt_id)
                )
                .mappings()
                .one()
            )
        self.assertEqual(old['status'], 'attempt_lost')
        self.assertIsNone(old['actual_cost_microusd'])

    def test_unaccepted_alert_retries_receipt_without_reclaim_or_rental_replay(self):
        self.report.return_value = False
        queue = TaskQueue(self.repo)
        job = self.job()
        claim = queue.claim(
            'modal-' + self.identity.replace('-', ''), self.pool, job_ids=[job['id']]
        )
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, 'original-upstream')
        self.now += 601
        self.lifecycle.sweep()
        self.report.return_value = True
        self.lifecycle.sweep()
        self.lifecycle.sweep()
        self.assertEqual(self.report.call_count, 2)
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(self.client.stopped, ['sb-1'])
        self.assertEqual(
            self.repo.get_job(self.scope, job['id'])['result'][
                'capacity_reclaim_count'
            ],
            1,
        )
