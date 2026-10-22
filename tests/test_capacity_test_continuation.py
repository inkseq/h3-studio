"""Legacy staging rentals retain their proven pre-adoption effective guard."""

import copy
import unittest
import uuid

from sqlalchemy import insert, select, update

from studio_platform.capacity_settings import DEFAULTS, admission_snapshot
from studio_platform.dstack_test_budget import budget_reason
from studio_platform.repository import budget_reservations, outbox, spending_settings
from studio_platform.spending_limits import ADMIN_OWNER, admin_defaults
import test_dstack_test_budget as fixtures


class LegacyTestContinuationTests(unittest.TestCase):
    def setUp(self):
        # Compose the existing isolated fake-provider fixture without inheriting
        # and repeating its entire test module.
        self.fixture = fixtures.DailyTestBudgetTests(
            "test_native_timeout_is_only_opt_in_and_cannot_change_idle_or_production_scope"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.fixture.start()
        self.intent, self.node = self.fixture.rows()
        self.repo = self.fixture.repo
        self.at = self.fixture.now + 10
        self.now = self.fixture.now + 1000
        self.old = admin_defaults(self.fixture.service)
        self.old.update(test_task_limit_microusd=10_000_000, max_runtime_seconds=4000)
        self.adopted = {**copy.deepcopy(DEFAULTS), **self.old}
        self.current = {
            **self.adopted,
            "test_task_limit_microusd": 1_000_000,
            "max_runtime_seconds": 500,
        }
        self.event = {
            "tenant_id": self.fixture.settings.tenant_id,
            "owner_id": ADMIN_OWNER,
            "actor_id": "browser:admin",
            "reason": "#456 original adoption",
            "old": {"version": 2, "value": self.old},
            "new": {"version": 3, "value": self.adopted},
        }
        with self.repo.transaction() as c:
            c.execute(
                insert(spending_settings).values(
                    tenant_id=self.fixture.settings.tenant_id,
                    owner_id=ADMIN_OWNER,
                    version=4,
                    value=self.current,
                    actor_id="later-admin",
                    updated_at=self.at + 1,
                )
            )
        self.audit_id = self.audit()

    def audit(self, event=None, at=None):
        key = str(uuid.uuid4())
        with self.repo.transaction() as c:
            c.execute(
                insert(outbox).values(
                    id=key,
                    event_type="spending.settings_updated",
                    aggregate_id=ADMIN_OWNER,
                    payload=copy.deepcopy(self.event if event is None else event),
                    created_at=self.at if at is None else at,
                )
            )
        return key

    def reason(self, *, intent=None, node=None, now=None):
        with self.repo.engine.connect() as c:
            return budget_reason(
                c,
                self.intent if intent is None else intent,
                self.node if node is None else node,
                self.now if now is None else now,
            )

    def test_later_lower_settings_do_not_shorten_original_rental(self):
        before = copy.deepcopy((self.intent, self.node))
        self.assertIsNone(self.reason())
        self.assertEqual(
            self.reason(now=self.fixture.now + 3800),
            "dstack_test_runtime_limit_reached",
        )
        self.assertEqual((self.intent, self.node), before)

    def test_independently_sampled_command_clock_may_precede_intent(self):
        from studio_platform.operator_capacity import operator_commands

        with self.repo.transaction() as c:
            c.execute(
                update(operator_commands)
                .where(operator_commands.c.id == self.node["command_id"])
                .values(created_at=self.intent["created_at"] - 0.25)
            )
        self.assertIsNone(self.reason())

    def test_original_lower_cap_is_not_expanded_by_template_or_later_raise(self):
        with self.repo.transaction() as c:
            c.execute(
                update(spending_settings).values(
                    value={
                        **self.current,
                        "test_task_limit_microusd": 20_000_000,
                        "max_runtime_seconds": 7200,
                    }
                )
            )
            c.execute(update(budget_reservations).values(amount_microusd=12_000_000))
        self.assertEqual(self.reason(), "dstack_test_task_limit_reached")

    def test_absent_pre_adoption_row_does_not_create_a_historical_clamp(self):
        event = copy.deepcopy(self.event)
        old = {
            **self.old,
            "test_task_limit_microusd": 1_000_000,
            "max_runtime_seconds": 500,
        }
        event["old"] = {"version": 0, "value": old}
        event["new"] = {"version": 1, "value": {**copy.deepcopy(DEFAULTS), **old}}
        with self.repo.transaction() as c:
            c.execute(
                update(outbox).where(outbox.c.id == self.audit_id).values(payload=event)
            )
        # Version zero meant no stored admin clamp. The original 20m/7200s
        # guard remains authoritative, rather than later service defaults.
        self.assertIsNone(self.reason())

    def test_missing_or_duplicate_original_adoption_is_unconfirmed(self):
        with self.repo.transaction() as c:
            c.execute(
                update(outbox)
                .where(outbox.c.id == self.audit_id)
                .values(event_type="unrelated.historical.event")
            )
        self.assertEqual(self.reason(), "dstack_test_budget_unconfirmed")
        self.audit()
        self.assertIsNone(self.reason())
        self.audit()
        self.assertEqual(self.reason(), "dstack_test_budget_unconfirmed")

    def test_bad_adoption_source_facts_fail_closed(self):
        for mutate in (
            lambda p: p["old"].update(version=1),
            lambda p: p["old"]["value"].update(max_runtime_seconds=True),
            lambda p: p["old"]["value"].update(capacity_policy_version=None),
            lambda p: p["new"].update(version=5),
            lambda p: p["new"]["value"].update(capacity_policy_version=True),
            lambda p: p.update(actor_id=""),
            lambda p: p.update(reason=""),
        ):
            event = copy.deepcopy(self.event)
            mutate(event)
            with self.repo.transaction() as c:
                c.execute(
                    update(outbox)
                    .where(outbox.c.id == self.audit_id)
                    .values(payload=event)
                )
            with self.subTest(event=event):
                self.assertEqual(self.reason(), "dstack_test_budget_unconfirmed")

    def test_original_creation_command_and_tenant_are_bound(self):
        with self.repo.transaction() as c:
            c.execute(
                update(outbox)
                .where(outbox.c.id == self.audit_id)
                .values(created_at=self.intent["created_at"] - 1)
            )
        self.assertEqual(self.reason(), "dstack_test_budget_unconfirmed")
        with self.repo.transaction() as c:
            c.execute(
                update(outbox)
                .where(outbox.c.id == self.audit_id)
                .values(created_at=self.at)
            )
        for change in (
            {"command_id": str(uuid.uuid4())},
            {"intent_id": str(uuid.uuid4())},
        ):
            with self.subTest(change=change):
                self.assertEqual(
                    self.reason(node={**self.node, **change}),
                    "dstack_test_budget_unconfirmed",
                )
        foreign = copy.deepcopy(self.node)
        foreign["payload"]["owner_id"] = "other"
        self.assertEqual(self.reason(node=foreign), "dstack_test_budget_unconfirmed")
        self.assertEqual(
            self.reason(intent={**self.intent, "created_at": self.at + 1}),
            "dstack_test_budget_unconfirmed",
        )

    def test_new_frozen_snapshot_needs_no_legacy_history(self):
        node = copy.deepcopy(self.node)
        node["payload"]["capacity_policy_snapshot"] = admission_snapshot(
            {
                "version": 3,
                "value": {
                    **copy.deepcopy(DEFAULTS),
                    "test_task_limit_microusd": 20_000_000,
                },
            }
        )
        with self.repo.transaction() as c:
            c.execute(
                update(outbox)
                .where(outbox.c.id == self.audit_id)
                .values(event_type="unrelated.historical.event")
            )
        self.assertIsNone(self.reason(node=node))
        with self.repo.engine.connect() as c:
            current = c.execute(select(spending_settings)).mappings().one()
            self.assertEqual(current["value"], self.current)


if __name__ == "__main__":
    unittest.main()
