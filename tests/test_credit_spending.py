"""Credit-unit caps/confirmation on the original ledger, no paid execution."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import unittest
import uuid

from sqlalchemy import insert, select, update

from studio_platform.auth import Principal
from studio_platform import credits
from studio_platform.repository import (
    BudgetExceeded,
    Conflict,
    budget_reservations,
    jobs,
    plans,
    spending_confirmations,
    spending_origins,
    spending_settings,
)
from studio_platform.spending_limits import (
    CREDIT_DEFAULTS,
    USER_DEFAULTS,
    SpendingConfirmationRequired,
    check_submission_confirmation,
    credit_settings,
    read_settings,
)
import test_credits as fixtures


class CreditSpendingTests(fixtures.CreditTests):
    setUp = fixtures.CreditTests.setUp
    policy = fixtures.CreditTests.policy
    seed = fixtures.CreditTests.seed
    balance = fixtures.CreditTests.balance
    complete = fixtures.CreditTests.complete
    assert_invariants = fixtures.CreditTests.assert_invariants

    def configure(
        self,
        *,
        daily=0,
        ask=0,
        connections=None,
        usd_daily=0,
        usd_ask=0,
        usd_connections=None,
    ):
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
            version = old["version"] + 1 if old else 1
            value = {
                "daily_limit_microusd": usd_daily,
                "ask_above_microusd": usd_ask,
                "connection_limits": usd_connections or {},
                "daily_limit_credits": daily,
                "ask_above_credits": ask,
                "connection_credit_limits": connections or {},
            }
            change = dict(
                value=value,
                version=version,
                updated_at=self.now,
                actor_id="browser:superdan",
            )
            if old:
                connection.execute(
                    update(spending_settings).where(*condition).values(**change)
                )
            else:
                connection.execute(
                    insert(spending_settings).values(
                        tenant_id=self.scope.tenant_id,
                        owner_id=self.scope.owner_id,
                        **change,
                    )
                )
            return version

    def plan(self, amount=20, *, cost=100_000, quoted=True):
        execution = {
            "pool": "test-pool",
            "expected_runtime_s": 120,
            "model_id": "test-model-only",
        }
        if quoted:
            execution["credit_quote"] = {
                "version": 1,
                "enforced": True,
                "user_id": self.user_id,
                "credits": amount,
                "price_revision": "price-1",
                "tier": "standard",
            }
        return self.repo.create_plan(
            self.scope,
            {"prompt": "Synthetic spending boundary"},
            execution,
            expires_at=self.now + 1000,
            estimated_cost_microusd=cost,
        )

    def submit(
        self,
        plan=None,
        *,
        actor="browser:superdan",
        key=None,
        required=False,
        version=None,
    ):
        return self.repo.create_job(
            self.scope,
            (plan or self.plan())["id"],
            key or uuid.uuid4().hex,
            budget_account_ids=("owner-budget",),
            spending_actor=actor,
            confirmation_required=required,
            spending_settings_version=version,
        )

    def confirm(
        self, plan, *, actor="browser:superdan", version=None, digest=None, expires=None
    ):
        with self.repo.transaction() as connection:
            current = read_settings(
                connection, self.scope.tenant_id, self.scope.owner_id
            )
            connection.execute(
                insert(spending_confirmations).values(
                    tenant_id=self.scope.tenant_id,
                    owner_id=self.scope.owner_id,
                    actor_id=actor,
                    plan_id=plan["id"],
                    plan_hash=digest or plan["plan_hash"],
                    settings_version=current["version"] if version is None else version,
                    expires_at=self.now + 100 if expires is None else expires,
                )
            )

    def enable(self, *, daily=0, ask=0, connections=None, **usd):
        self.policy()
        self.seed(1000)
        return self.configure(daily=daily, ask=ask, connections=connections, **usd)

    def test_credit_daily_cap_is_atomic_without_reinterpreting_old_usd_cap(self):
        self.enable(daily=30, usd_daily=1, usd_ask=1)
        selected = [self.plan(), self.plan()]

        def run(plan):
            try:
                return self.submit(plan)["created"]
            except credits.CreditError as error:
                self.assertEqual(error.code, "user_daily_limit_reached")
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(run, selected)), 1)
        self.assertEqual(
            (self.balance()["held"], self.balance()["daily_limit"]), (20, 30)
        )
        with self.repo.engine.connect() as connection:
            stored = read_settings(
                connection, self.scope.tenant_id, self.scope.owner_id
            )
            self.assertEqual(stored["value"]["daily_limit_microusd"], 1)
            self.assertEqual(stored["value"]["ask_above_microusd"], 1)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )
        self.assert_invariants()

    def test_legacy_usd_admission_still_uses_retained_usd_cap(self):
        self.configure(usd_daily=50_000, daily=1000)
        with self.assertRaisesRegex(BudgetExceeded, "user_daily_limit_reached"):
            self.submit(self.plan(quoted=False))
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)

    def test_agent_credit_cap_uses_original_actor_and_pending_holds_across_days(self):
        self.now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc).timestamp()
        self.enable(
            connections={"key:one": 25, "key:two": 100},
            usd_connections={"key:one": 1, "key:two": 1},
        )
        job = self.submit(actor="key:one")
        self.now += 86400
        with self.assertRaises(credits.CreditError) as refusal:
            self.submit(actor="key:one")
        self.assertEqual(
            (
                refusal.exception.code,
                {k: refusal.exception.data[k] for k in ('need', 'have')},
            ),
            ("connection_daily_limit_reached", {"need": 20, "have": 5}),
        )
        self.assertTrue(refusal.exception.data['messages']['en'])
        self.assertTrue(refusal.exception.data['messages']['zh'])
        self.submit(actor="key:two")
        # Additional provider budget attribution may not rewrite the original
        # customer obligation or its connection even after midnight.
        self.repo.configure_budget(
            "extra-credit-layer",
            tenant_id=self.scope.tenant_id,
            limit_microusd=1_000_000,
        )
        with self.repo.transaction() as connection:
            self.repo._reserve(
                connection,
                self.scope,
                ("extra-credit-layer",),
                "job",
                job["id"],
                100_000,
                spending_actor="key:two",
            )
            origin = connection.execute(
                select(spending_origins.c.actor_id).where(
                    spending_origins.c.reference_id == job["id"]
                )
            ).scalar_one()
            self.assertEqual(origin, "key:one")
            self.assertEqual(
                credits.daily_projection(
                    connection,
                    self.scope.tenant_id,
                    self.scope.owner_id,
                    self.now,
                    "key:one",
                ),
                20,
            )
        self.assertEqual(self.balance()["held"], 40)
        self.assert_invariants()

    def test_aggregate_confirmation_uses_frozen_credits_and_single_use_grants(self):
        version = self.enable(ask=25, usd_ask=1)
        selected = [self.plan(), self.plan()]
        principal = Principal(self.scope.owner_id, "browser:superdan")
        with self.assertRaises(SpendingConfirmationRequired) as refusal:
            check_submission_confirmation(
                self.repo, self.scope.tenant_id, principal, [p["id"] for p in selected]
            )
        self.assertEqual(
            (refusal.exception.amount, refusal.exception.unit), (40, "credits")
        )
        for plan in selected:
            self.confirm(plan)
        check = check_submission_confirmation(
            self.repo, self.scope.tenant_id, principal, [p["id"] for p in selected]
        )
        self.assertEqual((check.required, check.version), (True, version))
        original = []
        for index, plan in enumerate(selected):
            original.append(
                self.submit(
                    plan,
                    key="approved-" + str(index),
                    required=check.required,
                    version=check.version,
                )
            )
        self.assertFalse(
            self.submit(selected[0], key="approved-0", required=True, version=version)[
                "created"
            ]
        )
        with self.assertRaises(SpendingConfirmationRequired):
            self.submit(
                selected[0], key="duplicate-paid-job", required=True, version=version
            )
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                set(
                    connection.execute(
                        select(spending_confirmations.c.consumed_reference)
                    ).scalars()
                ),
                {job["id"] for job in original},
            )
        self.assertEqual(self.balance()["held"], 40)
        self.assert_invariants()

    def test_confirmation_fences_hash_version_actor_and_expiry_without_any_reservation(
        self,
    ):
        self.enable(ask=1)
        cases = (
            {"digest": "0" * 64},
            {"version": 0},
            {"actor": "browser:supervan"},
            {"expires": self.now - 1},
        )
        for values in cases:
            plan = self.plan()
            self.confirm(plan, **values)
            with (
                self.subTest(values=values),
                self.assertRaises(SpendingConfirmationRequired),
            ):
                self.submit(plan)
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(jobs)).all(), [])
            self.assertEqual(connection.execute(select(budget_reservations)).all(), [])
        self.assertEqual(self.balance()["held"], 0)

    def test_changed_settings_after_precheck_cannot_bypass_new_credit_threshold(self):
        self.enable(ask=100)
        selected = [self.plan(), self.plan()]
        principal = Principal(self.scope.owner_id, "browser:superdan")
        check = check_submission_confirmation(
            self.repo, self.scope.tenant_id, principal, [p["id"] for p in selected]
        )
        self.assertFalse(check)
        self.configure(ask=25)
        with self.assertRaisesRegex(Conflict, "spending_settings_changed"):
            self.submit(selected[0], required=check.required, version=check.version)
        self.assertEqual(self.balance()["held"], 0)

    def test_mixed_credit_and_legacy_batch_does_not_obscure_confirmation_units(self):
        self.enable(ask=1)
        selected = [self.plan(), self.plan(quoted=False)]
        with self.assertRaisesRegex(Conflict, "credit_quote_required"):
            check_submission_confirmation(
                self.repo,
                self.scope.tenant_id,
                Principal(self.scope.owner_id, "browser:superdan"),
                [p["id"] for p in selected],
            )

    def test_policy_disable_cannot_reuse_credit_confirmation_as_usd_approval(self):
        self.enable(ask=1, usd_ask=1)
        plan = self.plan()
        self.confirm(plan)
        check = check_submission_confirmation(
            self.repo,
            self.scope.tenant_id,
            Principal(self.scope.owner_id, "browser:superdan"),
            [plan["id"]],
        )
        self.assertTrue(check.required)
        self.policy(False)
        with self.assertRaisesRegex(Conflict, "credit_policy_changed_repreflight"):
            self.submit(plan, required=check.required, version=check.version)
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(jobs)).all(), [])
            self.assertEqual(connection.execute(select(budget_reservations)).all(), [])
            self.assertIsNone(
                connection.execute(
                    select(spending_confirmations.c.consumed_reference).where(
                        spending_confirmations.c.plan_id == plan["id"]
                    )
                ).scalar_one()
            )
        self.assertEqual(self.balance()["held"], 0)

    def test_old_and_credit_client_settings_updates_preserve_other_currency(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings

        self.policy()
        app = create_app(
            Settings(
                Path(self.temp.name) / "spending-api",
                tenant_id=self.scope.tenant_id,
                auth_mode="local-test",
            ),
            repository=self.repo,
        )
        with TestClient(app) as client:
            self.assertEqual(
                client.post(
                    "/api/auth/login", json={"username": "superdan"}
                ).status_code,
                200,
            )
            path = "/v1/account/spending-settings"
            first = client.put(
                path,
                json={
                    "expected_version": 0,
                    "value": {
                        **CREDIT_DEFAULTS,
                        "daily_limit_credits": 90,
                        "ask_above_credits": 35,
                    },
                },
            )
            self.assertEqual(first.status_code, 200, first.text)
            second = client.put(
                path,
                json={
                    "expected_version": 1,
                    "value": {
                        **USER_DEFAULTS,
                        "daily_limit_microusd": 123456,
                        "ask_above_microusd": 7890,
                    },
                },
            )
            self.assertEqual(second.status_code, 200, second.text)
            stale = client.put(
                path, json={"expected_version": 0, "value": CREDIT_DEFAULTS}
            )
            self.assertEqual(stale.status_code, 409, stale.text)
            with self.repo.engine.connect() as connection:
                value = read_settings(
                    connection, self.scope.tenant_id, self.scope.owner_id
                )["value"]
                self.assertEqual(
                    (value["daily_limit_credits"], value["ask_above_credits"]), (90, 35)
                )
                self.assertEqual(
                    (value["daily_limit_microusd"], value["ask_above_microusd"]),
                    (123456, 7890),
                )
                self.assertEqual(
                    credit_settings(
                        connection, self.scope.tenant_id, self.scope.owner_id
                    )["value"]["daily_limit_credits"],
                    90,
                )
            shown = client.get(path).json()
            self.assertEqual(shown["unit"], "credits")
            self.assertNotIn("microusd", str(shown))
            third = client.put(
                path,
                json={
                    "expected_version": 2,
                    "value": {
                        **CREDIT_DEFAULTS,
                        "daily_limit_credits": 110,
                    },
                },
            )
            self.assertEqual(third.status_code, 200, third.text)
            with self.repo.engine.connect() as connection:
                self.assertEqual(
                    read_settings(
                        connection, self.scope.tenant_id, self.scope.owner_id
                    )["value"]["daily_limit_microusd"],
                    123456,
                )
            foreign = client.put(
                path,
                json={
                    "expected_version": 3,
                    "value": {
                        **CREDIT_DEFAULTS,
                        "connection_credit_limits": {"key:foreign": 20},
                    },
                },
            )
            self.assertEqual(foreign.status_code, 404, foreign.text)


def load_tests(_loader, _standard, _pattern):
    # Reuse the original fixture, not its already-covered inherited core tests.
    return unittest.TestSuite(
        CreditSpendingTests(name)
        for name in CreditSpendingTests.__dict__
        if name.startswith("test_")
    )
