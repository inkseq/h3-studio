"""Continuing capacity uses the original money rows, with atomic capped funding."""

from concurrent.futures import ThreadPoolExecutor
import unittest

from sqlalchemy import insert, inspect, select, update

from studio_platform.operator_capacity import operator_commands, operator_nodes
from studio_platform.repository import (
    BudgetExceeded,
    Conflict,
    budget_accounts,
    budget_reservations,
    instance_intents,
)
from test_platform_repository import LedgerCase


class DstackBudgetWindowTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.repo.configure_pool('continuous', max_instances=3, max_physical_gpus=3)
        self.repo.configure_budget(
            'global-budget', tenant_id=self.scope.tenant_id, limit_microusd=20_000_000
        )
        self.accounts = ['global-budget', 'owner-budget']
        usage = {'instances': 0, 'physical_gpus': 0}
        self.intent = self.repo.reserve_instance_intent(
            self.scope,
            pool='continuous',
            intent_key='new-v2',
            physical_gpus=1,
            slots=1,
            reserved_cost_microusd=1_000_000,
            hard_deadline=None,
            provider='vast',
            budget_account_ids=self.accounts,
            continuing=True,
            capacity_usage=usage,
            active_instance_ids=(),
            dry_run=False,
        )
        self.node_id = self.intent['id']
        payload = {
            'capacity_backend': 'dstack-v1',
            'tenant_id': self.scope.tenant_id,
            'owner_id': self.scope.owner_id,
            'project_id': self.scope.project_id,
            'hard_deadline': None,
            'lifecycle': {'version': 2, 'budget_account_ids': self.accounts},
            'request': {
                'lifecycle_version': 2,
                'hard_deadline': None,
                'max_duration_s': None,
            },
            'dstack': {'lifecycle_version': 2},
        }
        with self.repo.transaction() as conn:
            conn.execute(
                insert(operator_commands).values(
                    id='original-command',
                    actor='superdan',
                    idempotency_key='original',
                    request_hash='a' * 64,
                    kind='start',
                    state='accepted',
                    payload={},
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            conn.execute(
                insert(operator_nodes).values(
                    intent_id=self.node_id,
                    command_id='original-command',
                    ordinal=0,
                    binding_id='test',
                    binding_hash='b' * 64,
                    payload=payload,
                    desired_state='running',
                    runtime_state='starting',
                    updated_at=self.now,
                )
            )

    def rows(self):
        with self.repo.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(budget_reservations)
                    .where(budget_reservations.c.reference_id == self.node_id)
                    .order_by(budget_reservations.c.account_id)
                ).mappings()
            ]

    def fund(self, amount):
        with self.repo.transaction() as conn:
            return self.repo.fund_instance_window(
                conn, self.node_id, amount, self.accounts
            )

    def test_same_rows_increase_once_without_settlement_or_deadline(self):
        before = self.rows()
        self.assertEqual(self.fund(2_000_000), 2_000_000)
        self.assertEqual(self.fund(2_000_000), 2_000_000)
        after = self.rows()
        self.assertEqual([r['id'] for r in after], [r['id'] for r in before])
        self.assertTrue(
            all(
                r['state'] == 'reserved'
                and r['actual_cost_microusd'] is None
                and r['amount_microusd'] == 2_000_000
                for r in after
            )
        )
        intent = self.repo.list_instance_intents()[0]
        self.assertIsNone(intent['hard_deadline'])
        self.assertEqual(intent['state'], 'reserved')
        self.assertEqual(intent['reserved_cost_microusd'], 2_000_000)
        for account in self.accounts:
            self.assertEqual(self.repo.get_budget(account)['spent_microusd'], 0)
            self.assertEqual(
                self.repo.get_budget(account)['reserved_microusd'], 2_000_000
            )

    def test_exhausted_second_account_leaves_first_account_unchanged(self):
        self.repo.configure_budget(
            'owner-budget',
            tenant_id=self.scope.tenant_id,
            owner_id=self.scope.owner_id,
            limit_microusd=1_500_000,
        )
        before = (
            self.rows(),
            [self.repo.get_budget(account) for account in self.accounts],
        )
        with self.assertRaises(BudgetExceeded):
            self.fund(2_000_000)
        self.assertEqual(
            (self.rows(), [self.repo.get_budget(account) for account in self.accounts]),
            before,
        )

    def test_concurrent_topups_do_not_duplicate_reserved_counters(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(self.fund, [2_000_000] * 4))
        self.assertEqual(values, [2_000_000] * 4)
        self.assertEqual(
            self.repo.get_budget('owner-budget')['reserved_microusd'], 2_000_000
        )

    def test_missing_or_cross_owner_account_and_settled_rows_block(self):
        with self.repo.transaction() as conn:
            conn.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == 'owner-budget')
                .values(owner_id='supervan')
            )
        with self.assertRaises(BudgetExceeded):
            self.fund(2_000_000)
        with self.repo.transaction() as conn:
            conn.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == 'owner-budget')
                .values(owner_id='superdan')
            )
            conn.execute(
                update(budget_reservations)
                .where(budget_reservations.c.reference_id == self.node_id)
                .values(state='settled', actual_cost_microusd=100)
            )
        with self.assertRaises(BudgetExceeded):
            self.fund(2_000_000)

    def test_legacy_finite_or_stopped_node_cannot_gain_continuing_funds(self):
        with self.repo.transaction() as conn:
            conn.execute(
                update(instance_intents)
                .where(instance_intents.c.id == self.node_id)
                .values(hard_deadline=self.now + 60)
            )
        with self.assertRaises(Conflict):
            self.fund(2_000_000)
        with self.repo.transaction() as conn:
            conn.execute(
                update(instance_intents)
                .where(instance_intents.c.id == self.node_id)
                .values(hard_deadline=None)
            )
            conn.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == self.node_id)
                .values(desired_state='stopped')
            )
        with self.assertRaises(Conflict):
            self.fund(2_000_000)


class DstackDeadlineMigrationTests(LedgerCase):
    def test_postgres_relaxes_old_constraint_without_rewriting_history(self):
        if self.repo.engine.dialect.name != 'postgresql':
            self.skipTest('Existing PostgreSQL column migration')
        self.repo.configure_pool('migration', max_instances=3, max_physical_gpus=3)
        with self.repo.transaction() as conn:
            conn.exec_driver_sql(
                'ALTER TABLE platform_instance_intents ALTER COLUMN hard_deadline SET NOT NULL'
            )
        original = self.repo.reserve_instance_intent(
            self.scope,
            pool='migration',
            intent_key='finite-original',
            provider='lium',
            hard_deadline=self.now + 600,
            reserved_cost_microusd=1_000_000,
            budget_account_ids=['owner-budget'],
            dry_run=False,
        )
        before = self.repo.list_instance_intents()
        self.repo.create_schema()
        self.assertEqual(self.repo.list_instance_intents(), before)
        self.assertTrue(
            next(
                c
                for c in inspect(self.repo.engine).get_columns(
                    'platform_instance_intents'
                )
                if c['name'] == 'hard_deadline'
            )['nullable']
        )
        continuous = self.repo.reserve_instance_intent(
            self.scope,
            pool='migration',
            intent_key='continuing-new',
            provider='vast',
            hard_deadline=None,
            reserved_cost_microusd=1_000_000,
            budget_account_ids=['owner-budget'],
            continuing=True,
            capacity_usage={'instances': 0, 'physical_gpus': 0},
            active_instance_ids=(),
            dry_run=False,
        )
        self.assertIsNone(continuous['hard_deadline'])
        self.assertEqual(
            next(
                i
                for i in self.repo.list_instance_intents()
                if i['id'] == original['id']
            ),
            before[0],
        )


if __name__ == '__main__':
    unittest.main()
