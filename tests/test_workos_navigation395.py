"""Hosted browser failure and isolated IP limits; fake provider and local state."""

from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from studio_platform.auth import LoginLimited, SessionUnavailable
from studio_platform.workos_auth import STATE_COOKIE, WorkOSError


class WorkOSNavigationTests(unittest.TestCase):
    def setUp(self):
        from test_workos_auth import WorkOSAuthTests

        self.fixture = WorkOSAuthTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def test_browser_errors_are_fixed_allowlisted_redirects_api_errors_stay_json(self):
        expected = {
            "workos_state_invalid": (400, "state_invalid"),
            "workos_not_configured": (503, "provider_unavailable"),
            "workos_provider_unavailable": (503, "provider_unavailable"),
            "workos_verification_required": (403, "provider_unavailable"),
            "workos_sign_in_cancelled": (400, "cancelled"),
            "account_deleted": (403, "account_deleted"),
            "account_unavailable": (403, "account_unavailable"),
            "account_suspended": (403, "account_unavailable"),
        }
        with self.fixture.browser() as client:
            service = client.app.state.workos_auth
            for error_code, (status, public_code) in expected.items():
                for method, path, params in (
                    ("start", "/api/auth/workos/start", {}),
                    (
                        "callback",
                        "/api/auth/workos/callback",
                        {"state": "A" * 43, "code": "PRIVATE-CODE"},
                    ),
                ):
                    with (
                        self.subTest(code=error_code, method=method),
                        patch.object(
                            service, method, side_effect=WorkOSError(error_code, status)
                        ),
                    ):
                        self.fixture.now += 301
                        for headers in (
                            {"Sec-Fetch-Mode": "navigate"},
                            {"Accept": "text/html"},
                        ):
                            reply = client.get(
                                path,
                                params=params,
                                headers=headers,
                                follow_redirects=False,
                            )
                            self.assertEqual(
                                (reply.status_code, reply.headers["location"]),
                                (302, "/?signin_error=" + public_code),
                            )
                            self.assertEqual(reply.headers["cache-control"], "no-store")
                            self.assertEqual(
                                reply.headers["referrer-policy"], "no-referrer"
                            )
                            self.assertIn("Max-Age=0", reply.headers["set-cookie"])
                            self.assertNotIn("PRIVATE-CODE", str(reply.headers))
                        reply = client.get(
                            path,
                            params=params,
                            headers={"Accept": "application/json"},
                            follow_redirects=False,
                        )
                        self.assertEqual(
                            (reply.status_code, reply.json()["code"]),
                            (status, error_code),
                        )

    def test_untrusted_return_path_and_non_hosted_account_api_never_escape(self):
        with self.fixture.browser() as client:
            for path in (
                "//evil.test/private",
                "https://evil.test/private",
                "/%2f/evil.test",
            ):
                reply = client.get(
                    "/api/auth/workos/start",
                    params={"return_to": path},
                    headers={"Accept": "text/html"},
                    follow_redirects=False,
                )
                self.assertEqual(
                    (reply.status_code, reply.headers["location"]),
                    (302, "/?signin_error=state_invalid"),
                )
                self.assertNotIn("evil.test", reply.headers["location"])
            with (
                patch.object(
                    client.app.state.auth, "session", return_value=self.fixture.legacy
                ),
                patch.object(
                    client.app.state.workos_auth,
                    "account",
                    side_effect=WorkOSError("account_unavailable", 403),
                ),
            ):
                reply = client.get(
                    "/v1/account",
                    headers={"Accept": "text/html"},
                    follow_redirects=False,
                )
                self.assertEqual(
                    (reply.status_code, reply.json()["code"]),
                    (403, "account_unavailable"),
                )
                self.assertNotIn("location", reply.headers)
                self.assertNotIn("set-cookie", reply.headers)

    def test_rate_limited_extra_click_preserves_original_flow_and_cookie(self):
        with self.fixture.browser() as client:
            service = client.app.state.workos_auth
            started = client.get("/api/auth/workos/start", follow_redirects=False)
            state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
            cookie = client.cookies.get(STATE_COOKIE)
            with patch.object(
                service.auth, "reserve_workos_start", side_effect=LoginLimited(121)
            ):
                reply = client.get(
                    "/api/auth/workos/start",
                    headers={"Accept": "text/html"},
                    follow_redirects=False,
                )
                self.assertEqual(
                    (reply.status_code, reply.headers["location"]),
                    (302, "/?signin_error=rate_limited"),
                )
                self.assertEqual(reply.headers["retry-after"], "121")
                self.assertNotIn("set-cookie", reply.headers)
                self.assertEqual(client.cookies.get(STATE_COOKIE), cookie)
                reply = client.get("/api/auth/workos/start", follow_redirects=False)
                self.assertEqual(
                    (reply.status_code, reply.json()["code"]), (429, "rate_limited")
                )
                self.assertNotIn("set-cookie", reply.headers)
            finished = client.get(
                "/api/auth/workos/callback",
                params={"state": state, "code": "ORIGINAL"},
                follow_redirects=False,
            )
            self.assertEqual(finished.status_code, 302)
            self.assertEqual(finished.headers["location"], "/")
            self.assertEqual(len(self.fixture.provider.auth_calls), 1)

    def test_early_total_authentication_and_owner_capacity_redirects_are_bounded(self):
        with self.fixture.browser() as client:
            gate = client.app.state.request_admission
            for kind in ("total", "authentication", "owner"):
                owner = self.fixture.legacy.owner if kind == "owner" else None
                count = gate.limits[kind]
                for _ in range(count):
                    self.assertTrue(gate.acquire(kind, owner))
                try:
                    with patch.object(
                        client.app.state.auth,
                        "session",
                        return_value=self.fixture.legacy,
                    ):
                        for path in (
                            "/api/auth/workos/start",
                            "/api/auth/workos/callback",
                        ):
                            reply = client.get(
                                path,
                                headers={"Sec-Fetch-Mode": "navigate"},
                                follow_redirects=False,
                            )
                            self.assertEqual(
                                (reply.status_code, reply.headers["location"]),
                                (302, "/?signin_error=rate_limited"),
                            )
                            self.assertEqual(reply.headers["retry-after"], "3")
                            self.assertNotIn("set-cookie", reply.headers)
                            api = client.get(path, follow_redirects=False)
                            self.assertEqual(
                                (api.status_code, api.json()["code"]),
                                (429, "request_capacity_busy"),
                            )
                        other = client.get(
                            "/healthz",
                            headers={"Accept": "text/html"},
                            follow_redirects=False,
                        )
                        self.assertEqual(other.status_code, 429)
                        self.assertNotIn("location", other.headers)
                finally:
                    for _ in range(count):
                        gate.release(kind, owner)
            self.assertFalse(gate.counts)
        self.assertEqual(self.fixture.provider.authorization_calls, [])
        self.assertEqual(self.fixture.provider.auth_calls, [])

    def test_session_provider_failure_redirect_does_not_bypass_authority(self):
        with (
            self.fixture.browser() as client,
            patch.object(
                client.app.state.auth,
                "session",
                side_effect=SessionUnavailable("PRIVATE"),
            ),
        ):
            reply = client.get(
                "/api/auth/workos/start",
                headers={"Accept": "text/html"},
                follow_redirects=False,
            )
            self.assertEqual(
                (reply.status_code, reply.headers["location"]),
                (302, "/?signin_error=provider_unavailable"),
            )
            self.assertNotIn("set-cookie", reply.headers)
            reply = client.get("/api/auth/workos/start", follow_redirects=False)
            self.assertEqual(
                (reply.status_code, reply.json()["code"]),
                (503, "session_provider_unavailable"),
            )
        self.assertEqual(self.fixture.provider.authorization_calls, [])

    def test_hosted_twenty_and_password_five_limits_are_independent_and_atomic(self):
        auth = self.fixture.auth

        def take(_):
            try:
                auth.reserve_workos_start("same-ip")
                return True
            except LoginLimited:
                return False

        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertEqual(sum(pool.map(take, range(30))), 20)
        for _ in range(5):
            auth.reserve_login("same-ip")
        with self.assertRaises(LoginLimited):
            auth.reserve_login("same-ip")
        self.fixture.now += 301
        auth.reserve_workos_start("same-ip")
        auth.reserve_login("same-ip")


if __name__ == "__main__":
    unittest.main()
