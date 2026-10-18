"""Disposable browser/session clocks; no live account, credential or network."""

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from studio_platform.api import BROWSER_HANDLE_SECONDS, COOKIE, create_app
from studio_platform.auth import Auth, sessions
from studio_platform.settings import Settings


class SlidingSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(Path(self.tmp.name), auth_mode="local-test")
        self.app = create_app(self.settings)
        self.addCleanup(self.app.state.repository.close)
        self.client = TestClient(self.app)
        self.now = 1000.0
        self.app.state.auth.clock = lambda: self.now

    def login(self, username="superdan"):
        response = self.client.post("/api/auth/login", json={"username": username})
        self.assertEqual(response.status_code, 200)
        return response

    def expiry(self):
        with self.app.state.repository.engine.connect() as conn:
            return conn.execute(select(sessions.c.expires)).scalar_one()

    def test_seven_day_authority_slides_after_twelve_hours_and_survives_app_recreation(
        self,
    ):
        response = self.login()
        self.assertIn(
            f"Max-Age={BROWSER_HANDLE_SECONDS}", response.headers["set-cookie"]
        )
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=lax", response.headers["set-cookie"])
        first = self.expiry()
        self.now += 13 * 3600
        response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.expiry(), first + 13 * 3600)
        # A fresh app/Auth instance reads the same original session row.
        recreated = Auth(
            self.app.state.repository.engine, mode="local-test", clock=lambda: self.now
        )
        self.assertEqual(
            recreated.session(self.client.cookies.get(COOKIE)).owner, "superdan"
        )
        self.now += 6 * 86400
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        self.now += 7 * 86400 + 1
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_reads_do_not_write_or_set_a_cookie_on_every_poll(self):
        self.login()
        initial = self.expiry()
        self.now += 3599
        for _ in range(4):
            response = self.client.get("/api/auth/me")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.expiry(), initial)
        self.now += 1
        self.assertNotIn("set-cookie", self.client.get("/api/auth/me").headers)
        self.assertEqual(self.expiry(), self.now + 7 * 86400)

    def test_error_response_activity_extends_server_authority_without_a_cookie_race(
        self,
    ):
        self.login()
        self.now += 3600
        response = self.client.get("/v1/fixture-missing")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("set-cookie", response.headers)
        expiry = self.expiry()
        self.now += 100
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        self.assertEqual(self.expiry(), expiry)
        # Retaining an opaque browser handle never bypasses inactivity expiry.
        self.now = expiry + 1
        self.assertIsNotNone(self.client.cookies.get(COOKIE))
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_late_old_request_cannot_overwrite_account_switch_or_restore_logout(self):
        from fastapi import Request

        entered, release = threading.Event(), threading.Event()

        @self.app.get("/v1/fixture-blocked")
        def blocked(request: Request):
            entered.set()
            if not release.wait(10):
                raise AssertionError("fixture request not released")
            return {"owner": request.state.principal.owner}

        for action in ("switch", "logout"):
            with self.subTest(action=action):
                self.login()
                self.now += 3600
                entered.clear()
                release.clear()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(self.client.get, "/v1/fixture-blocked")
                    try:
                        self.assertTrue(entered.wait(10))
                        if action == "switch":
                            self.login("supervan")
                            current = self.client.cookies.get(COOKIE)
                        else:
                            self.assertEqual(
                                self.client.post("/api/auth/logout").status_code, 200
                            )
                    finally:
                        release.set()
                    response = pending.result(timeout=10)
                self.assertEqual(response.json()["owner"], "superdan")
                self.assertNotIn("set-cookie", response.headers)
                if action == "switch":
                    self.assertEqual(self.client.cookies.get(COOKIE), current)
                    self.assertEqual(
                        self.client.get("/api/auth/me").json()["username"], "supervan"
                    )
                else:
                    self.assertIsNone(self.client.cookies.get(COOKIE))
                    self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_old_valid_short_session_upgrades_on_use_but_expired_session_does_not(self):
        old = Auth(
            self.app.state.repository.engine,
            mode="local-test",
            session_seconds=12 * 3600,
            clock=lambda: self.now,
        )
        token = old.login("superdan", "")
        self.client.cookies.set(COOKIE, token)
        self.now += 3600
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        self.assertEqual(self.expiry(), self.now + 7 * 86400)
        self.now += 7 * 86400 + 1
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_login_does_not_overwrite_new_account_cookie_with_old_renewal_and_logout_revokes(
        self,
    ):
        self.login()
        self.now += 3600
        response = self.login("supervan")
        self.assertEqual(len(response.headers.get_list("set-cookie")), 1)
        self.assertEqual(self.client.get("/api/auth/me").json()["username"], "supervan")
        self.now += 3600
        response = self.client.post("/api/auth/logout")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.headers.get_list("set-cookie")), 1)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_password_change_keeps_revocation_and_pat_deadline(self):
        # Only disposable fake passwords: a real account password is never changed.
        app = create_app(
            Settings(
                Path(self.tmp.name),
                database_url=self.settings.database_url,
                public_origin="https://studio.example.org",
            )
        )
        self.addCleanup(app.state.repository.close)
        auth = app.state.auth
        auth.clock = lambda: self.now
        client = TestClient(app, base_url="https://studio.example.org")
        self.addCleanup(client.close)
        password = "fixture-only-browser-password"
        for owner in ("superdan", "supervan"):
            auth.set_password(owner, password)
        response = client.post(
            "/api/auth/login", json={"username": "superdan", "password": password}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Secure", response.headers["set-cookie"])
        token = client.cookies.get(COOKIE)
        body = {
            "name": "fixture",
            "scopes": ["projects:read"],
            "all_projects": True,
            "expires_in_days": 1,
        }
        key = client.post("/v1/api-keys", json=body).json()
        expires = key["key"]["expires_at"]
        self.now += 13 * 3600
        self.assertEqual(client.get("/api/auth/me").status_code, 200)
        response = client.get(
            "/v1/projects", headers={"Authorization": "Bearer " + key["api_key"]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(auth.list_keys("superdan")[0]["expires_at"], expires)
        self.now = expires + 1
        self.assertEqual(
            client.get(
                "/v1/projects", headers={"Authorization": "Bearer " + key["api_key"]}
            ).status_code,
            401,
        )
        self.assertEqual(client.get("/api/auth/me").status_code, 200)
        fresh = client.post("/v1/api-keys", json={**body, "expires_in_days": 90}).json()
        self.now += 3600
        response = client.post(
            "/v1/auth/password",
            json={
                "old_password": password,
                "new_password": "fixture-only-new-password",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.headers.get_list("set-cookie")), 1)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertIsNone(auth.session(token))
        self.assertIsNone(auth.bearer(fresh["api_key"]))
        self.assertEqual(client.get("/api/auth/me").status_code, 401)

    def test_duration_config_is_explicit_and_invalid_values_fail_closed(self):
        with patch.dict(
            "os.environ", {"INKSEQ_BROWSER_SESSION_SECONDS": "900"}, clear=True
        ):
            self.assertEqual(Settings.from_environment().session_seconds, 900)
        for value in ("0", "true", "-1", " 900", "31536001"):
            with (
                self.subTest(value=value),
                patch.dict(
                    "os.environ", {"INKSEQ_BROWSER_SESSION_SECONDS": value}, clear=True
                ),
            ):
                with self.assertRaises(ValueError):
                    Settings.from_environment()
        with patch.dict(
            "os.environ",
            {
                "INKSEQ_BROWSER_SESSION_SECONDS": "900",
                "SIXNINE_SESSION_SECONDS": "1800",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                Settings.from_environment()


if __name__ == "__main__":
    unittest.main()
