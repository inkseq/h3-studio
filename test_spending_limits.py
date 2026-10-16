"""Real ledger admission, concurrent caps, Sydney rollover and quote approval."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import unittest
import uuid

from sqlalchemy import insert, select, update

from studio_platform.repository import (BudgetExceeded, spending_settings, spending_confirmations,
    jobs, budget_reservations, Scope)
from studio_platform.spending_limits import daily_projection, SpendingConfirmationRequired, check_submission_confirmation
from studio_platform.auth import Principal
from test_platform_repository import LedgerCase


class SpendingLimitsTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc).timestamp()

    def settings(self, daily=0, ask=0, connections=None):
        with self.repo.transaction() as connection:
            condition = (spending_settings.c.tenant_id == self.scope.tenant_id,
                         spending_settings.c.owner_id == self.scope.owner_id)
            old = connection.execute(select(spending_settings).where(*condition)).mappings().first()
            values = {"value": {"daily_limit_microusd": daily, "ask_above_microusd": ask,
                "connection_limits": connections or {}}, "version": old["version"] + 1 if old else 1,
                "updated_at": self.now, "actor_id": "browser:superdan"}
            if old: connection.execute(update(spending_settings).where(*condition).values(**values))
            else: connection.execute(insert(spending_settings).values(tenant_id=self.scope.tenant_id,
                owner_id=self.scope.owner_id, **values))

    def submit(self, cost=100_000, *, key=None, plan=None, actor="browser:superdan", confirmation_required=False, spending_settings_version=None):
        plan = plan or self.plan(cost=cost)
        return self.repo.create_job(self.scope, plan["id"], key or uuid.uuid4().hex,
            budget_account_ids=("owner-budget",), spending_actor=actor, confirmation_required=confirmation_required,
            spending_settings_version=spending_settings_version)

    def projection(self, actor=None):
        with self.repo.engine.connect() as connection:
            return daily_projection(connection, self.scope.tenant_id, self.scope.owner_id,
                                    self.now, actor_id=actor)

    def test_zero_does_not_remove_cumulative_credit_or_unknown_holds(self):
        self.settings()
        self.repo.configure_budget("owner-budget", tenant_id=self.scope.tenant_id,
            owner_id=self.scope.owner_id, limit_microusd=100_000)
        self.submit()
        with self.assertRaises(BudgetExceeded): self.submit()
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000)

    def test_parallel_daily_cap_is_atomic(self):
        self.settings(daily=100_000)
        plans = [self.plan(cost=60_000) for _ in range(8)]
        def run(plan):
            try: return self.submit(plan=plan)["created"]
            except BudgetExceeded: return False
        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(sum(executor.map(run, plans)), 1)
        self.assertEqual(self.projection(), 60_000)

    def test_sydney_rollover_keeps_unresolved_and_avoids_layer_double_count(self):
        self.settings(daily=150_000)
        self.repo.configure_budget("shared", tenant_id=self.scope.tenant_id, limit_microusd=10_000_000)
        plan=self.plan()
        row=self.repo.create_job(self.scope,plan["id"],"original",budget_account_ids=("owner-budget","shared"))
        self.assertEqual(self.projection(),100_000)
        self.now += 3601  # 00:00 Sydney, not UTC midnight
        self.assertEqual(self.projection(),100_000)
        with self.assertRaises(BudgetExceeded):self.submit()
        with self.repo.transaction() as connection:self.repo._settle(connection,"job",row["id"],80_000)
        self.assertEqual(self.projection(),80_000)
        self.now += 86400
        self.assertEqual(self.projection(),0)

    def test_per_connection_cap_uses_actual_caller_not_quick_chat_namespace(self):
        self.settings(connections={"key:one":100_000,"key:two":200_000})
        self.submit(actor="key:one")
        with self.assertRaisesRegex(BudgetExceeded,"connection_daily_limit_reached"):self.submit(actor="key:one")
        self.submit(actor="key:two")
        self.assertEqual(self.projection("key:one"),100_000)
        self.assertEqual(self.projection("key:two"),100_000)

    def test_quote_confirmation_is_single_use_and_replay_stays_idempotent(self):
        self.settings(ask=1)
        plan=self.plan()
        with self.assertRaises(SpendingConfirmationRequired):self.submit(plan=plan,key="original")
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(jobs)).all(),[])
            self.assertEqual(connection.execute(select(budget_reservations)).all(),[])
        with self.repo.transaction() as connection:
            connection.execute(insert(spending_confirmations).values(tenant_id=self.scope.tenant_id,
                owner_id=self.scope.owner_id,actor_id="browser:superdan",plan_id=plan["id"],
                plan_hash=plan["plan_hash"],settings_version=1,expires_at=self.now+100))
        self.assertTrue(self.submit(plan=plan,key="original")["created"])
        self.assertFalse(self.submit(plan=plan,key="original")["created"])
        with self.assertRaises(SpendingConfirmationRequired):self.submit(plan=plan,key="second")

    def test_settings_change_invalidates_old_confirmation_without_reset(self):
        self.settings(ask=1)
        plan=self.plan()
        with self.repo.transaction() as connection:
            connection.execute(insert(spending_confirmations).values(tenant_id=self.scope.tenant_id,
                owner_id=self.scope.owner_id,actor_id="browser:superdan",plan_id=plan["id"],
                plan_hash=plan["plan_hash"],settings_version=1,expires_at=self.now+100))
        self.settings(ask=2)
        with self.assertRaises(SpendingConfirmationRequired):self.submit(plan=plan)

    def test_combined_submission_asks_before_first_smaller_job(self):
        self.settings(ask=75_000)
        selected=[self.plan(cost=50_000),self.plan(cost=50_000)]
        with self.assertRaises(SpendingConfirmationRequired) as raised:
            check_submission_confirmation(self.repo,self.scope.tenant_id,Principal(self.scope.owner_id,'browser:superdan'),[p['id'] for p in selected])
        self.assertEqual(raised.exception.amount,100_000)
        with self.repo.engine.connect() as connection:self.assertEqual(connection.execute(select(jobs)).all(),[])
        with self.repo.transaction() as connection:
            for plan in selected:
                connection.execute(insert(spending_confirmations).values(tenant_id=self.scope.tenant_id,
                    owner_id=self.scope.owner_id,actor_id='browser:superdan',plan_id=plan['id'],
                    plan_hash=plan['plan_hash'],settings_version=1,expires_at=self.now+100))
        required=check_submission_confirmation(self.repo,self.scope.tenant_id,Principal(self.scope.owner_id,'browser:superdan'),[p['id'] for p in selected])
        # Both concurrent batches pass the preliminary check before either pays.
        other_required=check_submission_confirmation(self.repo,self.scope.tenant_id,Principal(self.scope.owner_id,'browser:superdan'),[p['id'] for p in selected])
        self.assertTrue(required);self.assertTrue(other_required)
        for plan in selected:self.submit(plan=plan,confirmation_required=required)
        for plan in selected:
            with self.assertRaises(SpendingConfirmationRequired):self.submit(plan=plan,confirmation_required=other_required)
        self.assertEqual(self.projection(),100_000)
        self.settings(ask=0)
        with self.assertRaises(SpendingConfirmationRequired):self.submit(plan=selected[0],confirmation_required=other_required)
        with self.repo.engine.connect() as connection:
            self.assertTrue(all(row.consumed_reference for row in connection.execute(select(spending_confirmations))))

    def test_lowering_ask_after_a_no_confirmation_check_cannot_bypass_the_new_batch_threshold(self):
        self.settings(ask=150_000)
        selected=[self.plan(cost=50_000),self.plan(cost=50_000)]
        check=check_submission_confirmation(self.repo,self.scope.tenant_id,Principal(self.scope.owner_id,'browser:superdan'),[p['id'] for p in selected])
        self.assertFalse(check)
        self.settings(ask=75_000)
        from studio_platform.repository import Conflict
        with self.assertRaisesRegex(Conflict,'spending_settings_changed'):
            self.submit(plan=selected[0],confirmation_required=bool(check),spending_settings_version=check.version)
        with self.repo.engine.connect() as connection:self.assertEqual(connection.execute(select(jobs)).all(),[])


if __name__ == "__main__": unittest.main()
