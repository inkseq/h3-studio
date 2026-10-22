"""WorkOS welcome and original reviewed billing wired to customer credits."""

from concurrent.futures import ThreadPoolExecutor
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import Column, MetaData, String, Table, insert, select, update

from studio_platform import credits
from studio_platform.auth import Principal, sessions
from studio_platform.credit_schema import (
    accounts,
    entries,
    metadata as credit_metadata,
    prices,
)
from studio_platform.credits_pricing import (
    DEFAULT_CONFIGURATION,
    DEFAULT_RATES,
    PricingError,
    quote,
)
from studio_platform.credits_routes import CreditAPI
from studio_platform.identity_schema import metadata as identity_metadata, users
from studio_platform.queue import TaskQueue
from studio_platform.repository import (
    attempts,
    billing_adjustments,
    budget_accounts,
    budget_reservations,
    jobs,
    spending_origins,
)
from studio_platform.workos_auth import WorkOSError
import test_operator_billing as billing_fixture
import test_workos_auth as auth_fixture


class CreditIdentityBillingTests(unittest.TestCase):
    def fixture(self, case, method):
        value = case(method)
        value.setUp()
        self.addCleanup(value.doCleanups)
        self.addCleanup(value.tearDown)
        return value

    def adopt(self, fixture, *, version=None, welcome=0, enabled=True, publish=True):
        tenant = fixture.settings.tenant_id
        if version is None:
            version = Table(
                "alembic_version",
                MetaData(),
                Column("version_num", String(32), primary_key=True),
            )
            version.create(fixture.repo.engine)
            with fixture.repo.transaction() as connection:
                connection.execute(insert(version).values(version_num="20261019_0004"))
        with fixture.repo.transaction() as connection:
            identity_metadata.create_all(connection)
            credit_metadata.create_all(connection)
            connection.execute(update(version).values(version_num="20261019_0004"))
            for owner in ("superdan", "supervan"):
                if (
                    connection.execute(
                        select(users.c.id).where(
                            users.c.tenant == tenant, users.c.owner == owner
                        )
                    ).first()
                    is None
                ):
                    connection.execute(
                        insert(users).values(
                            id=str(uuid.uuid4()),
                            tenant=tenant,
                            owner=owner,
                            role="admin" if owner == "superdan" else "user",
                            status="active",
                            display_name=owner,
                            created_at=fixture.now,
                            updated_at=fixture.now,
                        )
                    )
        service = CreditAPI(fixture.repo, fixture.settings)
        actor = getattr(fixture, "legacy", None) or fixture.admin
        if not publish:
            return service
        price = service.publish(
            actor,
            {
                "expected_revision": None,
                "rates": dict(DEFAULT_RATES),
                "welcome_credits": welcome,
            },
            key="wire-prices",
        )
        if enabled:
            service.policy(
                actor,
                {
                    "expected_revision": price["revision"],
                    "enabled": True,
                    "reason": "Disposable wire acceptance",
                },
                key="wire-enable",
            )
        return service

    def rows(self, fixture, table):
        with fixture.repo.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(table)).mappings()]

    def test_new_workos_welcome_is_atomic_and_repeat_or_legacy_link_never_regrants(
        self,
    ):
        fixture = self.fixture(
            auth_fixture.WorkOSAuthTests,
            "test_config_requires_protected_credentials_and_adopted_schema",
        )
        service = self.adopt(
            fixture, version=fixture.version, welcome=200, enabled=False
        )
        policy = service.policy(fixture.legacy)
        self.assertFalse(policy["enabled"])
        value, state = fixture.start()
        principal = fixture.auth.session(fixture.finish(value, state)["token"])
        self.assertEqual(service.balance(principal)["balance"], 200)
        value, state = fixture.start()
        repeated = fixture.auth.session(fixture.finish(value, state)["token"])
        self.assertEqual(repeated.owner, principal.owner)
        self.assertEqual(service.balance(repeated)["balance"], 200)
        grants = self.rows(fixture, entries)
        self.assertEqual(len(grants), 1)
        self.assertEqual(
            (grants[0]["kind"], grants[0]["amount"], grants[0]["reference_type"]),
            ("grant", 200, "signup"),
        )
        self.assertEqual(grants[0]["idempotency_key"], "welcome:" + principal.user_id)
        self.assertEqual(grants[0]["price_revision"], policy["active_revision"])
        self.assertEqual(grants[0]["origin"], "workos")
        self.assertFalse(service.policy(fixture.legacy)["enabled"])
        fixture.provider.user.id = "user_" + "Z" * 24
        fixture.provider.user.email = "linked-legacy@example.test"
        value, state = fixture.start(link=True, principal=fixture.legacy)
        linked = fixture.auth.session(
            fixture.finish(value, state, principal=fixture.legacy)["token"]
        )
        self.assertEqual(linked.owner, "superdan")
        self.assertEqual(service.balance(linked)["balance"], 0)
        self.assertEqual(self.rows(fixture, entries), grants)

    def test_welcome_failure_rolls_back_new_identity_and_never_replays_sdk_exchange(
        self,
    ):
        fixture = self.fixture(
            auth_fixture.WorkOSAuthTests,
            "test_config_requires_protected_credentials_and_adopted_schema",
        )
        self.adopt(fixture, version=fixture.version, welcome=200, enabled=False)
        original_sessions = self.rows(fixture, sessions)
        value, state = fixture.start()
        with patch.object(
            credits,
            "grant",
            side_effect=credits.CreditError("synthetic_welcome_refusal"),
        ):
            with self.assertRaisesRegex(
                WorkOSError, "workos_provider_unavailable"
            ) as failure:
                fixture.finish(value, state)
        self.assertEqual(failure.exception.status, 503)
        self.assertNotIn("synthetic_welcome_refusal", str(failure.exception))
        self.assertEqual(fixture.count_users(), 2)
        self.assertEqual(self.rows(fixture, entries), [])
        self.assertEqual(self.rows(fixture, accounts), [])
        self.assertEqual(self.rows(fixture, sessions), original_sessions)
        with self.assertRaisesRegex(WorkOSError, "workos_state_invalid"):
            fixture.finish(value, state)
        self.assertEqual(len(fixture.provider.auth_calls), 1)

    def test_disposable_or_unknown_classification_allows_login_without_grant(self):
        for email, missing_list in (
            ("person@sub.MAILINATOR.com", False),
            ("person@example.test", True),
        ):
            with self.subTest(email=email, missing_list=missing_list):
                fixture = self.fixture(
                    auth_fixture.WorkOSAuthTests,
                    "test_config_requires_protected_credentials_and_adopted_schema",
                )
                service = self.adopt(
                    fixture, version=fixture.version, welcome=200, enabled=False
                )
                fixture.provider.user.email = email
                value, state = fixture.start()
                with patch.dict(
                    sys.modules,
                    {"disposable_email_domains": None} if missing_list else {},
                ):
                    principal = fixture.auth.session(
                        fixture.finish(value, state)["token"]
                    )
                self.assertTrue(principal.owner.startswith("u_"))
                self.assertEqual(service.balance(principal)["balance"], 0)
                self.assertEqual(self.rows(fixture, accounts), [])
                self.assertEqual(self.rows(fixture, entries), [])

    def test_unadopted_credits_allow_workos_signup_without_grant(self):
        fixture = self.fixture(
            auth_fixture.WorkOSAuthTests,
            "test_config_requires_protected_credentials_and_adopted_schema",
        )
        value, state = fixture.start()
        with patch.object(credits, "grant") as grant:
            principal = fixture.auth.session(fixture.finish(value, state)["token"])
        self.assertTrue(principal.owner.startswith("u_"))
        self.assertEqual(fixture.count_users(), 3)
        grant.assert_not_called()

    def test_default_zero_and_unpublished_prices_do_not_grant(self):
        for published in (False, True):
            with self.subTest(published=published):
                fixture = self.fixture(
                    auth_fixture.WorkOSAuthTests,
                    "test_config_requires_protected_credentials_and_adopted_schema",
                )
                self.adopt(
                    fixture, version=fixture.version, enabled=False, publish=published
                )
                value, state = fixture.start()
                # A future default change cannot authorize an unpublished grant.
                with patch.dict(
                    DEFAULT_CONFIGURATION,
                    {"welcome_credits": 0 if published else 200},
                ):
                    principal = fixture.auth.session(
                        fixture.finish(value, state)["token"]
                    )
                self.assertTrue(principal.owner.startswith("u_"))
                self.assertEqual(self.rows(fixture, entries), [])
                self.assertEqual(self.rows(fixture, accounts), [])

    def test_published_revision_changes_only_future_signups_and_not_existing_qa(self):
        fixture = self.fixture(
            auth_fixture.WorkOSAuthTests,
            "test_config_requires_protected_credentials_and_adopted_schema",
        )
        service = self.adopt(fixture, version=fixture.version, enabled=False)
        value, state = fixture.start()
        existing = fixture.auth.session(fixture.finish(value, state)["token"])
        original_prices = self.rows(fixture, prices)
        publication = service.publish(
            fixture.legacy,
            {
                "expected_revision": original_prices[0]["revision"],
                "rates": dict(DEFAULT_RATES),
                "welcome_credits": 200,
                "reason": "Disposable future-signup policy acceptance",
            },
            key="future-welcome",
        )
        value, state = fixture.start()
        replay = fixture.auth.session(fixture.finish(value, state)["token"])
        self.assertEqual(replay.owner, existing.owner)
        self.assertEqual(service.balance(replay)["balance"], 0)
        self.assertEqual(service.balance(fixture.legacy)["balance"], 0)
        self.assertEqual(self.rows(fixture, entries), [])
        fixture.provider.user.id = "user_" + "B" * 24
        fixture.provider.user.email = "new-person@example.test"
        value, state = fixture.start()
        new = fixture.auth.session(fixture.finish(value, state)["token"])
        self.assertEqual(service.balance(new)["balance"], 200)
        self.assertEqual(
            self.rows(fixture, entries)[0]["price_revision"], publication["revision"]
        )
        self.assertIn(original_prices[0], self.rows(fixture, prices))
        self.assertFalse(service.policy(fixture.legacy)["enabled"])

    def test_same_verified_email_with_different_subjects_can_only_grant_once(self):
        fixture = self.fixture(
            auth_fixture.WorkOSAuthTests,
            "test_config_requires_protected_credentials_and_adopted_schema",
        )
        self.adopt(fixture, version=fixture.version, welcome=200, enabled=False)

        def signup(index):
            identity = fixture.service._provider_user(
                SimpleNamespace(
                    id="user_" + ("C" if index else "D") * 24,
                    email="PERSON@EXAMPLE.TEST" if index else "person@example.test",
                    email_verified=True,
                    first_name="Synthetic",
                    last_name="Signup",
                )
            )
            try:
                return fixture.service._upsert(identity, {"purpose": "signin"}, None)
            except WorkOSError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(signup, range(2)))
        self.assertEqual(sum(type(value) is dict for value in outcomes), 1)
        self.assertIn("workos_link_required", outcomes)
        self.assertEqual(fixture.count_users(), 3)
        self.assertEqual([row["amount"] for row in self.rows(fixture, entries)], [200])

    def test_corrupt_published_pricing_rolls_back_identity_and_session(self):
        fixture = self.fixture(
            auth_fixture.WorkOSAuthTests,
            "test_config_requires_protected_credentials_and_adopted_schema",
        )
        self.adopt(fixture, version=fixture.version, welcome=200, enabled=False)
        with fixture.repo.transaction() as connection:
            connection.execute(
                update(prices).values(configuration={"welcome_credits": 200})
            )
        original_sessions = self.rows(fixture, sessions)
        value, state = fixture.start()
        with self.assertRaisesRegex(
            WorkOSError, "workos_provider_unavailable"
        ) as failure:
            fixture.finish(value, state)
        self.assertEqual(failure.exception.status, 503)
        self.assertNotIn("credit_prices_invalid", str(failure.exception))
        self.assertEqual(fixture.count_users(), 2)
        self.assertEqual(self.rows(fixture, accounts), [])
        self.assertEqual(self.rows(fixture, entries), [])
        self.assertEqual(self.rows(fixture, sessions), original_sessions)

    def test_original_billing_waiver_refunds_captured_credits_once_and_retains_usd_history(
        self,
    ):
        fixture = self.fixture(
            billing_fixture.OperatorBillingTests,
            "test_actual_allocation_waiver_preserves_gross_and_originals",
        )
        service = self.adopt(fixture)
        _instance, worker = fixture.instance()
        owner = Principal("supervan", "key:van", machine=True)
        with fixture.repo.transaction() as connection:
            credits.grant(
                connection,
                fixture.settings.tenant_id,
                "supervan",
                100,
                actor="system:wire-test",
                reason="Disposable acceptance grant",
                idempotency_key="wire-grant",
                now=fixture.now,
            )
            compiled = {
                "recipe_id": "h3-base-fl2va-v1",
                "request": {"model": "MiniMax-H3-Pruned-Rank8-INT8"},
                "output_spec": {"width": 832, "height": 480, "frames": 124},
            }
            frozen = quote(connection, fixture.settings.tenant_id, "supervan", compiled)
        plan = fixture.repo.create_plan(
            fixture.owner,
            compiled,
            {"pool": "billing-test", "credit_quote": frozen},
            expires_at=fixture.now + 1000,
            estimated_cost_microusd=1000,
        )
        job = fixture.repo.create_job(
            fixture.owner, plan["id"], "wire-job", budget_account_ids=("van", "shared")
        )
        queue = TaskQueue(fixture.repo)
        claim = queue.claim(worker, job["pool"], job_ids=[job["id"]])
        self.assertIsNotNone(claim)
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "synthetic-" + job["id"])
        queue.begin_collection(claim.lease)
        queue.complete(
            claim.lease,
            [
                {
                    "kind": "video",
                    "object_key": "synthetic/wire.mp4",
                    "size_bytes": 1,
                    "sha256": "a" * 64,
                    "validated": True,
                }
            ],
            actual_cost_microusd=300,
        )
        self.assertEqual(service.balance(owner)["balance"], 79)
        tables = (
            jobs,
            attempts,
            budget_accounts,
            budget_reservations,
            spending_origins,
        )
        before = {table.name: self.rows(fixture, table) for table in tables}
        _preview, body, result = fixture.execute(
            fixture.request("waive", [("job", job["id"], {})]), key="wire-waiver"
        )
        self.assertEqual(
            fixture.service.apply(fixture.admin, body, "wire-waiver"), result
        )
        self.assertEqual(service.balance(owner)["balance"], 100)
        for table in tables:
            self.assertEqual(self.rows(fixture, table), before[table.name])
        self.assertEqual(len(self.rows(fixture, billing_adjustments)), 1)
        ledger = self.rows(fixture, entries)
        self.assertEqual(
            [row["kind"] for row in ledger], ["grant", "hold", "capture", "waive"]
        )
        self.assertEqual(ledger[-1]["amount"], 21)
        self.assertEqual(fixture.repo.get_budget("van")["spent_microusd"], 300)
