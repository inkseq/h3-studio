"""Offline signed callback contracts; no hosted provider roundtrip is claimed."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from studio_platform.auth import AuthenticationError
from studio_platform.repository import request_hash
from studio_platform.session_authority import deleted_workos_subject, write
from studio_platform.workos_auth import (
    CALLBACK_FAILURE_PHASES,
    STATE_COOKIE,
    WORKOS_API_ORIGIN,
    WorkOSError,
    _client,
    _issuers,
    _jwks,
)


class CallbackTokenTests(unittest.TestCase):
    def setUp(self):
        # Compose the shared fixture without rediscovering its existing suite.
        from test_workos_auth import WorkOSAuthTests

        self.fixture = WorkOSAuthTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service
        self.client_id = self.service.config()["client_id"]

    def token(self, *, omit=(), key=None, headers=None, **changes):
        claims = {
            "iss": "https://api.workos.com/",
            "sub": self.fixture.provider.user.id,
            "client_id": self.client_id,
            "sid": "session_" + "S" * 24,
            "iat": int(self.fixture.now),
            "exp": int(self.fixture.now) + 900,
            **changes,
        }
        for name in omit:
            claims.pop(name)
        return jwt.encode(
            claims,
            key or self.fixture.provider.key,
            algorithm="RS256",
            headers={"kid": "offline-key", **(headers or {})},
        )

    def verify(self, token):
        return self.service._provider_session(
            SimpleNamespace(access_token=token),
            {"subject": self.fixture.provider.user.id},
            self.service.config(),
        )

    def assert_diagnostic(self, captured, phase):
        self.assertIn(phase, CALLBACK_FAILURE_PHASES)
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.getMessage(), "workos_callback_failed phase=" + phase)
        self.assertEqual(record.args, (phase,))
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.stack_info)

    def refused(self, token, phase="jwt_claims"):
        with (
            self.assertLogs("studio_platform.workos_auth", level="WARNING") as logged,
            self.assertRaises(WorkOSError) as caught,
        ):
            self.verify(token)
        self.assertEqual(
            (caught.exception.code, caught.exception.status),
            ("workos_provider_unavailable", 503),
        )
        self.assert_diagnostic(logged, phase)

    def test_both_documented_issuers_bind_missing_client_to_server_exchange(self):
        for issuer in _issuers(self.client_id):
            with self.subTest(issuer=issuer):
                self.fixture.provider.access_token = self.token(
                    omit=("client_id",), iss=issuer
                )
                value, state = self.fixture.start()
                result = self.fixture.finish(value, state)
                principal = self.fixture.auth.session(result["token"])
                self.assertEqual(principal.auth_mode, "workos")
                sidecar = self.fixture.sidecars()[-1]["payload"]
                self.assertEqual(sidecar["client_id"], self.client_id)
                self.assertEqual(sidecar["subject"], self.fixture.provider.user.id)
                before = len(self.fixture.provider.auth_calls)
                with self.assertRaisesRegex(WorkOSError, "workos_state_invalid"):
                    self.fixture.finish(value, state)
                self.assertEqual(len(self.fixture.provider.auth_calls), before)
        self.fixture.mock_jwks.assert_called_with(self.client_id)
        serialized = json.dumps(self.fixture.sidecars())
        self.assertNotIn(self.fixture.provider.access_token, serialized)
        self.assertNotIn("DO-NOT-SAVE-REFRESH", serialized)

    def test_optional_present_client_and_audience_remain_exact(self):
        for audience in (self.client_id, [self.client_id]):
            with self.subTest(audience=audience):
                self.assertEqual(
                    self.verify(self.token(aud=audience))["client_id"], self.client_id
                )
        for changes in (
            {"client_id": None},
            {"client_id": "client_" + "B" * 24},
            {"aud": None},
            {"aud": "client_" + "B" * 24},
            {"aud": [self.client_id, "other"]},
            {"sub": "user_" + "B" * 24},
        ):
            with self.subTest(changes=changes):
                self.refused(self.token(**changes))

    def test_issuer_is_exact_and_never_selects_a_remote_url(self):
        for issuer in (
            WORKOS_API_ORIGIN,
            "https://api.workos.com.evil.test/",
            "http://api.workos.com/",
            "https://api.workos.com/extra",
            WORKOS_API_ORIGIN + "/user_management/client_" + "B" * 24,
            WORKOS_API_ORIGIN + "/user_management/" + self.client_id + "/",
            WORKOS_API_ORIGIN + "/user_management/" + self.client_id + "/extra",
            "https://attacker.test/user_management/" + self.client_id,
        ):
            with self.subTest(issuer=issuer):
                self.refused(self.token(iss=issuer), "jwt_issuer")
        self.refused(self.token(omit=("iss",)), "jwt_issuer")
        self.assertEqual(
            self.verify(
                self.token(
                    headers={
                        "jku": "https://attacker.test/private-jwks",
                        "x5u": "https://attacker.test/private-key",
                    }
                )
            )["client_id"],
            self.client_id,
        )
        self.assertEqual(
            {call.args for call in self.fixture.mock_jwks.call_args_list},
            {(self.client_id,)},
        )

    def test_fixed_sdk_and_jwks_configuration_has_bounded_cache_and_timeout(self):
        config = self.service.config()
        with patch("workos.WorkOSClient") as constructor:
            _client(config)
        self.assertEqual(
            constructor.call_args.kwargs,
            {
                "api_key": config["api_key"],
                "client_id": self.client_id,
                "base_url": WORKOS_API_ORIGIN,
                "request_timeout": 15,
                "max_retries": 0,
                "jwt_issuer": _issuers(self.client_id),
            },
        )
        with patch("jwt.PyJWKClient") as constructor:
            _jwks.__wrapped__(self.client_id)
        constructor.assert_called_once_with(
            WORKOS_API_ORIGIN + "/sso/jwks/" + self.client_id,
            cache_jwk_set=True,
            lifespan=300,
            timeout=10,
        )

    def test_required_subject_sid_and_integer_times_are_not_relaxed(self):
        for name in ("sub", "sid", "iat", "exp"):
            with self.subTest(missing=name):
                self.refused(self.token(omit=(name,)))
        for changes in (
            {"sid": "bad"},
            {"sid": None},
            {"iat": True},
            {"iat": 1000.0},
            {"iat": 1001},
            {"iat": -1},
            {"exp": True},
            {"exp": 1000},
            {"exp": 999},
            {"nbf": True},
            {"nbf": 1001},
        ):
            with self.subTest(changes=changes):
                self.refused(self.token(**changes))

    def test_signature_algorithm_and_jwks_failure_are_finite(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.refused(self.token(key=other))
        self.refused(
            jwt.encode({"private": "claim"}, "offline-only-" * 4, algorithm="HS256")
        )
        self.refused("not-a-jwt")
        with patch(
            "studio_platform.workos_auth._jwks",
            side_effect=RuntimeError("PRIVATE-JWKS-ERROR"),
        ):
            self.refused(self.token(), "jwks")

    def test_exchange_provider_shape_upsert_and_issuance_errors_are_private(self):
        for phase in ("exchange", "provider_shape", "upsert", "issuance"):
            with self.subTest(phase=phase):
                value, state = self.fixture.start()
                if phase == "exchange":
                    target, attribute = self.fixture.provider, "authenticate_with_code"
                elif phase == "provider_shape":
                    target, attribute = self.service, "_provider_user"
                elif phase == "upsert":
                    target, attribute = self.service, "_upsert"
                else:
                    target, attribute = self.fixture.auth, "issue_workos_session"
                with (
                    patch.object(
                        target, attribute, side_effect=RuntimeError("PRIVATE-ERROR")
                    ),
                    self.assertLogs(
                        "studio_platform.workos_auth", level="WARNING"
                    ) as logged,
                    self.assertRaises(WorkOSError) as caught,
                ):
                    self.fixture.finish(value, state)
                self.assertEqual(
                    (caught.exception.code, caught.exception.status),
                    ("workos_provider_unavailable", 503),
                )
                self.assertNotIn("PRIVATE-ERROR", str(caught.exception))
                self.assert_diagnostic(
                    logged, "upsert" if phase == "issuance" else phase
                )
        self.assertEqual(self.fixture.sidecars(), [])

    def test_specific_fences_and_verification_errors_preserve_their_codes(self):
        for code in (
            "account_deleted",
            "account_suspended",
            "workos_identity_conflict",
        ):
            value, state = self.fixture.start()
            status = 409 if code == "workos_identity_conflict" else 403
            with (
                self.subTest(code=code),
                patch.object(
                    self.service, "_upsert", side_effect=WorkOSError(code, status)
                ),
                self.assertLogs(
                    "studio_platform.workos_auth", level="WARNING"
                ) as logged,
                self.assertRaises(WorkOSError) as caught,
            ):
                self.fixture.finish(value, state)
            self.assertEqual(
                (caught.exception.code, caught.exception.status), (code, status)
            )
            self.assert_diagnostic(logged, "upsert")
        self.fixture.provider.user.email_verified = False
        value, state = self.fixture.start()
        with self.assertRaisesRegex(WorkOSError, "workos_verification_required"):
            self.fixture.finish(value, state)

    def test_rejected_flow_keeps_exact_verified_sid_and_failure_is_private(self):
        for failure in (None, RuntimeError("PRIVATE-REJECTED-FLOW")):
            value, state = self.fixture.start()
            with (
                patch.object(
                    self.fixture.auth,
                    "issue_workos_session",
                    side_effect=AuthenticationError("PRIVATE-ISSUANCE"),
                ),
                patch(
                    "studio_platform.account_sessions.AccountSessions.rejected_flow",
                    side_effect=failure,
                ) as rejected,
                self.assertLogs(
                    "studio_platform.workos_auth", level="WARNING"
                ) as logged,
                self.assertRaises(WorkOSError) as caught,
            ):
                self.fixture.finish(value, state)
            self.assertEqual(
                (caught.exception.code, caught.exception.status),
                ("workos_provider_unavailable", 503)
                if failure
                else ("account_unavailable", 403),
            )
            self.assert_diagnostic(logged, "rejected_flow")
            owner, epoch, provider_session = rejected.call_args.args
            self.assertTrue(owner.startswith("u_"))
            self.assertEqual(epoch, 0)
            self.assertEqual(provider_session["subject"], self.fixture.provider.user.id)
            self.assertEqual(provider_session["client_id"], self.client_id)
            self.assertEqual(
                provider_session["sid"],
                "session_" + str(len(self.fixture.provider.auth_calls)).zfill(26),
            )
        self.assertEqual(self.fixture.sidecars(), [])

    def test_documented_issuers_do_not_change_durable_deleted_subject_identity(self):
        tenant, subject = self.fixture.scope.tenant_id, self.fixture.provider.user.id
        expected = request_hash(
            {
                "tenant": tenant,
                "provider": "workos",
                "issuer": WORKOS_API_ORIGIN,
                "subject": subject,
            }
        )
        self.assertEqual(deleted_workos_subject(tenant, subject), expected)
        with self.fixture.repo.transaction() as connection:
            write(
                connection,
                tenant,
                "deleted_subject",
                expected,
                {"state": "deleted"},
                self.fixture.now,
            )
        for client_id in (self.client_id, "client_" + "C" * 24):
            self.fixture.config_file.write_text(
                json.dumps({"client_id": client_id, "api_key": "sk_test_" + "B" * 30}),
                encoding="utf-8",
            )
            for issuer in _issuers(client_id):
                with self.subTest(client_id=client_id, issuer=issuer):
                    self.fixture.provider.access_token = self.token(
                        omit=("client_id",), iss=issuer
                    )
                    value, state = self.fixture.start()
                    with self.assertRaisesRegex(WorkOSError, "account_deleted"):
                        self.fixture.finish(value, state)
                    self.assertEqual(self.fixture.count_users(), 2)
                    self.assertEqual(self.fixture.sidecars(), [])
                    self.assertEqual(deleted_workos_subject(tenant, subject), expected)

    def test_unexpected_callback_errors_use_browser_redirect_or_static_api_json(self):
        for failure_at in ("_upsert", "issue_workos_session"):
            for headers in ({"Accept": "text/html"}, {"Accept": "application/json"}):
                with self.fixture.browser() as client:
                    started = client.get(
                        "/api/auth/workos/start", follow_redirects=False
                    )
                    state = parse_qs(urlsplit(started.headers["location"]).query)[
                        "state"
                    ][0]
                    service = client.app.state.workos_auth
                    target = service if failure_at == "_upsert" else service.auth
                    with patch.object(
                        target,
                        failure_at,
                        side_effect=RuntimeError("PRIVATE-BROWSER-ERROR"),
                    ):
                        reply = client.get(
                            "/api/auth/workos/callback",
                            params={"state": state, "code": "PRIVATE-CODE"},
                            headers=headers,
                            follow_redirects=False,
                        )
                    if headers["Accept"] == "text/html":
                        self.assertEqual(
                            (reply.status_code, reply.headers["location"]),
                            (302, "/?signin_error=provider_unavailable"),
                        )
                    else:
                        self.assertEqual(
                            (reply.status_code, reply.json()["code"]),
                            (503, "workos_provider_unavailable"),
                        )
                    self.assertEqual(reply.headers["cache-control"], "no-store")
                    self.assertEqual(reply.headers["referrer-policy"], "no-referrer")
                    self.assertIn("Max-Age=0", reply.headers["set-cookie"])
                    self.assertIsNone(client.cookies.get(STATE_COOKIE))
                    self.assertNotIn("PRIVATE-", reply.text + str(reply.headers))
        self.assertEqual(self.fixture.sidecars(), [])


if __name__ == "__main__":
    unittest.main()
