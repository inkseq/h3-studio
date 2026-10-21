"""Zero-allowance onboarding/backfill on disposable SQL, never provider access."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch
import uuid

from sqlalchemy import Column, MetaData, String, Table, insert, select, update
from fastapi.testclient import TestClient

from studio_platform.api import create_app
from studio_platform.auth import Auth, Principal
from studio_platform.credit_schema import metadata as credit_metadata, entries
from studio_platform.credits_pricing import DEFAULT_RATES, PricingError
from studio_platform.credits_routes import CreditAPI
from studio_platform import credits, generation_budget_binding as binding
from studio_platform.identity_schema import (
    metadata as identity_metadata,
    admin_audit,
    users,
)
from studio_platform.repository import Conflict, budget_accounts
from studio_platform.settings import Settings
from studio_platform.workos_auth import WorkOSAuth
from test_platform_execution_policy import policy
from test_platform_repository import LedgerCase


class GenerationBudgetBindingTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.path = Path(self.temp.name) / "policy.json"
        self.value = policy(self.now)
        self.value["budget_accounts"].append("project:{owner_id}:{project_id}")
        self.path.write_text(json.dumps(self.value), encoding="utf-8")
        self.path.chmod(0o600)
        self.settings = Settings(
            Path(self.temp.name),
            tenant_id=self.scope.tenant_id,
            execution_policy_file=self.path,
        )
        self.ids = {owner: str(uuid.uuid4()) for owner in ("superdan", "supervan")}
        version = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), primary_key=True),
        )
        with self.repo.transaction() as connection:
            identity_metadata.create_all(connection)
            credit_metadata.create_all(connection)
            version.create(connection)
            connection.execute(insert(version).values(version_num="20261019_0004"))
            for owner, ident in self.ids.items():
                connection.execute(
                    insert(users).values(
                        id=ident,
                        tenant=self.settings.tenant_id,
                        owner=owner,
                        role="admin" if owner == "superdan" else "user",
                        status="active",
                        display_name=owner,
                        email_verified=0,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
        self.api = CreditAPI(self.repo, self.settings)
        self.admin = Principal("superdan", "browser:superdan", role="admin")
        self.user = Principal("supervan", "browser:supervan")
        self.auth = Auth(
            self.repo.engine, mode="local-test", tenant=self.settings.tenant_id
        )
        self.workos = WorkOSAuth(self.repo, self.auth, self.settings)

    def rows(self, table):
        with self.repo.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(table).order_by(table.c.id)
                ).mappings()
            ]

    def bind(self, owner="supervan", *, key="binding", actor=None, **body):
        return self.api.bind_generation_budget(
            actor or self.admin,
            self.ids[owner],
            {"reason": "Explicit test backfill", **body},
            key=key,
        )

    def signup(self):
        return self.workos._upsert(
            {
                "subject": "user_" + "A" * 24,
                "email": "person@example.test",
                "display_name": "New Creator",
            },
            {"purpose": "signin"},
            None,
        )

    def publish(self):
        self.api.publish(
            self.admin,
            {
                "expected_revision": None,
                "rates": dict(DEFAULT_RATES),
                "welcome_credits": 200,
            },
            key="prices",
        )

    def test_explicit_backfill_is_zero_scoped_audited_and_idempotent(self):
        old = self.rows(budget_accounts)
        result = self.bind()
        expected = "test-owner:test-tenant:supervan"
        self.assertEqual(
            result,
            {"user_id": self.ids["supervan"], "created": [expected], "existing": []},
        )
        row = self.repo.get_budget(expected)
        self.assertEqual(
            (row["tenant_id"], row["owner_id"], row["project_id"]),
            ("test-tenant", "supervan", None),
        )
        self.assertEqual(
            (row["limit_microusd"], row["reserved_microusd"], row["spent_microusd"]),
            (0, 0, 0),
        )
        self.assertEqual(self.bind(), result)
        self.assertEqual(
            self.bind(key="again"),
            {"user_id": self.ids["supervan"], "created": [], "existing": [expected]},
        )
        self.assertEqual(len(self.rows(admin_audit)), 1)
        self.assertEqual(
            self.rows(admin_audit)[0]["actor_user_id"], self.ids["superdan"]
        )
        self.assertFalse(self.rows(admin_audit)[0]["after"]["allowance_granted"])
        self.assertEqual(
            [value for value in self.rows(budget_accounts) if value["id"] != expected],
            old,
        )
        self.assertEqual(self.rows(entries), [])

    def test_concurrent_backfill_creates_one_row_and_audit(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(lambda i: self.bind(key="parallel-" + str(i)), range(8))
            )
        self.assertEqual(sum(len(value["created"]) for value in results), 1)
        self.assertEqual(len(self.rows(admin_audit)), 1)

    def test_existing_financial_fields_are_preserved_and_conflicts_roll_back(self):
        expected = "test-owner:test-tenant:supervan"
        self.repo.configure_budget(
            expected, tenant_id="test-tenant", owner_id="supervan", limit_microusd=900
        )
        with self.repo.transaction() as connection:
            connection.execute(
                update(budget_accounts)
                .where(budget_accounts.c.id == expected)
                .values(reserved_microusd=100, spent_microusd=200)
            )
        before = self.rows(budget_accounts)
        self.assertEqual(self.bind()["existing"], [expected])
        self.assertEqual(self.rows(budget_accounts), before)
        self.assertEqual(self.rows(admin_audit), [])
        for values in (
            {"tenant_id": "another"},
            {"owner_id": "other"},
            {"project_id": "other"},
        ):
            with self.subTest(values=values):
                with self.repo.transaction() as connection:
                    connection.execute(
                        update(budget_accounts)
                        .where(budget_accounts.c.id == expected)
                        .values(
                            **{
                                "tenant_id": "test-tenant",
                                "owner_id": "supervan",
                                "project_id": None,
                                **values,
                            }
                        )
                    )
                with self.assertRaisesRegex(Conflict, "budget_scope_conflict"):
                    self.bind(key="conflict-" + next(iter(values)))
                self.assertEqual(self.rows(admin_audit), [])

    def test_backfill_requires_human_admin_and_exact_canonical_user(self):
        for actor in (self.user, Principal("superdan", "key:test", machine=True)):
            with (
                self.subTest(actor=actor),
                self.assertRaisesRegex(PricingError, "admin_required"),
            ):
                self.bind(actor=actor)
        with self.assertRaisesRegex(PricingError, "generation_budget_binding_invalid"):
            self.bind(limit_microusd=10000000)
        with self.assertRaisesRegex(PricingError, "idempotency_key_required"):
            self.bind(key=None)
        with self.repo.transaction() as connection:
            connection.execute(
                update(users)
                .where(users.c.id == self.ids["supervan"])
                .values(tenant="another")
            )
        with self.assertRaisesRegex(PricingError, "user_not_found"):
            self.bind()
        self.assertEqual(len(self.rows(budget_accounts)), 1)
        self.assertEqual(self.rows(admin_audit), [])

    def test_readers_do_not_backfill_and_existing_login_stays_read_only_for_budget(
        self,
    ):
        before = self.rows(budget_accounts)
        self.api.balance(self.user)
        self.api.policy(self.admin)
        self.assertEqual(self.rows(budget_accounts), before)
        self.assertEqual(self.rows(admin_audit), [])

    def test_backfill_http_auth_csrf_key_and_replay_contract(self):
        app = create_app(
            replace(self.settings, auth_mode="local-test"), repository=self.repo
        )
        url = "/v1/admin/users/" + self.ids["supervan"] + "/generation-budget-binding"
        body = {"reason": "Explicit HTTP test backfill"}
        headers = {"Idempotency-Key": "http-binding"}
        with TestClient(app) as client:
            self.assertEqual(
                client.post(url, json=body, headers=headers).status_code, 401
            )
            client.post(
                "/api/auth/login", json={"username": "supervan"}
            ).raise_for_status()
            self.assertEqual(
                client.post(url, json=body, headers=headers).status_code, 403
            )
            client.post(
                "/api/auth/login", json={"username": "superdan"}
            ).raise_for_status()
            self.assertEqual(
                client.post(
                    url,
                    json=body,
                    headers={**headers, "Origin": "https://untrusted.example"},
                ).status_code,
                403,
            )
            self.assertEqual(client.post(url, json=body).status_code, 422)
            result = client.post(url, json=body, headers=headers)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.headers["cache-control"], "no-store")
            self.assertEqual(
                client.post(url, json=body, headers=headers).json(), result.json()
            )
            self.assertEqual(
                client.post(
                    url, json={"reason": "Changed"}, headers=headers
                ).status_code,
                409,
            )
        self.assertEqual(len(self.rows(admin_audit)), 1)

    def test_absent_retired_legacy_cap_cannot_turn_zero_binding_into_authority(self):
        self.value["budget_accounts"] = ["demand-job-owner-{owner_id}-20261004"]
        self.path.write_text(json.dumps(self.value), encoding="utf-8")
        self.settings = replace(self.settings, tenant_id="sixnine")
        self.api = CreditAPI(self.repo, self.settings)
        with self.repo.transaction() as connection:
            connection.execute(update(users).values(tenant="sixnine"))
        before = self.rows(budget_accounts)
        with self.assertRaisesRegex(
            PricingError, "generation_budget_binding_requires_review"
        ):
            self.bind()
        self.assertEqual(self.rows(budget_accounts), before)
        self.assertEqual(self.rows(admin_audit), [])

    def test_new_workos_user_binding_and_welcome_are_atomic_and_retry_safe(self):
        self.publish()
        before = self.rows(budget_accounts)
        with ThreadPoolExecutor(max_workers=3) as executor:
            people = list(executor.map(lambda _: self.signup(), range(3)))
        self.assertEqual(len({row["id"] for row in people}), 1)
        person = people[0]
        expected = "test-owner:test-tenant:" + person["owner"]
        self.assertEqual(self.repo.get_budget(expected)["limit_microusd"], 0)
        self.assertEqual(len(self.rows(entries)), 1)
        self.assertEqual(self.rows(entries)[0]["amount"], 200)
        audits = [
            value
            for value in self.rows(admin_audit)
            if value["action"] == "generation budget bound"
        ]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["target_user_id"], person["id"])
        self.assertEqual(
            [value for value in self.rows(budget_accounts) if value["id"] != expected],
            before,
        )

    def test_binding_audit_or_welcome_failure_rolls_back_identity_budget_and_grant(
        self,
    ):
        self.publish()
        before = {
            table.name: self.rows(table)
            for table in (users, budget_accounts, admin_audit, entries)
        }
        original_insert = binding.insert

        def no_audit(table):
            if table is admin_audit:
                raise RuntimeError("synthetic_audit_failure")
            return original_insert(table)

        with (
            patch.object(binding, "insert", side_effect=no_audit),
            self.assertRaisesRegex(RuntimeError, "synthetic_audit_failure"),
        ):
            self.signup()
        with (
            patch.object(
                credits,
                "grant",
                side_effect=credits.CreditError("synthetic_grant_failure"),
            ),
            self.assertRaisesRegex(credits.CreditError, "synthetic_grant_failure"),
        ):
            self.signup()
        for table in (users, budget_accounts, admin_audit, entries):
            self.assertEqual(self.rows(table), before[table.name])
