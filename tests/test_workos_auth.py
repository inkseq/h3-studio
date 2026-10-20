"""Offline AuthKit/PKCE, account isolation and browser callback acceptance."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import (
    Column,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    func,
    insert,
    select,
    update,
)
from workos.pkce import PKCE

from studio_platform.auth import Auth, AuthenticationError, Principal, sessions
from studio_platform.identity_schema import metadata, users
from studio_platform.repository import documents
from studio_platform.workos_auth import (
    CONTROL_OWNER,
    CONTROL_PROJECT,
    STATE_CLEANUP_LIMIT,
    STATE_KIND,
    WorkOSAuth,
    WorkOSError,
    _read_cookie,
    return_path,
)
from test_platform_repository import LedgerCase


class FakeProvider:
    def __init__(self):
        self.user = SimpleNamespace(
            id="user_" + "A" * 24,
            email="person@example.test",
            email_verified=True,
            first_name="New",
            last_name="Creator",
        )
        self.auth_calls, self.widget_calls = [], []
        self.failure = None

    def client(self, _config):
        return SimpleNamespace(
            pkce=PKCE(), user_management=self, widgets=self, close=lambda: None
        )

    def get_authorization_url(
        self, *, provider, redirect_uri, state, code_challenge, code_challenge_method
    ):
        return "https://api.workos.com/user_management/authorize?" + urlencode(
            locals() | {"self": "offline"}
        )

    def authenticate_with_code(self, *, code, code_verifier):
        self.auth_calls.append((code, code_verifier))
        if self.failure:
            raise RuntimeError(self.failure)
        return SimpleNamespace(
            user=self.user,
            access_token="DO-NOT-SAVE-ACCESS",
            refresh_token="DO-NOT-SAVE-REFRESH",
        )

    def create_token(self, *, user_id, scopes):
        self.widget_calls.append({"user_id": user_id, "scopes": scopes})
        return SimpleNamespace(token="DO-NOT-SAVE-WIDGET")


class WorkOSAuthTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.auth = Auth(
            self.repo.engine,
            mode="local-test",
            tenant=self.scope.tenant_id,
            clock=lambda: self.now,
        )
        self.version = Table(
            "alembic_version",
            MetaData(),
            Column("version_num", String(32), nullable=False),
            PrimaryKeyConstraint("version_num", name="alembic_version_pkc"),
        )
        with self.repo.transaction() as connection:
            metadata.create_all(connection)
            self.version.create(connection)
            connection.execute(insert(self.version).values(version_num="20261019_0003"))
            for owner in ("superdan", "supervan"):
                connection.execute(
                    insert(users).values(
                        id=str(uuid.uuid4()),
                        tenant=self.scope.tenant_id,
                        owner=owner,
                        role="admin",
                        status="active",
                        display_name=owner,
                        email=None,
                        email_verified=0,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
        self.config_file = Path(self.temp.name) / "workos.json"
        self.config_file.write_text(
            json.dumps(
                {"client_id": "client_" + "A" * 24, "api_key": "sk_test_" + "B" * 30}
            ),
            encoding="utf-8",
        )
        self.config_file.chmod(0o600)
        self.settings = SimpleNamespace(
            tenant_id=self.scope.tenant_id, workos_config_file=self.config_file
        )
        self.provider = FakeProvider()
        self.service = WorkOSAuth(
            self.repo, self.auth, self.settings, sdk_factory=self.provider.client
        )
        self.origin = "https://staging.inkseq.com"
        self.session = self.auth.login("superdan", "")
        self.legacy = self.auth.session(self.session)

    def start(self, **extra):
        value = self.service.start(
            origin=self.origin, return_to="/story?project=original", **extra
        )
        state = parse_qs(urlsplit(value["url"]).query)["state"][0]
        return value, state

    def finish(self, value, state, **extra):
        return self.service.callback(
            code="ONE-TIME-CODE",
            state=state,
            cookie=value["cookie"],
            origin=self.origin,
            **extra,
        )

    def count_users(self):
        with self.repo.engine.connect() as connection:
            return connection.execute(
                select(func.count()).select_from(users)
            ).scalar_one()

    def row(self, owner):
        with self.repo.engine.connect() as connection:
            return dict(
                connection.execute(select(users).where(users.c.owner == owner))
                .mappings()
                .one()
            )

    def test_config_requires_protected_credentials_and_adopted_schema(self):
        self.assertTrue(self.service.enabled())
        with self.repo.transaction() as connection:
            connection.execute(update(self.version).values(version_num="20261019_0002"))
        self.assertFalse(self.service.enabled())
        self.settings.workos_config_file = None
        self.assertFalse(self.service.enabled())
        with self.assertRaisesRegex(WorkOSError, "workos_not_configured"):
            self.start()

    def test_return_paths_reject_external_encoded_and_control_ambiguity(self):
        for value in (
            "https://evil.test",
            "//evil.test",
            "/\\evil",
            "/%2f%2fevil",
            "/%252fevil",
            "/%0d%0aHost:evil",
            " /story",
            "/story\n",
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(WorkOSError, "return_to_invalid"),
            ):
                return_path(value)
        self.assertEqual(
            return_path("/story?project=one&shot=two#tail"),
            "/story?project=one&shot=two#tail",
        )

    def test_pkce_state_cookie_is_bound_one_use_and_secrets_never_persist(self):
        value, state = self.start()
        material = _read_cookie(value["cookie"])
        query = parse_qs(urlsplit(value["url"]).query)
        self.assertEqual(
            query["code_challenge"],
            [PKCE().generate_code_challenge(material["verifier"])],
        )
        result = self.finish(value, state)
        principal = self.auth.session(result["token"])
        self.assertTrue(principal.owner.startswith("u_"))
        self.assertEqual(principal.role, "user")
        self.assertEqual(principal.auth_mode, "workos")
        self.assertEqual(result["return_to"], "/story?project=original")
        with self.assertRaisesRegex(WorkOSError, "workos_state_invalid"):
            self.finish(value, state)
        self.assertEqual(len(self.provider.auth_calls), 1)
        with self.repo.engine.connect() as connection:
            payloads = json.dumps(
                list(connection.execute(select(documents.c.payload)).scalars())
            )
        for secret in (
            material["verifier"],
            material["binding"],
            state,
            "ONE-TIME-CODE",
            "DO-NOT-SAVE-ACCESS",
            "DO-NOT-SAVE-REFRESH",
        ):
            self.assertNotIn(secret, payloads)

    def test_cookie_mismatch_expiration_and_parallel_replay_do_not_exchange_twice(self):
        value, state = self.start()
        another, _ = self.start()
        with self.assertRaisesRegex(WorkOSError, "workos_state_invalid"):
            self.finish(another, state)
        self.assertEqual(self.provider.auth_calls, [])

        def complete(_):
            try:
                return self.finish(value, state)["token"]
            except WorkOSError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(complete, range(2)))
        self.assertEqual(results.count("workos_state_invalid"), 1)
        self.assertEqual(len(self.provider.auth_calls), 1)
        expired, state = self.start()
        self.now += 601
        with self.assertRaisesRegex(WorkOSError, "workos_state_invalid"):
            self.finish(expired, state)

    def test_provider_failure_is_static_and_consumes_the_original_exchange(self):
        value, state = self.start()
        self.provider.failure = "PRIVATE API KEY AND TOKEN RESPONSE"
        with self.assertRaisesRegex(
            WorkOSError, "workos_provider_unavailable"
        ) as failure:
            self.finish(value, state)
        self.assertNotIn("PRIVATE", str(failure.exception))
        with self.assertRaisesRegex(WorkOSError, "workos_state_invalid"):
            self.finish(value, state)
        self.assertEqual(self.count_users(), 2)

    def test_start_cleanup_is_bounded_and_only_removes_expired_auth_documents(self):
        def record(ident, *, payload=None, updated=None, **scope):
            return {
                "tenant_id": self.scope.tenant_id,
                "owner_id": CONTROL_OWNER,
                "project_id": CONTROL_PROJECT,
                "kind": STATE_KIND,
                "document_id": ident,
                "version": 1,
                "payload": {"expires_at": self.now - 1000}
                if payload is None
                else payload,
                "updated_at": self.now - 1000 if updated is None else updated,
                **scope,
            }

        preserved = [
            record("recent", updated=self.now),
            record(
                "active",
                payload={"expires_at": self.now + 1000},
                updated=self.now - 700,
            ),
            record("business", owner_id="superdan", project_id="real-story"),
            record("different-kind", kind="story"),
            record("foreign-tenant", tenant_id="another"),
        ]
        with self.repo.transaction() as connection:
            connection.execute(
                insert(documents),
                [record(f"expired-{n:03}") for n in range(STATE_CLEANUP_LIMIT + 5)]
                + preserved,
            )
        self.start()
        with self.repo.engine.connect() as connection:
            remaining = set(
                connection.execute(select(documents.c.document_id)).scalars()
            )
        self.assertEqual(sum(ident.startswith("expired-") for ident in remaining), 5)
        self.assertTrue({row["document_id"] for row in preserved} <= remaining)

    def test_public_start_reuses_login_rate_limit_without_creating_unbounded_state(
        self,
    ):
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings

        app = create_app(
            Settings(
                Path(self.temp.name) / "api-rate",
                tenant_id=self.scope.tenant_id,
                auth_mode="local-test",
                database_url=self.url,
                workos_config_file=self.config_file,
            ),
            repository=self.repo,
        )
        app.state.workos_auth.sdk_factory = self.provider.client
        app.state.auth.clock = lambda: self.now
        with TestClient(app, base_url="https://testserver") as client:
            for _ in range(5):
                self.assertEqual(
                    client.get(
                        "/api/auth/workos/start", follow_redirects=False
                    ).status_code,
                    302,
                )
            limited = client.get("/api/auth/workos/start", follow_redirects=False)
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.headers["retry-after"], "300")
            self.assertEqual(limited.headers["cache-control"], "no-store")
            with self.repo.engine.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        select(func.count())
                        .select_from(documents)
                        .where(documents.c.kind == STATE_KIND)
                    ).scalar_one(),
                    5,
                )
            self.now += 301
            self.assertEqual(
                client.get(
                    "/api/auth/workos/start", follow_redirects=False
                ).status_code,
                302,
            )

    def test_callback_issuance_refusal_is_finite_and_original_exchange_stays_consumed(
        self,
    ):
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings

        app = create_app(
            Settings(
                Path(self.temp.name) / "api-race",
                tenant_id=self.scope.tenant_id,
                auth_mode="local-test",
                database_url=self.url,
                workos_config_file=self.config_file,
            ),
            repository=self.repo,
        )
        app.state.workos_auth.sdk_factory = self.provider.client
        with TestClient(app, base_url="https://testserver") as client:
            start = client.get("/api/auth/workos/start", follow_redirects=False)
            state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
            cookie = client.cookies.get("__Host-inkseq-workos")
            with patch.object(
                app.state.auth,
                "issue_workos_session",
                side_effect=AuthenticationError("PRIVATE-SUSPENSION-DETAIL"),
            ):
                refused = client.get(
                    "/api/auth/workos/callback",
                    params={"state": state, "code": "RACE-CODE"},
                    follow_redirects=False,
                )
            self.assertEqual(refused.status_code, 403)
            self.assertEqual(refused.json()["code"], "account_unavailable")
            self.assertNotIn("PRIVATE", refused.text)
            self.assertIn("Max-Age=0", refused.headers["set-cookie"])
            # Even replay with the original cookie cannot exchange/create again.
            replay = client.get(
                "/api/auth/workos/callback",
                params={"state": state, "code": "RACE-CODE"},
                headers={"cookie": "__Host-inkseq-workos=" + cookie},
                follow_redirects=False,
            )
            self.assertEqual(replay.status_code, 400)
            self.assertEqual(replay.json()["code"], "workos_state_invalid")
        self.assertEqual(self.count_users(), 3)
        self.assertEqual(len(self.provider.auth_calls), 1)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(func.count()).select_from(sessions)
                ).scalar_one(),
                1,
            )

    def test_repeat_signin_keeps_immutable_owner_role_and_local_profile(self):
        value, state = self.start()
        owner = self.auth.session(self.finish(value, state)["token"]).owner
        with self.repo.transaction() as connection:
            connection.execute(
                update(users)
                .where(users.c.owner == owner)
                .values(role="admin", display_name="My local name")
            )
        self.provider.user.first_name = "Upstream overwrite"
        value, state = self.start()
        current = self.auth.session(self.finish(value, state)["token"])
        self.assertEqual((current.owner, current.role), (owner, "admin"))
        self.assertEqual(self.row(owner)["display_name"], "My local name")
        self.assertEqual(self.count_users(), 3)

    def test_verified_email_cannot_take_over_a_legacy_account(self):
        with self.repo.transaction() as connection:
            connection.execute(
                update(users)
                .where(users.c.owner == "superdan")
                .values(email=self.provider.user.email)
            )
        value, state = self.start()
        with self.assertRaisesRegex(WorkOSError, "workos_link_required"):
            self.finish(value, state)
        self.assertIsNone(self.row("superdan")["workos_user_id"])
        self.assertEqual(self.count_users(), 2)

    def test_explicit_legacy_link_preserves_owner_admin_and_refuses_subject_rebind(
        self,
    ):
        value, state = self.start(link=True, principal=self.legacy)
        current = self.auth.session(
            self.finish(value, state, principal=self.legacy)["token"]
        )
        self.assertEqual((current.owner, current.role), ("superdan", "admin"))
        self.assertEqual(self.row("superdan")["display_name"], "superdan")
        original_subject = self.row("superdan")["workos_user_id"]
        self.provider.user.id = "user_" + "Z" * 24
        value, state = self.start(link=True, principal=self.legacy)
        with self.assertRaisesRegex(WorkOSError, "workos_identity_conflict"):
            self.finish(value, state, principal=self.legacy)
        self.assertEqual(self.row("superdan")["workos_user_id"], original_subject)

    def test_link_callback_rechecks_original_session_and_current_browser(self):
        value, state = self.start(link=True, principal=self.legacy)
        van = self.auth.session(self.auth.login("supervan", ""))
        with self.assertRaisesRegex(WorkOSError, "workos_link_session_changed"):
            self.finish(value, state, principal=van)
        self.auth.logout(self.session)
        with self.assertRaisesRegex(WorkOSError, "workos_link_session_changed"):
            self.finish(value, state, principal=self.legacy)
        self.assertEqual(self.provider.auth_calls, [])

    def test_unverified_email_creates_no_local_account(self):
        self.provider.user.email_verified = False
        value, state = self.start()
        with self.assertRaisesRegex(WorkOSError, "workos_verification_required"):
            self.finish(value, state)
        self.assertEqual(self.count_users(), 2)

    def test_account_profile_and_widget_are_bound_to_human_owner_without_admin_scopes(
        self,
    ):
        value, state = self.start()
        principal = self.auth.session(self.finish(value, state)["token"])
        profile = self.service.update_account(
            principal, {"display_name": "Editable name"}
        )
        self.assertEqual(profile["display_name"], "Editable name")
        self.assertEqual(
            self.service.widget_token(principal), {"token": "DO-NOT-SAVE-WIDGET"}
        )
        self.assertEqual(
            self.provider.widget_calls,
            [{"user_id": self.provider.user.id, "scopes": []}],
        )
        with self.assertRaisesRegex(WorkOSError, "profile_invalid"):
            self.service.update_account(principal, {"role": "admin"})
        machine = Principal(principal.owner, "key:creator", machine=True)
        with self.assertRaisesRegex(WorkOSError, "browser_required"):
            self.service.widget_token(machine)
        self.assertEqual(len(self.provider.widget_calls), 1)

    def test_actual_pinned_sdk_parameters_are_used_without_environment_fallback(self):
        from workos import WorkOSClient
        from studio_platform.workos_auth import _client

        actual = _client(self.service.config())
        self.addCleanup(actual.close)
        self.service.sdk_factory = lambda _config: actual
        value, state = self.start()
        with patch.object(
            WorkOSClient,
            "request",
            return_value=SimpleNamespace(user=self.provider.user),
        ) as request:
            self.finish(value, state)
        body = request.call_args.kwargs["body"]
        self.assertEqual(
            body["code_verifier"], _read_cookie(value["cookie"])["verifier"]
        )
        self.assertEqual(
            request.call_args.kwargs["path"], ("user_management", "authenticate")
        )

    def test_https_browser_callback_is_public_cookie_bound_and_profile_is_usable(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings

        app = create_app(
            Settings(
                Path(self.temp.name) / "api",
                tenant_id=self.scope.tenant_id,
                auth_mode="local-test",
                database_url=self.url,
                workos_config_file=self.config_file,
            ),
            repository=self.repo,
        )
        app.state.workos_auth.sdk_factory = self.provider.client
        with TestClient(app, base_url="https://testserver") as client:
            self.assertTrue(
                client.get("/api/auth/config").json()["hosted_sign_in"]["enabled"]
            )
            start = client.get(
                "/api/auth/workos/start?return_to=/story?project=web",
                follow_redirects=False,
            )
            self.assertEqual(start.status_code, 302)
            self.assertIn("__Host-inkseq-workos", start.headers["set-cookie"])
            self.assertIn("HttpOnly", start.headers["set-cookie"])
            self.assertIn("Secure", start.headers["set-cookie"])
            state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
            result = client.get(
                "/api/auth/workos/callback",
                params={"state": state, "code": "WEB-CODE"},
                follow_redirects=False,
            )
            self.assertEqual(
                (result.status_code, result.headers["location"]),
                (302, "/story?project=web"),
            )
            self.assertEqual(result.headers["cache-control"], "no-store")
            who = client.get("/api/auth/me").json()
            self.assertEqual(who["authentication"], "workos")
            self.assertEqual(who["role"], "user")
            self.assertTrue(who["username"].startswith("u_"))
            self.assertEqual(who["display_name"], "New Creator")
            self.assertEqual(who["email"], "person@example.test")
            self.assertEqual(
                client.patch(
                    "/v1/account", json={"display_name": "Web creator"}
                ).status_code,
                200,
            )
            self.assertEqual(
                client.get("/v1/account").json()["display_name"], "Web creator"
            )
            refreshed = client.get("/api/auth/me").json()
            self.assertEqual(refreshed["display_name"], "Web creator")
            self.assertEqual(refreshed["email"], who["email"])
            self.assertEqual(refreshed["username"], who["username"])
            self.assertEqual(
                client.post("/v1/account/authkit-widget-token").json(),
                {"token": "DO-NOT-SAVE-WIDGET"},
            )


if __name__ == "__main__":
    unittest.main()
