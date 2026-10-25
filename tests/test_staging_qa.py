"""Staging-only QA provisioning/transport; synthetic secrets, no cloud or GPU."""

from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import bcrypt
from fastapi.testclient import TestClient
from sqlalchemy import Column, MetaData, String, Table, insert, select, update

from studio_platform.api import create_app
from studio_platform.auth import Auth, AuthenticationError, Principal, accounts
from studio_platform.identity_schema import admin_audit, metadata, users
from studio_platform.settings import Settings
from studio_platform.staging_qa import ORIGIN, StagingQA, StagingQAError, enabled
from tools import provision_staging_qa as tool
from test_platform_repository import LedgerCase

EXISTING_PASSWORD = "ExistingStagingFixturePassword_OnlyForOfflineTests"
QA_PASSWORDS = {"qa-admin": "A" * 48, "qa-user": "B" * 48, "qa-credit": "C" * 48}
EXISTING_HASH = bcrypt.hashpw(
    EXISTING_PASSWORD.encode(), bcrypt.gensalt(rounds=12)
).decode()


def body(*, grant=False):
    return {
        "accounts": [
            {"username": name, "role": role, "password": QA_PASSWORDS[name]}
            for name, role in (("qa-admin", "admin"), ("qa-user", "user"))
        ],
        "grant_test_credits": grant,
    }


def credit_body(*, grant=False):
    value = body(grant=grant)
    value["accounts"].append(
        {"username": "qa-credit", "role": "user", "password": QA_PASSWORDS["qa-credit"]}
    )
    return value


class StagingQATests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.auth = Auth(
            self.repo.engine, tenant=self.scope.tenant_id, clock=lambda: self.now
        )
        self.marker = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), primary_key=True),
        )
        self.ids = {name: str(uuid.uuid4()) for name in ("superdan", "supervan")}
        with self.repo.transaction() as connection:
            metadata.create_all(connection)
            self.marker.create(connection)
            connection.execute(insert(self.marker).values(version_num="20261019_0003"))
            for name, ident in self.ids.items():
                connection.execute(
                    insert(users).values(
                        id=ident,
                        tenant=self.scope.tenant_id,
                        owner=name,
                        role="admin" if name == "superdan" else "user",
                        status="active",
                        display_name=name,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
                connection.execute(
                    insert(accounts).values(
                        tenant=self.scope.tenant_id,
                        username=name,
                        password_hash=EXISTING_HASH,
                        disabled=0,
                        updated=self.now,
                    )
                )
        self.settings = Settings(
            Path(self.temp.name) / "api",
            database_url=self.url,
            tenant_id=self.scope.tenant_id,
            deployment_target="staging",
            public_origin=ORIGIN,
        )
        self.service = StagingQA(self.repo, self.settings)
        self.admin = Principal(
            "superdan", "browser:superdan", role="admin", identity_source="canonical"
        )

    def rows(self, table):
        with self.repo.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(table)).mappings()]

    def test_production_wrong_origin_alias_or_mode_refuses_before_any_database_access(
        self,
    ):
        for settings in (
            replace(self.settings, deployment_target="production"),
            replace(self.settings, public_origin="https://inkseq.com"),
            replace(self.settings, public_origin_aliases=("https://inkseq.com",)),
            replace(self.settings, auth_mode="local-test", public_origin=""),
        ):
            with (
                self.subTest(
                    settings=(
                        settings.deployment_target,
                        settings.public_origin,
                        settings.auth_mode,
                    )
                ),
                patch.object(
                    self.repo,
                    "transaction",
                    side_effect=AssertionError("No database access"),
                ),
                self.assertRaisesRegex(StagingQAError, "staging_qa_unavailable"),
            ):
                StagingQA(self.repo, settings).provision(self.admin, body())
        self.assertTrue(enabled(self.settings))

    def test_fixed_names_roles_and_fresh_human_admin_are_required(self):
        for mutate in (
            lambda value: value["accounts"][0].update(username="superdan"),
            lambda value: value["accounts"][1].update(role="admin"),
            lambda value: value["accounts"][1].update(
                username="qa-admin", role="admin"
            ),
            lambda value: value.update(grant_test_credits=2000),
        ):
            request = body()
            mutate(request)
            with self.assertRaisesRegex(StagingQAError, "staging_qa_request_invalid"):
                self.service.provision(self.admin, request)
        for principal in (
            Principal("supervan", "browser:supervan", role="admin"),
            Principal("superdan", "key:admin", machine=True, role="admin"),
        ):
            with self.assertRaisesRegex(StagingQAError, "admin_required"):
                self.service.provision(principal, body())
        with self.repo.transaction() as connection:
            connection.execute(
                update(users)
                .where(users.c.id == self.ids["superdan"])
                .values(status="suspended")
            )
        with self.assertRaisesRegex(StagingQAError, "admin_required"):
            self.service.provision(self.admin, body())
        self.assertEqual(len(self.rows(users)), 2)

    def test_atomic_creation_exact_replay_and_changed_password_never_reset_accounts(
        self,
    ):
        real_before = self.rows(accounts)
        first = self.service.provision(self.admin, body())
        stored = self.rows(accounts)
        self.assertTrue(all(row["created"] for row in first["accounts"]))
        replay = self.service.provision(self.admin, body())
        self.assertFalse(any(row["created"] for row in replay["accounts"]))
        self.assertEqual(stored, self.rows(accounts))
        self.assertEqual(
            [row for row in stored if row["username"] in self.ids], real_before
        )
        self.assertEqual(len(self.rows(admin_audit)), 2)
        changed = body()
        changed["accounts"][0]["password"] = "C" * 48
        with self.assertRaisesRegex(
            StagingQAError, "staging_qa_existing_account_changed"
        ):
            self.service.provision(self.admin, changed)
        self.assertEqual(stored, self.rows(accounts))
        self.assertNotIn(QA_PASSWORDS["qa-admin"], json.dumps(first))
        self.assertNotIn("password", json.dumps(self.rows(admin_audit)))

    def test_credit_extension_preserves_original_identities_credentials_and_budgets(
        self,
    ):
        from studio_platform.repository import budget_accounts

        self.service.provision(self.admin, body())
        original_users = self.rows(users)
        original_accounts = self.rows(accounts)
        original_budgets = self.rows(budget_accounts)
        first = self.service.provision(self.admin, credit_body())
        self.assertIsNone(first["credit_grant"])
        self.assertEqual(
            [row["username"] for row in first["accounts"] if row["created"]],
            ["qa-credit"],
        )
        self.assertEqual(
            [row for row in self.rows(users) if row["owner"] != "qa-credit"],
            original_users,
        )
        self.assertEqual(
            [row for row in self.rows(accounts) if row["username"] != "qa-credit"],
            original_accounts,
        )
        self.assertEqual(self.rows(budget_accounts), original_budgets)
        all_users, all_credentials = self.rows(users), self.rows(accounts)
        replay = self.service.provision(self.admin, credit_body())
        self.assertFalse(any(row["created"] for row in replay["accounts"]))
        self.service.provision(self.admin, body())  # Legacy clients remain valid.
        self.assertEqual(self.rows(users), all_users)
        self.assertEqual(self.rows(accounts), all_credentials)
        audit = self.rows(admin_audit)
        self.assertEqual(len(audit), 3)
        self.assertTrue(audit[-1]["after"]["is_test"])
        self.assertEqual(audit[-1]["after"]["username"], "qa-credit")
        self.assertNotIn(QA_PASSWORDS["qa-credit"], json.dumps(first))

    def test_credit_extension_failure_never_changes_original_pair(self):
        self.service.provision(self.admin, body())
        before = (self.rows(users), self.rows(accounts), self.rows(admin_audit))
        with patch.object(
            self.service, "_audit", side_effect=RuntimeError("offline audit refusal")
        ):
            with self.assertRaisesRegex(RuntimeError, "offline audit refusal"):
                self.service.provision(self.admin, credit_body())
        self.assertEqual(
            (self.rows(users), self.rows(accounts), self.rows(admin_audit)), before
        )
        for request in (
            {**credit_body(), "grant_test_credits": True},
            {**body(), "accounts": credit_body()["accounts"][1:]},
        ):
            with patch.object(
                self.repo,
                "transaction",
                side_effect=AssertionError("No DB for invalid set/grant"),
            ):
                with self.assertRaisesRegex(
                    StagingQAError, "staging_qa_request_invalid"
                ):
                    self.service.provision(self.admin, request)
        self.assertEqual(
            (self.rows(users), self.rows(accounts), self.rows(admin_audit)), before
        )

    def test_credit_password_identity_is_exact_staging_only(self):
        self.service.provision(self.admin, credit_body())
        staging = Auth(
            self.repo.engine,
            tenant=self.scope.tenant_id,
            staging_qa=True,
            clock=lambda: self.now,
        )
        token = staging.login(
            "qa-credit", QA_PASSWORDS["qa-credit"], source="credit-proof"
        )
        self.assertEqual(staging.session(token).owner, "qa-credit")
        self.assertEqual(staging.session(token).role, "user")
        self.assertIsNone(self.auth.session(token))
        with self.assertRaises(AuthenticationError):
            self.auth.login("qa-credit", QA_PASSWORDS["qa-credit"], source="production")
        for name in ("qa-credit-other", "qa-credit-20261022", "credit"):
            with self.assertRaises(AuthenticationError):
                staging.login(name, QA_PASSWORDS["qa-credit"], source=name)
        with self.assertRaisesRegex(ValueError, "Unsupported account"):
            staging.set_password("qa-credit", "NeverRotateThisFixture")

    def test_audit_failure_rolls_back_both_identity_and_password_rows(self):
        with patch.object(
            self.service, "_audit", side_effect=RuntimeError("synthetic audit failure")
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                self.service.provision(self.admin, body())
        self.assertEqual(len(self.rows(users)), 2)
        self.assertEqual(len(self.rows(accounts)), 2)
        self.assertEqual(self.rows(admin_audit), [])

    def test_partial_existing_identity_is_preserved_without_repair_or_other_creation(
        self,
    ):
        ident = str(uuid.uuid4())
        with self.repo.transaction() as connection:
            connection.execute(
                insert(users).values(
                    id=ident,
                    tenant=self.scope.tenant_id,
                    owner="qa-user",
                    role="user",
                    status="active",
                    display_name="qa-user",
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        before = self.rows(users)
        with self.assertRaisesRegex(
            StagingQAError, "staging_qa_existing_account_changed"
        ):
            self.service.provision(self.admin, body())
        self.assertEqual(self.rows(users), before)
        self.assertEqual(len(self.rows(accounts)), 2)
        self.assertEqual(self.rows(admin_audit), [])

    def test_password_sessions_are_staging_only_and_recheck_canonical_status(self):
        self.service.provision(self.admin, body())
        staging = Auth(
            self.repo.engine,
            tenant=self.scope.tenant_id,
            staging_qa=True,
            clock=lambda: self.now,
        )
        tokens = {}
        for name, role in (("qa-admin", "admin"), ("qa-user", "user")):
            tokens[name] = staging.login(name, QA_PASSWORDS[name], source=name)
            principal = staging.session(tokens[name])
            self.assertEqual(
                (principal.owner, principal.role, principal.auth_mode),
                (name, role, "password"),
            )
            self.assertIsNone(self.auth.session(tokens[name]))
            with self.assertRaises(AuthenticationError):
                self.auth.login(name, QA_PASSWORDS[name], source="production:" + name)
            with self.assertRaisesRegex(ValueError, "Unsupported account"):
                staging.set_password(name, "DoNotResetAnyQAPassword")
        with self.repo.transaction() as connection:
            connection.execute(
                update(users)
                .where(users.c.owner == "qa-user")
                .values(status="suspended")
            )
        self.assertIsNone(staging.session(tokens["qa-user"]))
        with self.assertRaisesRegex(
            StagingQAError, "staging_qa_existing_account_changed"
        ):
            self.service.provision(self.admin, body())
        self.assertTrue(staging.ready())
        self.assertIsNotNone(staging.session(tokens["qa-admin"]))

    def test_https_route_reuses_login_csrf_and_does_not_echo_credentials(self):
        app = create_app(self.settings, repository=self.repo)
        with TestClient(app, base_url=ORIGIN) as client:
            client.post(
                "/api/auth/login",
                json={"username": "superdan", "password": EXISTING_PASSWORD},
            ).raise_for_status()
            denied = client.post(
                "/v1/admin/staging/qa-accounts",
                json=body(),
                headers={"Origin": "https://elsewhere.invalid"},
            )
            self.assertEqual(denied.status_code, 403)
            accepted = client.post(
                "/v1/admin/staging/qa-accounts", json=body(), headers={"Origin": ORIGIN}
            )
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertNotIn(QA_PASSWORDS["qa-user"], accepted.text)
            client.post("/api/auth/logout")
            client.post(
                "/api/auth/login",
                json={"username": "qa-user", "password": QA_PASSWORDS["qa-user"]},
            ).raise_for_status()
            self.assertEqual(client.get("/v1/admin/users").status_code, 403)
            client.post("/api/auth/logout")
            client.post(
                "/api/auth/login",
                json={"username": "qa-admin", "password": QA_PASSWORDS["qa-admin"]},
            ).raise_for_status()
            self.assertEqual(client.get("/v1/admin/users").status_code, 200)
            malformed = client.post(
                "/v1/admin/staging/qa-accounts",
                content=json.dumps({**body(), "unexpected": QA_PASSWORDS["qa-user"]}),
            )
            self.assertEqual(malformed.status_code, 422)
            self.assertNotIn(QA_PASSWORDS["qa-user"], malformed.text)
        production = create_app(
            replace(
                self.settings,
                deployment_target="production",
                public_origin="https://inkseq.com",
            ),
            repository=self.repo,
        )
        self.assertFalse(production.state.auth.staging_qa)
        self.assertNotIn(
            "/v1/admin/staging/qa-accounts",
            {getattr(route, "path", None) for route in production.routes},
        )

    def test_credit_grant_requires_adoption_then_is_once_only_and_audited(self):
        with self.assertRaisesRegex(StagingQAError, "staging_qa_credits_not_ready"):
            self.service.provision(self.admin, body(grant=True))
        self.assertEqual(len(self.rows(users)), 2)
        from studio_platform.credit_schema import entries, metadata as credit_metadata

        with self.repo.transaction() as connection:
            credit_metadata.create_all(connection)
            connection.execute(update(self.marker).values(version_num="20261019_0004"))
        first = self.service.provision(self.admin, body(grant=True))
        self.assertTrue(first["credit_grant"]["created"])
        qa_admin = Principal(
            "qa-admin", "browser:qa-admin", role="admin", identity_source="canonical"
        )
        replay = self.service.provision(qa_admin, body(grant=True))
        self.assertFalse(replay["credit_grant"]["created"])
        self.assertEqual(
            first["credit_grant"]["entry_id"], replay["credit_grant"]["entry_id"]
        )
        rows = self.rows(entries)
        self.assertEqual(
            [(row["kind"], row["amount"], row["actor"]) for row in rows],
            [("grant", 2000, self.admin.actor_id)],
        )
        self.assertEqual(len(self.rows(admin_audit)), 3)
        from studio_platform.identity_admin import IdentityAdmin

        overview = IdentityAdmin(self.repo, self.settings).list_users(
            qa_admin, q='qa-user'
        )
        self.assertEqual(len(overview['items']), 1)
        self.assertEqual(overview['items'][0]['balance'], 2000)
        self.assertEqual(overview['items'][0]['used_30d'], 0)
        self.assertEqual(len(self.rows(entries)), 1)
        self.assertEqual(len(self.rows(admin_audit)), 3)


class LocalProvisionTests(unittest.TestCase):
    def test_finite_extension_is_memory_only_preserves_old_values_and_reuses_third(
        self,
    ):
        value = {"base_url": ORIGIN, "accounts": body()["accounts"]}
        before = json.dumps(value, sort_keys=True)
        token = Mock(return_value=QA_PASSWORDS["qa-credit"])
        with patch(
            "os.open", side_effect=AssertionError("No file access in preparation")
        ):
            extended = tool.credit_fixture_accounts(value, token=token)
            replay = tool.credit_fixture_accounts(
                extended, token=Mock(side_effect=AssertionError("No rotation"))
            )
        self.assertEqual(json.dumps(value, sort_keys=True), before)
        self.assertEqual(extended["accounts"][:2], value["accounts"])
        self.assertEqual(extended, replay)
        token.assert_called_once_with(36)
        self.assertEqual(tool._validate(value), value)
        self.assertEqual(tool._validate(extended), extended)
        for records in (
            extended["accounts"][1:],
            [extended["accounts"][0], extended["accounts"][2]],
            extended["accounts"] + [extended["accounts"][2]],
        ):
            with self.assertRaisesRegex(tool.ProvisionError, "qa_private_file_invalid"):
                tool._validate({"base_url": ORIGIN, "accounts": records})
        bad = json.loads(json.dumps(extended))
        bad["accounts"][2]["username"] = "qa-credit-20261022"
        with self.assertRaisesRegex(tool.ProvisionError, "qa_private_file_invalid"):
            tool._validate(bad)

    def test_three_name_transport_never_grants_or_falls_back_to_bootstrap(self):
        value = {"base_url": ORIGIN, "accounts": credit_body()["accounts"]}
        client = Mock()
        bootstrap = Mock(side_effect=AssertionError("No new authority"))
        with self.assertRaisesRegex(tool.ProvisionError, "qa_request_invalid"):
            tool.provision(
                value, grant_test_credits=True, client=client, read_bootstrap=bootstrap
            )
        client.post.assert_not_called()
        client.post.side_effect = [Mock(status_code=401), Mock(status_code=200)]
        with self.assertRaisesRegex(tool.ProvisionError, "qa_admin_login_failed"):
            tool.provision(value, client=client, read_bootstrap=bootstrap)
        bootstrap.assert_not_called()
        self.assertEqual(
            [call.args[0] for call in client.post.call_args_list],
            ["/api/auth/login", "/api/auth/logout"],
        )
        response = Mock(status_code=200)
        response.json.return_value = {"accounts": [], "credit_grant": None}
        client = Mock()
        client.post.side_effect = [
            Mock(status_code=200),
            response,
            Mock(status_code=200),
        ]
        result = tool.provision(value, client=client, read_bootstrap=bootstrap)
        self.assertEqual(result["accounts"], ["qa-admin", "qa-credit", "qa-user"])
        self.assertFalse(
            client.post.call_args_list[1].kwargs["json"]["grant_test_credits"]
        )
        self.assertNotIn("password", json.dumps(result))

    def test_settings_target_defaults_to_production_and_needs_exact_staging_pair(self):
        with patch.dict(os.environ, {"PUBLIC_ORIGIN": ORIGIN}, clear=True):
            settings = Settings.from_environment()
            self.assertEqual(settings.deployment_target, "production")
            self.assertFalse(enabled(settings))
            with patch.dict(os.environ, {"INKSEQ_DEPLOYMENT_TARGET": "staging"}):
                self.assertTrue(enabled(Settings.from_environment()))
            with patch.dict(os.environ, {"INKSEQ_DEPLOYMENT_TARGET": "other"}):
                with self.assertRaisesRegex(
                    ValueError, "Unsupported deployment target"
                ):
                    Settings.from_environment()

    def test_private_local_file_is_exclusive_and_retry_reuses_synthetic_credentials(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "accounts.json"
            token = Mock(
                side_effect=[QA_PASSWORDS["qa-admin"], QA_PASSWORDS["qa-user"]]
            )
            first = tool.local_accounts(path, token=token)
            replay = tool.local_accounts(
                path, token=Mock(side_effect=AssertionError("No new passwords"))
            )
            self.assertEqual(first, replay)
            self.assertEqual(token.call_count, 2)
            # A second hard link or incomplete/corrupt file cannot become a
            # silently overwritten credential source.
            link = Path(temporary) / "copy.json"
            os.link(path, link)
            with self.assertRaisesRegex(tool.ProvisionError, "qa_private_file_invalid"):
                tool.local_accounts(
                    path, token=Mock(side_effect=AssertionError("No new passwords"))
                )
            self.assertEqual(json.loads(path.read_bytes()), first)

    def test_tool_fixed_https_cookie_transport_suppresses_secrets_and_redirects(self):
        value = {"base_url": ORIGIN, "accounts": body()["accounts"]}
        response = Mock(status_code=200)
        response.json.return_value = {"accounts": [], "credit_grant": None}
        client = Mock()
        client.post.side_effect = [
            Mock(status_code=401),
            Mock(status_code=200),
            response,
            Mock(status_code=200),
        ]
        read_bootstrap = Mock(return_value="SyntheticBootstrapOnlyNeverStored")
        with patch("httpx.Client", return_value=client) as factory:
            result = tool.provision(
                value, grant_test_credits=True, read_bootstrap=read_bootstrap
            )
        self.assertEqual(factory.call_args.kwargs["base_url"], ORIGIN)
        self.assertTrue(factory.call_args.kwargs["verify"])
        self.assertFalse(factory.call_args.kwargs["follow_redirects"])
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertEqual(factory.call_args.kwargs["headers"]["Origin"], ORIGIN)
        self.assertEqual(read_bootstrap.call_count, 1)
        self.assertNotIn("password", json.dumps(result))
        self.assertNotIn("SyntheticBootstrap", json.dumps(result))
        self.assertTrue(
            client.post.call_args_list[2].kwargs["json"]["grant_test_credits"]
        )
        redirected = Mock()
        redirected.post.side_effect = [Mock(status_code=308), Mock(status_code=200)]
        with self.assertRaisesRegex(tool.ProvisionError, "qa_admin_login_failed"):
            tool.provision(
                value,
                client=redirected,
                read_bootstrap=Mock(side_effect=AssertionError("No redirect fallback")),
            )
        invalid = {**value, "base_url": "https://inkseq.com"}
        with patch("httpx.Client", side_effect=AssertionError("No production request")):
            with self.assertRaisesRegex(tool.ProvisionError, "qa_private_file_invalid"):
                tool.provision(invalid)

    def test_cli_refuses_secret_arguments_without_echoing_them(self):
        out = io.StringIO()
        with patch("sys.stderr", out), self.assertRaises(SystemExit):
            tool.main(["--password", QA_PASSWORDS["qa-admin"]])
        self.assertNotIn(QA_PASSWORDS["qa-admin"], out.getvalue())


if __name__ == "__main__":
    unittest.main()
