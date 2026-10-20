"""Disposable account authority bridge tests; no live identity or cloud calls."""

from pathlib import Path
import secrets
import tempfile
from types import SimpleNamespace
import unittest

from sqlalchemy import Column, MetaData, String, Table, inspect, insert, update

from studio_platform.auth import Auth, AuthenticationError, Principal
from studio_platform.identity import (
    capacity_owner_known,
    is_admin,
    lock_account_gate,
    operator_owners,
    public_identity,
    require_admin,
)
from studio_platform.identity_schema import users, admin_audit
from studio_platform.operator_capacity import OperatorError
from studio_platform.repository import Repository


class IdentityReaderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.repo = Repository(
            "sqlite:///" + (self.directory / "roles.sqlite").as_posix()
        )
        self.addCleanup(self.repo.close)
        self.settings = SimpleNamespace(
            tenant_id="sixnine",
            operator_capacity_owners=("superdan", "supervan"),
            operator_admin_owners=("superdan",),
        )
        self.auth = Auth(self.repo.engine, mode="local-test", clock=lambda: 1000.0)
        self.token = self.auth.login("superdan", "")
        self.original = self.auth.session(self.token)
        self.key = self.auth.create_key(
            "superdan",
            authenticated_session=self.original,
            name="reader-test",
            scopes=["projects:read", "jobs:read"],
            all_projects=True,
        )["api_key"]
        self.static = secrets.token_urlsafe(32)
        self.auth.register_client(
            "reader-test", self.static, "superdan", ["p"], ["jobs:read"]
        )

    def adopt(
        self, *, role="admin", status="active", owner="superdan", tenant="sixnine"
    ):
        users.create(self.repo.engine, checkfirst=True)
        admin_audit.create(self.repo.engine, checkfirst=True)
        with self.repo.engine.begin() as connection:
            connection.execute(
                insert(users).values(
                    id="00000000-0000-4000-8000-000000000001",
                    tenant=tenant,
                    owner=owner,
                    role=role,
                    status=status,
                    display_name=owner,
                    created_at=1000.0,
                    updated_at=1000.0,
                )
            )

    def change(self, **values):
        with self.repo.engine.begin() as connection:
            connection.execute(
                update(users).where(users.c.owner == "superdan").values(**values)
            )

    def test_readers_never_create_users_and_preserve_legacy_split(self):
        self.assertFalse(inspect(self.repo.engine).has_table(users.name))
        self.assertTrue(is_admin(self.original, self.repo, self.settings))
        van = Principal("supervan", "browser:supervan")
        self.assertTrue(is_admin(van, self.repo, self.settings, legacy_capacity=True))
        self.assertFalse(is_admin(van, self.repo, self.settings))
        with self.assertRaisesRegex(OperatorError, "operator_admin_required"):
            require_admin(van, self.repo, self.settings)
        self.assertFalse(inspect(self.repo.engine).has_table(users.name))

    def test_global_postgres_gate_precedes_schema_detection_and_adoption(self):
        # This connection deliberately supports no inspection or table reads.
        # A pre-adoption claim and later adoption/admin writer still lock the
        # same resource, regardless of the caller's tenant or table existence.
        calls = []
        connection = SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql"),
            execute=lambda statement, parameters: calls.append(
                (str(statement), parameters)
            ),
        )
        lock_account_gate(connection, "legacy-tenant")
        lock_account_gate(connection, "adopted-tenant", exclusive=True)
        self.assertEqual(
            [call[0] for call in calls],
            [
                "SELECT pg_advisory_xact_lock_shared(:key)",
                "SELECT pg_advisory_xact_lock(:key)",
            ],
        )
        self.assertEqual(calls[0][1], calls[1][1])

    def test_canonical_role_overrides_env_and_stale_principal(self):
        self.adopt(role="user")
        self.assertEqual(self.auth.session(self.token).role, "user")
        self.assertFalse(
            is_admin(self.original, self.repo, self.settings, legacy_capacity=True)
        )
        with self.assertRaisesRegex(OperatorError, "admin_required"):
            require_admin(self.original, self.repo, self.settings)
        self.change(role="admin")
        self.settings.operator_admin_owners = ()
        self.settings.operator_capacity_owners = ()
        self.assertEqual(
            require_admin(self.original, self.repo, self.settings), "superdan"
        )
        self.assertEqual(self.auth.session(self.token).role, "admin")

    def test_admin_keys_never_inherit_admin_role_or_lose_scopes(self):
        self.adopt()
        for token in (self.key, self.static):
            principal = self.auth.bearer(token)
            self.assertTrue(principal.machine)
            self.assertEqual(principal.role, "user")
            self.assertTrue(principal.allows("p", "jobs:read"))
            self.assertFalse(is_admin(principal, self.repo, self.settings))
            with self.assertRaisesRegex(OperatorError, "admin_required"):
                require_admin(principal, self.repo, self.settings)

    def test_suspension_revokes_browser_keys_and_original_workflow_grants_next_read(
        self,
    ):
        browser_grant = {
            name: getattr(self.original, name)
            for name in (
                "actor_id",
                "machine",
                "auth_mode",
                "password_version",
                "session_hash",
            )
        }
        machine = self.auth.bearer(self.key)
        key_grant = {name: getattr(machine, name) for name in browser_grant}
        self.adopt(status="suspended")
        self.assertIsNone(self.auth.session(self.token))
        self.assertIsNone(self.auth.bearer(self.key))
        self.assertIsNone(self.auth.bearer(self.static))
        with self.assertRaisesRegex(AuthenticationError, "账户已暂停"):
            self.auth.login("superdan", "", source="suspended")
        for grant in (browser_grant, key_grant):
            with self.assertRaisesRegex(
                AuthenticationError, "film_actor_grant_unavailable"
            ):
                self.auth.workflow_principal("superdan", grant)
        with self.assertRaisesRegex(OperatorError, "admin_required"):
            require_admin(self.original, self.repo, self.settings)

    def test_missing_owner_and_other_tenant_never_fall_back_to_env(self):
        self.adopt(tenant="other")
        self.assertIsNone(self.auth.session(self.token))
        self.assertIsNone(self.auth.bearer(self.key))
        self.assertIsNone(self.auth.bearer(self.static))
        self.assertFalse(capacity_owner_known(self.repo, self.settings, "superdan"))
        self.assertFalse(is_admin(self.original, self.repo, self.settings))
        with self.assertRaisesRegex(OperatorError, "admin_required"):
            require_admin(self.original, self.repo, self.settings)

    def test_partial_schema_never_enables_legacy_allowlists(self):
        admin_audit.create(self.repo.engine)
        self.assertIsNone(self.auth.session(self.token))
        self.assertEqual(
            public_identity(self.original, self.repo, self.settings),
            {
                "role": "user",
                "status": "suspended",
                "user_id": None,
                "display_name": None,
                "email": None,
            },
        )
        self.assertFalse(is_admin(self.original, self.repo, self.settings))
        with self.assertRaisesRegex(OperatorError, "admin_required"):
            require_admin(self.original, self.repo, self.settings)

    def test_adopted_or_unknown_marker_without_tables_never_restores_env_authority(
        self,
    ):
        version = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), primary_key=True),
        )
        version.create(self.repo.engine)
        for marker in ("20261019_0003", "unknown"):
            with self.subTest(marker=marker):
                with self.repo.engine.begin() as connection:
                    connection.execute(version.delete())
                    connection.execute(insert(version).values(version_num=marker))
                self.assertIsNone(self.auth.session(self.token))
                self.assertIsNone(self.auth.bearer(self.key))
                self.assertFalse(is_admin(self.original, self.repo, self.settings))
                with self.assertRaisesRegex(OperatorError, "admin_required"):
                    require_admin(self.original, self.repo, self.settings)

    def test_identity_reads_do_not_rewrite_business_owner_or_money(self):
        self.adopt()
        current = self.auth.session(self.token)
        self.assertEqual(current.owner, "superdan")
        self.assertEqual(current.user_id, "00000000-0000-4000-8000-000000000001")
        self.assertEqual(operator_owners(self.repo, self.settings), {"superdan"})
        self.assertEqual(
            public_identity(current, self.repo, self.settings),
            {
                "role": "admin",
                "status": "active",
                "user_id": current.user_id,
                "display_name": "superdan",
                "email": None,
            },
        )

    def test_auth_me_profile_is_current_human_owned_and_private_from_machines(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import COOKIE, create_app
        from studio_platform.settings import Settings

        app = create_app(
            Settings(self.directory / "profile-app", auth_mode="local-test"),
            repository=self.repo,
        )
        with TestClient(app) as client:
            self.assertEqual(
                client.post(
                    "/api/auth/login", json={"username": "superdan"}
                ).status_code,
                200,
            )
            legacy = client.get("/api/auth/me").json()
            self.assertEqual(
                (legacy["username"], legacy["display_name"], legacy["email"]),
                ("superdan", "superdan", None),
            )
            self.assertFalse(inspect(self.repo.engine).has_table(users.name))

            self.adopt(role="user")
            self.change(display_name="Current creator", email="current@example.test")
            with self.repo.transaction() as connection:
                for ident, tenant, owner in (
                    ("00000000-0000-4000-8000-000000000002", "other", "superdan"),
                    ("00000000-0000-4000-8000-000000000003", "sixnine", "another"),
                ):
                    connection.execute(
                        insert(users).values(
                            id=ident,
                            tenant=tenant,
                            owner=owner,
                            role="admin",
                            status="active",
                            display_name="Foreign creator",
                            email="foreign@example.test",
                            created_at=1000.0,
                            updated_at=1000.0,
                        )
                    )
            response = client.get(
                "/api/auth/me", params={"tenant": "other", "owner": "another"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            current = response.json()
            self.assertEqual(
                (current["username"], current["display_name"], current["email"]),
                ("superdan", "Current creator", "current@example.test"),
            )
            self.assertEqual(current["role"], "user")
            self.assertFalse(any(current["operator_capacity"].values()))
            self.change(display_name="Updated creator", email="updated@example.test")
            refreshed = client.get("/api/auth/me").json()
            self.assertEqual(
                (refreshed["display_name"], refreshed["email"]),
                ("Updated creator", "updated@example.test"),
            )

            key = app.state.auth.create_key(
                "superdan",
                authenticated_session=app.state.auth.session(
                    client.cookies.get(COOKIE)
                ),
                name="profile-agent",
                scopes=["jobs:read"],
                all_projects=True,
            )["api_key"]
            machine = client.get(
                "/api/auth/me", headers={"Authorization": "Bearer " + key}
            ).json()
            self.assertTrue(machine["machine"])
            self.assertEqual(machine["username"], "superdan")
            self.assertEqual(machine["scopes"], ["jobs:read"])
            self.assertIsNone(machine["display_name"])
            self.assertIsNone(machine["email"])
            self.assertFalse(any(machine["operator_capacity"].values()))

            self.change(status="suspended")
            self.assertEqual(client.get("/api/auth/me").status_code, 401)
            self.change(status="active")
            self.assertEqual(
                client.post(
                    "/api/auth/login", json={"username": "superdan"}
                ).status_code,
                200,
            )
            with self.repo.transaction() as connection:
                connection.execute(
                    users.delete().where(
                        users.c.tenant == "sixnine", users.c.owner == "superdan"
                    )
                )
            # The identically named owner in the other tenant is no fallback.
            self.assertEqual(client.get("/api/auth/me").status_code, 401)

    def test_operator_service_guards_use_canonical_role_without_env_grant(self):
        from studio_platform.dstack_operator import DstackOperator
        from studio_platform.host_scorecard import OperatorHosts
        from studio_platform.operator_budgets import OperatorBudgets
        from studio_platform.operator_capacity import OperatorCapacity
        from studio_platform.operator_jobs import OperatorJobs

        self.adopt(role="user")
        dstack = object.__new__(DstackOperator)
        dstack.repo, dstack.settings = self.repo, self.settings
        hosts = object.__new__(OperatorHosts)
        hosts.repo, hosts.settings = self.repo, self.settings
        capacity = object.__new__(OperatorCapacity)
        capacity.repo, capacity.settings = self.repo, self.settings
        checks = (
            dstack.authorize,
            dstack.authorize_admin,
            hosts._authorize,
            capacity.authorize,
            OperatorBudgets(self.repo, self.settings)._admin,
            OperatorJobs(self.repo, self.settings)._admin,
        )
        for check in checks:
            with self.subTest(check=check.__qualname__):
                with self.assertRaisesRegex(OperatorError, "admin_required"):
                    check(self.original)
        self.change(role="admin")
        self.settings.operator_admin_owners = ()
        self.settings.operator_capacity_owners = ()
        for check in checks:
            with self.subTest(check=check.__qualname__):
                check(self.original)

    def test_http_identity_and_operator_guard_precede_handler_validation(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import COOKIE, create_app
        from studio_platform.settings import Settings

        self.adopt(role="user")
        settings = Settings(
            self.directory / "app",
            auth_mode="local-test",
            operator_capacity_owners=("superdan",),
            operator_admin_owners=("superdan",),
        )
        app = create_app(settings, repository=self.repo)
        with TestClient(app) as client:
            self.assertEqual(
                client.post(
                    "/api/auth/login", json={"username": "superdan"}
                ).status_code,
                200,
            )
            who = client.get("/api/auth/me").json()
            self.assertEqual(who["role"], "user")
            self.assertEqual(who["status"], "active")
            self.assertEqual(
                who["credits"], {"available": False, "balance": None, "held": None}
            )
            self.assertFalse(any(who["operator_capacity"].values()))
            self.assertEqual(client.get("/v1/projects").status_code, 200)
            denied = client.post("/v1/operator/dstack/starts", json={"invalid": True})
            self.assertEqual(
                (denied.status_code, denied.json()["code"]), (403, "admin_required")
            )
            self.change(role="admin")
            who = client.get("/api/auth/me").json()
            self.assertTrue(all(who["operator_capacity"].values()))
            key = app.state.auth.create_key(
                "superdan",
                authenticated_session=app.state.auth.session(
                    client.cookies.get(COOKIE)
                ),
                name="admin-agent",
                scopes=["jobs:read"],
                all_projects=True,
            )["api_key"]
            denied = client.get(
                "/v1/operator/dstack/state", headers={"Authorization": "Bearer " + key}
            )
            self.assertEqual(
                (denied.status_code, denied.json()["code"]), (403, "admin_required")
            )

    def test_suspended_owner_cannot_mint_key_from_earlier_principal(self):
        self.adopt(status="suspended")
        with self.assertRaisesRegex(AuthenticationError, "账户状态已改变"):
            self.auth.create_key(
                "superdan",
                authenticated_session=self.original,
                name="stale",
                scopes=["jobs:read"],
                all_projects=True,
            )

    def test_public_agent_code_exchange_rechecks_owner_status(self):
        from studio_platform.agent_connect import (
            AgentConnect,
            ConnectError,
            PROFILE_ID,
            PROFILE_VERSION,
        )
        from studio_platform.auth import digest

        service = AgentConnect(self.auth)
        origin = "http://127.0.0.1:8850"
        grant = service.issue(
            self.original,
            name="reader-agent",
            authorization_profile_id=PROFILE_ID,
            authorization_profile_version=PROFILE_VERSION,
            idempotency_key="reader-pending",
            origin=origin,
        )
        self.adopt(status="suspended")
        token = "sxp_" + secrets.token_urlsafe(32)
        with self.assertRaisesRegex(ConnectError, "connection_authority_changed"):
            service.exchange(
                code=grant["code"],
                client_challenge=digest(secrets.token_urlsafe(32)),
                token_hash=digest(token),
                key_prefix=token[:12],
                source="reader-test",
                expected_authorization_fingerprint=grant["connection"][
                    "authorization_fingerprint"
                ],
                origin=origin,
            )
        self.assertEqual(len(self.auth.list_keys("superdan")), 1)


if __name__ == "__main__":
    unittest.main()
