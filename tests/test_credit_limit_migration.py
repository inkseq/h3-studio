"""A credit rollout never silently removes a previously approved USD limit."""

import copy
from pathlib import Path

from sqlalchemy import insert, select, update

from studio_platform.auth import Principal
from studio_platform.repository import Conflict, jobs, spending_settings
from studio_platform.spending_limits import (
    CREDIT_DEFAULTS,
    USER_DEFAULTS,
    check_submission_confirmation,
    read_settings,
)
import test_credit_spending as fixtures


class CreditLimitMigrationTests(fixtures.CreditSpendingTests):
    def raw_limits(self, value):
        with self.repo.transaction() as connection:
            condition = (
                spending_settings.c.tenant_id == self.scope.tenant_id,
                spending_settings.c.owner_id == self.scope.owner_id,
            )
            old = (
                connection.execute(select(spending_settings).where(*condition))
                .mappings()
                .first()
            )
            values = {
                "version": old["version"] + 1 if old else 1,
                "value": copy.deepcopy(value),
                "actor_id": "browser:superdan",
                "updated_at": self.now,
            }
            if old:
                connection.execute(
                    update(spending_settings).where(*condition).values(**values)
                )
            else:
                connection.execute(
                    insert(spending_settings).values(
                        tenant_id=self.scope.tenant_id,
                        owner_id=self.scope.owner_id,
                        **values,
                    )
                )

    def assert_review(self, call):
        with self.assertRaises(Conflict) as error:
            call()
        self.assertEqual(str(error.exception), "credit_limits_review_required")

    def test_legacy_owner_and_agent_caps_block_direct_and_batch_without_reservations(
        self,
    ):
        self.policy()
        self.seed(1000)
        self.raw_limits(
            {
                **USER_DEFAULTS,
                "daily_limit_microusd": 1_000_000,
                "ask_above_microusd": 1_000_000,
                "connection_limits": {"key:old": 1_000_000},
            }
        )
        plan = self.plan()
        actor = Principal(self.scope.owner_id, "key:old", machine=True)
        self.assert_review(lambda: self.submit(plan, actor=actor.actor_id))
        self.assert_review(
            lambda: check_submission_confirmation(
                self.repo, self.scope.tenant_id, actor, [plan["id"]]
            )
        )
        self.assertEqual(self.balance()["held"], 0)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(jobs.c.id)).all(), [])

    def test_each_limit_needs_its_own_explicit_credit_setting_and_zero_is_valid(self):
        self.policy()
        self.seed(1000)
        value = {
            **USER_DEFAULTS,
            "daily_limit_microusd": 1_000_000,
            "ask_above_microusd": 1_000_000,
            "connection_limits": {"key:old": 1_000_000},
        }
        plan = self.plan()
        for additions in (
            {},
            {"daily_limit_credits": 0},
            {"daily_limit_credits": 0, "ask_above_credits": 0},
            {
                "daily_limit_credits": 0,
                "ask_above_credits": 0,
                "connection_credit_limits": {"key:other": 0},
            },
        ):
            self.raw_limits({**value, **additions})
            self.assert_review(lambda: self.submit(plan, actor="key:old"))
        self.raw_limits(
            {**value, **CREDIT_DEFAULTS, "connection_credit_limits": {"key:old": 0}}
        )
        accepted = self.submit(plan, actor="key:old")
        self.assertTrue(accepted["created"])
        self.assertEqual(self.balance()["held"], 20)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )
        with self.repo.engine.connect() as connection:
            saved = read_settings(
                connection, self.scope.tenant_id, self.scope.owner_id
            )["value"]
        self.assertEqual(saved["connection_limits"], value["connection_limits"])
        self.assertEqual(saved["daily_limit_microusd"], value["daily_limit_microusd"])

    def test_owner_browser_explicit_conversion_keeps_old_units_and_original_quote(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings

        self.policy()
        self.seed(1000)
        original = {
            **USER_DEFAULTS,
            "daily_limit_microusd": 1_000_000,
            "ask_above_microusd": 1,
        }
        self.raw_limits(original)
        plan = self.plan()
        self.assert_review(lambda: self.submit(plan))
        app = create_app(
            Settings(
                Path(self.temp.name) / "migration-api",
                tenant_id=self.scope.tenant_id,
                auth_mode="local-test",
            ),
            repository=self.repo,
        )
        with TestClient(app) as client:
            client.post(
                "/api/auth/login", json={"username": "superdan"}
            ).raise_for_status()
            result = client.put(
                "/v1/account/spending-settings",
                json={"expected_version": 1, "value": copy.deepcopy(CREDIT_DEFAULTS)},
            )
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["unit"], "credits")
        job = self.submit(plan)
        self.assertEqual(job["execution_plan"], plan["execution_plan"])
        self.assertEqual(job["execution_plan"]["credit_quote"]["credits"], 20)
        with self.repo.engine.connect() as connection:
            saved = read_settings(
                connection, self.scope.tenant_id, self.scope.owner_id
            )["value"]
        self.assertEqual({key: saved[key] for key in USER_DEFAULTS}, original)
        self.assertEqual(self.balance()["held"], 20)


def load_tests(_loader, _standard, _pattern):
    import unittest

    return unittest.TestSuite(
        CreditLimitMigrationTests(name)
        for name in CreditLimitMigrationTests.__dict__
        if name.startswith("test_")
    )
