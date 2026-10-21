"""Offline AuthKit/PKCE, account isolation and browser callback acceptance."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
import unittest
from unittest.mock import patch
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import (
    Column,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    event,
    func,
    insert,
    select,
    update,
)
from workos.pkce import PKCE

from studio_platform.auth import (
    Auth,
    AuthenticationError,
    Principal,
    WORKOS_SESSION_KIND,
    digest,
    sessions,
)
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
    _jwks,
    return_path,
)
from test_platform_repository import LedgerCase

_DEFAULT_TOKEN = object()


class FakeProvider:
    def __init__(self, clock):
        self.clock = clock
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.signing_key = self.key
        self.access_token = _DEFAULT_TOKEN
        self.claims = {}
        self.headers = {"kid": "offline-key"}
        self.user = SimpleNamespace(
            id="user_" + "A" * 24,
            email="person@example.test",
            email_verified=True,
            first_name="New",
            last_name="Creator",
        )
        self.auth_calls, self.widget_calls = [], []
        self.authorization_calls, self.logout_calls = [], []
        self.logout_failure = None
        self.logout_override = None
        self.failure = None

    def client(self, _config):
        return SimpleNamespace(
            pkce=PKCE(), user_management=self, widgets=self, close=lambda: None
        )

    def get_authorization_url(
        self,
        *,
        provider,
        redirect_uri,
        state,
        code_challenge,
        code_challenge_method,
        screen_hint=None,
    ):
        values = {
            key: value
            for key, value in locals().items()
            if key not in {"self", "screen_hint"}
        }
        if screen_hint is not None:
            values["screen_hint"] = screen_hint
        self.authorization_calls.append(values)
        return "https://api.workos.com/user_management/authorize?" + urlencode(
            values | {"self": "offline"}
        )

    def authenticate_with_code(self, *, code, code_verifier):
        self.auth_calls.append((code, code_verifier))
        if self.failure:
            raise RuntimeError(self.failure)
        return self.authentication()

    def authentication(self):
        token = self.access_token
        if token is _DEFAULT_TOKEN:
            token = jwt.encode(
                {
                    "iss": "https://api.workos.com",
                    "sub": self.user.id,
                    "client_id": "client_" + "A" * 24,
                    "sid": "session_" + str(len(self.auth_calls)).zfill(26),
                    "iat": int(self.clock()),
                    "exp": int(self.clock()) + 900,
                    **self.claims,
                },
                self.signing_key,
                algorithm="RS256",
                headers=self.headers,
            )
        return SimpleNamespace(
            user=self.user,
            access_token=token,
            refresh_token="DO-NOT-SAVE-REFRESH",
        )

    def get_logout_url(self, *, session_id, return_to):
        self.logout_calls.append({"session_id": session_id, "return_to": return_to})
        if self.logout_failure:
            raise RuntimeError(self.logout_failure)
        return (
            self.logout_override
            or "https://api.workos.com/user_management/sessions/logout?"
            + urlencode({"session_id": session_id, "return_to": return_to})
        )

    def list_sessions(self, subject, *, limit, after, order):
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    id="session_" + str(i).zfill(26),
                    user_id=subject,
                    status="active",
                    ended_at=None,
                    created_at=datetime.fromtimestamp(self.clock(), timezone.utc),
                    expires_at=datetime.fromtimestamp(self.clock() + 900, timezone.utc),
                )
                for i in range(1, len(self.auth_calls) + 1)
            ],
            list_metadata=SimpleNamespace(after=None),
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
            tenant_id=self.scope.tenant_id,
            workos_config_file=self.config_file,
            public_origin="https://staging.inkseq.com",
        )
        self.provider = FakeProvider(lambda: self.now)
        self.jwks_patch = patch(
            "studio_platform.workos_auth._jwks",
            return_value=SimpleNamespace(
                get_signing_key_from_jwt=lambda _token: SimpleNamespace(
                    key=self.provider.key.public_key()
                )
            ),
        )
        self.mock_jwks = self.jwks_patch.start()
        self.addCleanup(self.jwks_patch.stop)
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
        access_token = self.provider.authentication().access_token
        for secret in (
            material["verifier"],
            material["binding"],
            state,
            "ONE-TIME-CODE",
            "DO-NOT-SAVE-ACCESS",
            access_token,
            result["token"],
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
        app.state.auth.clock = lambda: self.now
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

    def test_revoke_cutoff_refuses_original_inflight_callback_and_allows_fresh_start(
        self,
    ):
        from studio_platform.account_sessions import AccountSessions

        original = self.hosted()
        value, state = self.start()
        revoker = AccountSessions(self.repo, self.auth, self.settings, self.service)
        operation = revoker.revoke_all(self.auth.session(original), {}, "old-flow")
        with self.assertRaisesRegex(WorkOSError, "account_unavailable"):
            self.finish(value, state)
        self.assertIsNone(self.auth.session(original))
        from studio_platform.session_authority import read

        with self.repo.engine.connect() as connection:
            saved = read(
                connection,
                self.scope.tenant_id,
                "revocation",
                operation["operation_id"],
            )
        self.assertIn("session_" + str(2).zfill(26), saved["provider_sessions"])
        self.assertIsNotNone(self.auth.session(self.hosted()))

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
            return_value=self.provider.authentication(),
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
        app.state.auth.clock = lambda: self.now
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

    def hosted(self):
        value, state = self.start()
        return self.finish(value, state)["token"]

    def sidecars(self):
        with self.repo.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(documents).where(documents.c.kind == WORKOS_SESSION_KIND)
                ).mappings()
            ]

    def browser(self, *, configured=True):
        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings

        app = create_app(
            Settings(
                Path(self.temp.name) / ("logout-api-" + uuid.uuid4().hex),
                tenant_id=self.scope.tenant_id,
                database_url=self.url,
                public_origin=self.origin,
                deployment_target="staging",
                workos_config_file=self.config_file if configured else None,
            ),
            repository=self.repo,
        )
        app.state.auth.clock = lambda: self.now
        app.state.workos_auth.sdk_factory = self.provider.client
        return TestClient(app, base_url=self.origin)

    def test_screen_hints_are_strict_additions_and_refused_before_sdk_even_when_off(
        self,
    ):
        self.start()
        self.assertNotIn("screen_hint", self.provider.authorization_calls[-1])
        for hint in ("sign-up", "sign-in"):
            value, _ = self.start(screen_hint=hint)
            self.assertEqual(
                parse_qs(urlsplit(value["url"]).query)["screen_hint"], [hint]
            )
        for hint in ("", "signup", "SIGN-UP", "sign-in&prompt=none", [], True):
            with (
                self.subTest(hint=hint),
                self.assertRaisesRegex(WorkOSError, "workos_state_invalid"),
            ):
                self.service.start(origin=self.origin, screen_hint=hint)
        before = len(self.provider.authorization_calls)
        with self.browser(configured=False) as client:
            for query in (
                "screen_hint=",
                "screen_hint=other",
                "screen_hint=sign-up&screen_hint=sign-in",
                "screen_hint=sign-up&screen_hint=sign-up",
            ):
                response = client.get(
                    "/api/auth/workos/start?" + query, follow_redirects=False
                )
                self.assertEqual(
                    (response.status_code, response.json()["code"]),
                    (400, "workos_state_invalid"),
                )
            self.assertEqual(
                client.get(
                    "/api/auth/workos/start", follow_redirects=False
                ).status_code,
                503,
            )
            self.assertEqual(
                client.get(
                    "/api/auth/workos/start?screen_hint=sign-up", follow_redirects=False
                ).status_code,
                503,
            )
        self.assertEqual(len(self.provider.authorization_calls), before)

    def test_pinned_sdk_builds_hint_and_exact_fixed_logout_without_network(self):
        from studio_platform.workos_auth import _client

        actual = _client(self.service.config())
        self.addCleanup(actual.close)
        for hint in (None, "sign-up", "sign-in"):
            kwargs = {"screen_hint": hint} if hint else {}
            query = parse_qs(
                urlsplit(
                    actual.user_management.get_authorization_url(
                        provider="authkit",
                        redirect_uri=self.origin + "/api/auth/workos/callback",
                        **kwargs,
                    )
                ).query
            )
            self.assertEqual(query.get("screen_hint"), [hint] if hint else None)
        url = actual.user_management.get_logout_url(
            session_id="session_" + "A" * 26, return_to=self.origin + "/"
        )
        self.assertEqual(urlsplit(url).netloc, "api.workos.com")
        self.assertEqual(
            parse_qs(urlsplit(url).query),
            {"session_id": ["session_" + "A" * 26], "return_to": [self.origin + "/"]},
        )

    def test_jwks_client_uses_only_fixed_configured_url_and_bounded_cache(self):
        _jwks.cache_clear()
        self.addCleanup(_jwks.cache_clear)
        with patch("jwt.PyJWKClient") as factory:
            first = _jwks("client_" + "A" * 24)
            self.assertIs(_jwks("client_" + "A" * 24), first)
            factory.assert_called_once_with(
                "https://api.workos.com/sso/jwks/client_" + "A" * 24,
                cache_jwk_set=True,
                lifespan=300,
                timeout=10,
            )
        self.assertEqual(_jwks.cache_info().maxsize, 16)
        self.provider.headers.update(
            jku="https://evil.test/keys", x5u="file:///private"
        )
        self.hosted()
        self.mock_jwks.assert_called_once_with("client_" + "A" * 24)

    def test_bad_provider_claims_or_signature_create_no_account_session_or_sidecar(
        self,
    ):
        bad_claims = [
            {"sid": None},
            {"sid": "session_bad?return_to=evil"},
            {"sub": "user_" + "Z" * 24},
            {"client_id": "client_" + "Z" * 24},
            {"iss": "https://evil.test"},
            {"iss": "https://api.workos.com/evil"},
            {"exp": int(self.now)},
            {"exp": True},
            {"iat": int(self.now) + 1},
            {"aud": "client_" + "Z" * 24},
            {"aud": []},
            {"nbf": int(self.now) + 1},
        ]
        for claims in bad_claims:
            with self.subTest(claims=claims):
                self.provider.claims = claims
                with self.assertRaisesRegex(WorkOSError, "workos_provider_unavailable"):
                    self.hosted()
        self.provider.claims = {}
        for token in (None, "", "DO-NOT-SAVE-ACCESS", "x" * 8193):
            self.provider.access_token = token
            with self.assertRaisesRegex(WorkOSError, "workos_provider_unavailable"):
                self.hosted()
        self.provider.access_token = _DEFAULT_TOKEN
        self.provider.signing_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        with self.assertRaisesRegex(WorkOSError, "workos_provider_unavailable"):
            self.hosted()
        self.assertEqual(self.count_users(), 2)
        self.assertEqual(self.sidecars(), [])
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(func.count()).select_from(sessions)
                ).scalar_one(),
                1,
            )

    def test_documented_optional_audience_matches_exact_client_and_jwks_outage_is_static(
        self,
    ):
        for audience in ("client_" + "A" * 24, ["client_" + "A" * 24]):
            self.provider.claims = {"aud": audience}
            self.assertIsNotNone(self.auth.session(self.hosted()))
        with patch(
            "studio_platform.workos_auth._jwks",
            side_effect=RuntimeError("PRIVATE JWKS DETAIL"),
        ):
            with self.assertRaisesRegex(
                WorkOSError, "workos_provider_unavailable"
            ) as failure:
                self.hosted()
        self.assertNotIn("PRIVATE", str(failure.exception))

    def test_sidecar_insert_failure_rolls_back_session_and_original_exchange_stays_consumed(
        self,
    ):
        def fail_sidecar(_connection, _cursor, _statement, _parameters, context, _many):
            if any(
                row.get("kind") == WORKOS_SESSION_KIND
                for row in getattr(context, "compiled_parameters", None) or []
            ):
                raise RuntimeError("offline sidecar insert failure")

        value, state = self.start()
        event.listen(self.repo.engine, "before_cursor_execute", fail_sidecar)
        try:
            with self.assertRaisesRegex(RuntimeError, "offline sidecar insert failure"):
                self.finish(value, state)
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", fail_sidecar)
        self.assertEqual(self.sidecars(), [])
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    select(func.count()).select_from(sessions)
                ).scalar_one(),
                1,
            )
        with self.assertRaisesRegex(WorkOSError, "workos_state_invalid"):
            self.finish(value, state)
        self.assertEqual(len(self.provider.auth_calls), 1)

    def test_logout_binding_separates_two_sessions_owners_and_tenants(self):
        first, second = self.hosted(), self.hosted()
        by_session = {
            row["payload"]["session_hash"]: row["payload"]["sid"]
            for row in self.sidecars()
        }
        first_sid, second_sid = by_session[digest(first)], by_session[digest(second)]
        self.provider.user.id = "user_" + "B" * 24
        self.provider.user.email = "another@example.test"
        other = self.hosted()
        foreign = Auth(
            self.repo.engine,
            tenant="another-tenant",
            mode="local-test",
            clock=lambda: self.now,
        )
        self.assertIsNone(foreign.logout(first))
        value = self.auth.logout(first)
        self.assertEqual(value["sid"], first_sid)
        self.assertNotEqual(first_sid, second_sid)
        self.assertIsNone(self.auth.logout(first))
        self.assertIsNotNone(self.auth.session(second))
        self.assertIsNotNone(self.auth.session(other))
        self.assertEqual(len(self.sidecars()), 2)
        self.assertNotEqual(
            self.auth.session(second).owner, self.auth.session(other).owner
        )

    def test_sliding_local_session_keeps_logout_metadata_after_access_token_expiry(
        self,
    ):
        token = self.hosted()
        original = self.sidecars()[0]["payload"]
        self.now += 2 * 3600
        self.assertIsNotNone(self.auth.session(token))
        self.assertLess(original["access_expires_at"], self.now)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.auth.logout(token), range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)
        value = next(value for value in results if value is not None)
        self.assertIsNotNone(self.service.logout_url(value))
        self.assertIsNone(self.auth.session(token))
        self.assertEqual(self.sidecars(), [])

    def test_stale_or_malformed_metadata_never_blocks_local_logout(self):
        for field, value in (
            ("owner", "supervan"),
            ("session_hash", "f" * 64),
            ("session_created_at", self.now - 1),
            ("sid", "bad"),
        ):
            with self.subTest(field=field):
                token = self.hosted()
                row = self.sidecars()[0]
                with self.repo.transaction() as connection:
                    connection.execute(
                        update(documents)
                        .where(documents.c.document_id == row["document_id"])
                        .values(payload={**row["payload"], field: value})
                    )
                self.assertIsNone(self.auth.logout(token))
                self.assertIsNone(self.auth.session(token))
                self.assertEqual(self.sidecars(), [])

    def test_metadata_query_failure_still_revokes_local_session(self):
        token = self.hosted()

        def fail_metadata(_connection, _cursor, statement, *_args):
            if (
                statement.startswith("SELECT")
                and "platform_documents.payload" in statement
            ):
                raise RuntimeError("private sidecar read failure")

        event.listen(self.repo.engine, "before_cursor_execute", fail_metadata)
        try:
            self.assertIsNone(self.auth.logout(token))
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", fail_metadata)
        self.assertIsNone(self.auth.session(token))
        self.assertIsNone(self.auth.logout(token))
        # The failed sidecar operation leaves no authority or replay path.
        self.assertEqual(len(self.sidecars()), 1)

    def test_browser_logout_fixed_return_target_and_password_response_compatibility(
        self,
    ):
        from studio_platform.api import COOKIE

        token = self.hosted()
        with self.browser() as client:
            client.cookies.set(COOKIE, token)
            response = client.post(
                "/api/auth/logout?return_to=https://evil.test/",
                json={"return_to": "https://evil.test/", "sid": "session_" + "Z" * 26},
                headers={"x-forwarded-host": "evil.test"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                parse_qs(urlsplit(response.json()["logout_url"]).query),
                {
                    "session_id": ["session_" + "1".zfill(26)],
                    "return_to": [self.origin + "/"],
                },
            )
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["referrer-policy"], "no-referrer")
            self.assertIn("Max-Age=0", response.headers["set-cookie"])
            self.assertIsNone(self.auth.session(token))
            self.assertEqual(self.sidecars(), [])
            for owner in ("superdan", "supervan"):
                client.app.state.auth.set_password(owner, "fixture-only-password")
            login_response = client.post(
                "/api/auth/login",
                json={"username": "superdan", "password": "fixture-only-password"},
            )
            self.assertEqual(login_response.status_code, 200)
            self.assertEqual(client.post("/api/auth/logout").json(), {"ok": True})
            self.assertEqual(len(self.provider.logout_calls), 1)

    def test_legacy_missing_sid_and_provider_config_failure_still_clear_local_cookie(
        self,
    ):
        from studio_platform.api import COOKIE

        for failure in ("legacy", "config", "provider"):
            with self.subTest(failure=failure):
                token = self.hosted()
                if failure == "legacy":
                    owner = self.auth.session(token).owner
                    self.auth.logout(token)
                    token = self.auth.issue_workos_session(owner, self.provider.user.id)
                with self.browser(configured=failure != "config") as client:
                    client.cookies.set(COOKIE, token)
                    self.provider.logout_failure = (
                        "PRIVATE PROVIDER ERROR" if failure == "provider" else None
                    )
                    response = client.post("/api/auth/logout")
                    self.assertEqual(response.json(), {"ok": True})
                    self.assertIn("Max-Age=0", response.headers["set-cookie"])
                    self.assertIsNone(self.auth.session(token))
                    self.assertEqual(self.sidecars(), [])

    def test_untrusted_sdk_logout_url_or_unconfigured_origin_cannot_escape(self):
        token = self.hosted()
        value = self.auth.logout(token)
        for url in (
            "https://evil.test/logout",
            "http://api.workos.com/logout",
            "https://api.workos.com.evil.test/logout",
            "https://api.workos.com/user_management/sessions/logout?access_token=bad",
        ):
            self.provider.logout_override = url
            self.assertIsNone(self.service.logout_url(value))
        self.provider.logout_override = None
        self.settings.public_origin = "https://evil.test"
        self.assertIsNone(self.service.logout_url(value))
        self.settings.public_origin = "https://inkseq.com"
        self.assertEqual(
            parse_qs(urlsplit(self.service.logout_url(value)).query)["return_to"],
            ["https://inkseq.com/"],
        )


if __name__ == "__main__":
    unittest.main()
