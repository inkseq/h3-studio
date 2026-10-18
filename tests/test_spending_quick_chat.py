"""Crash recovery after paid enqueue must reconcile the original job once."""

from dataclasses import replace
import unittest
from unittest import mock

from sqlalchemy import insert, select, update

from studio_platform.repository import (
    Scope,
    jobs,
    plans,
    budget_reservations,
    spending_settings,
    spending_confirmations,
)
import test_platform_quick_chat as chat_fixtures


class SpendingQuickChatTests(unittest.TestCase):
    setUp = chat_fixtures.QuickChatTests.setUp
    tearDown = chat_fixtures.QuickChatTests.tearDown
    card = chat_fixtures.QuickChatTests.card
    preflight = chat_fixtures.QuickChatTests.preflight
    submit = chat_fixtures.QuickChatTests.submit

    def test_original_replay_after_enqueue_does_not_need_another_consumed_grant(self):
        revision = self.card()['revision']
        preflight = self.preflight(revision)
        plan_id = preflight['items'][0]['plan']['plan_id']
        repo = self.repo
        tenant = self.settings.tenant_id
        repo.configure_budget(
            'spending-test',
            tenant_id=tenant,
            owner_id=self.principal.owner,
            limit_microusd=1_000_000,
        )
        with repo.transaction() as connection:
            # Fault-injection fixture supplies a nonzero deterministic quote;
            # no upstream service, GPU or live configuration is involved.
            connection.execute(
                update(plans)
                .where(plans.c.id == plan_id)
                .values(estimated_cost_microusd=100_000)
            )
            plan = (
                connection.execute(select(plans).where(plans.c.id == plan_id))
                .mappings()
                .one()
            )
            connection.execute(
                insert(spending_settings).values(
                    tenant_id=tenant,
                    owner_id=self.principal.owner,
                    version=1,
                    value={
                        'daily_limit_microusd': 0,
                        'ask_above_microusd': 1,
                        'connection_limits': {},
                    },
                    actor_id=self.principal.actor_id,
                    updated_at=repo.clock(),
                )
            )
            connection.execute(
                insert(spending_confirmations).values(
                    tenant_id=tenant,
                    owner_id=self.principal.owner,
                    actor_id=self.principal.actor_id,
                    plan_id=plan_id,
                    plan_hash=plan['plan_hash'],
                    settings_version=1,
                    expires_at=repo.clock() + 60,
                )
            )

        def enqueue(
            principal,
            job,
            *,
            confirmation_required=False,
            spending_settings_version=None,
        ):
            self.assertTrue(confirmation_required)
            return repo.enqueue(
                Scope(
                    tenant, principal.owner, job['project_id'], 'quick-chat-execution'
                ),
                job['id'],
                budget_account_ids=('spending-test',),
                spending_actor=principal.actor_id,
                confirmation_required=confirmation_required,
                spending_settings_version=spending_settings_version,
            )

        self.service.hooks = replace(self.service.hooks, enqueue=enqueue)
        original_put = self.service._put

        def interrupted(connection, row, value, **kwargs):
            if row['kind'] == 'execution' and value.get('status') == 'admitted':
                raise RuntimeError('injected_process_exit_after_enqueue')
            return original_put(connection, row, value, **kwargs)

        with mock.patch.object(self.service, '_put', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'injected_process_exit'):
                self.submit(revision, preflight)
        recovered = self.submit(revision, preflight)
        self.assertEqual(recovered['items'][0]['status'], 'queued')
        with repo.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(jobs)).all()), 1)
            self.assertEqual(
                len(connection.execute(select(budget_reservations)).all()), 1
            )
        self.assertEqual(repo.get_budget('spending-test')['reserved_microusd'], 100_000)


if __name__ == '__main__':
    unittest.main()
