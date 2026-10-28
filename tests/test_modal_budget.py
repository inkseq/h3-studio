"""Original shared ledger, fake Modal lifecycle; never a provider call."""

from dataclasses import asdict
import copy

from sqlalchemy import select, update

from studio_platform.dstack_test_budget import ACCOUNT, projected_cost
from studio_platform.modal_budget import bind_test_guard
from studio_platform.modal_host import HostSpec, ModalHosts
from studio_platform.worker_admission import worker_window_reason
from studio_platform import operator_capacity  # noqa: F401 -- register the original operator tables before fixture schema creation
from studio_platform.repository import (
    BudgetExceeded,
    Scope,
    budget_reservations,
    scaler_receipts,
)
from test_modal_host import FakeModal
from test_platform_repository import LedgerCase


GUARD = {
    'version': 2,
    'task_id': 'cr73-shared',
    'task_limit_microusd': 50_000_000,
    'budget_account_id': ACCOUNT,
    'max_runtime_seconds': 7200,
    'stop_margin_seconds': 300,
}


class ModalBudgetTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.scope = Scope('sixnine', 'superdan', 'project-inkseq-staging-superdan')
        self.pool = 'inkseq-staging-v7-fl'
        self.repo.configure_pool(self.pool, max_instances=10, max_physical_gpus=10)
        self.repo.configure_budget(
            ACCOUNT, tenant_id='sixnine', limit_microusd=50_000_000
        )
        self.spec = HostSpec('ap-test', 'im-test', 'vo-test', 3_000_000)
        self.client = FakeModal()
        self.hosts = ModalHosts(
            self.repo, self.client, self.spec, environment='staging', enabled=True
        )

    def reserve(self, key='one', guard=None, bind=True):
        with self.repo.transaction() as c:
            row = self.repo.reserve_instance_intent(
                self.scope,
                self.pool,
                key,
                provider='modal',
                hard_deadline=self.now + 600,
                reserved_cost_microusd=750_000,
                budget_account_ids=(ACCOUNT,),
                dry_run=False,
                connection=c,
            )
            if bind:
                bind_test_guard(
                    self.repo, c, row['id'], guard or GUARD, asdict(self.spec)
                )
            return row['id']

    def projection(self, guard=None):
        with self.repo.engine.connect() as c:
            return projected_cost(c, guard or GUARD, self.now)

    def worker_reason(self, instance='sb-1', duration=120):
        with self.repo.engine.connect() as c:
            return worker_window_reason(
                c,
                {'provider': 'modal', 'instance_id': instance},
                self.now,
                expected_runtime_s=duration,
            )

    def test_new_claim_and_submit_require_original_funded_modal_binding(self):
        self.assertEqual(self.worker_reason(), 'modal_test_budget_unconfirmed')
        identity = self.reserve()
        self.hosts.start(identity)
        self.assertIsNone(self.worker_reason())
        self.assertEqual(self.worker_reason(duration=480), 'job_exceeds_worker_window')
        self.assertEqual(
            self.worker_reason(duration=float('nan')), 'job_runtime_unconfirmed'
        )
        self.hosts.stop(identity)
        self.assertEqual(self.worker_reason(), 'worker_capacity_stopping')

    def test_late_shared_budget_limit_change_blocks_new_work(self):
        from studio_platform.repository import budget_accounts

        identity = self.reserve()
        self.hosts.start(identity)
        with self.repo.transaction() as c:
            c.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == ACCOUNT)
                .values(limit_microusd=1)
            )
        self.assertEqual(self.worker_reason(), 'modal_test_budget_unconfirmed')

    def test_bound_original_is_counted_once_and_rebinding_is_read_only(self):
        identity = self.reserve()
        with self.repo.transaction() as c:
            bind_test_guard(self.repo, c, identity, GUARD, asdict(self.spec))
            self.assertEqual(len(c.execute(select(scaler_receipts)).all()), 1)
        self.assertEqual(self.projection(), 750_000)
        self.hosts.start(identity)
        self.hosts.start(identity)
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(self.projection(), 750_000)

    def test_unattributed_shared_intent_cannot_create_or_disappear_from_projection(
        self,
    ):
        identity = self.reserve(bind=False)
        with self.assertRaises(BudgetExceeded):
            self.hosts.start(identity)
        with self.assertRaises(BudgetExceeded):
            self.projection()
        self.assertEqual(self.client.created, [])

    def test_exit_and_invoice_pending_keep_original_commitment(self):
        identity = self.reserve()
        self.hosts.start(identity)
        self.hosts.stop(identity)
        self.hosts.observe(identity)
        with self.repo.transaction() as c:
            c.execute(
                update(budget_reservations)
                .where(budget_reservations.c.reference_id == identity)
                .values(state='invoice_pending')
            )
        self.assertEqual(self.projection(), 750_000)
        self.repo.settle_instance_cost(identity, actual_cost_microusd=125_000)
        self.assertEqual(self.projection(), 125_000)

    def test_mutated_or_duplicate_binding_refuses(self):
        identity = self.reserve()
        bad = copy.deepcopy(GUARD)
        bad['task_id'] = 'other'
        with self.repo.transaction() as c, self.assertRaises(BudgetExceeded):
            bind_test_guard(self.repo, c, identity, bad, asdict(self.spec))
        with self.repo.transaction() as c:
            row = c.execute(select(scaler_receipts)).mappings().one()
            facts = copy.deepcopy(row['facts'])
            facts['max_hourly_microusd'] = 1
            c.execute(
                update(scaler_receipts)
                .where(scaler_receipts.c.id == row['id'])
                .values(facts=facts)
            )
        with self.assertRaises(BudgetExceeded):
            self.hosts.start(identity)
        self.assertEqual(self.client.created, [])

    def test_full_shared_projection_blocks_new_create(self):
        guard = {**GUARD, 'task_limit_microusd': 1_000_000}
        first = self.reserve('first', guard)
        self.hosts.start(first)
        with self.assertRaises(BudgetExceeded):
            self.reserve('second', guard)
        self.assertEqual(self.projection(guard), 750_000)
        self.assertEqual(len(self.client.created), 1)

    def test_different_valid_task_does_not_change_original_task(self):
        self.reserve()
        self.assertEqual(self.projection({**GUARD, 'task_id': 'other'}), 0)

    def test_removed_host_does_not_accrue_fake_elapsed_cost(self):
        identity = self.reserve()
        self.hosts.start(identity)
        self.hosts.stop(identity)
        self.hosts.observe(identity)
        self.now += 10000
        self.assertEqual(self.projection(), 750_000)

    def test_unconfirmed_running_beyond_deadline_grows_projection(self):
        identity = self.reserve()
        self.hosts.start(identity)
        self.now += 900
        self.assertEqual(self.projection(), 1_000_000)
