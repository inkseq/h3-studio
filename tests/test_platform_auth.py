"""Real bcrypt/session tests on disposable state; no secret values are emitted."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import secrets
import tempfile
import unittest

from sqlalchemy import create_engine, select, update
from studio_platform.auth import (
    Auth,
    AuthenticationError,
    LoginLimited,
    accounts,
    clients,
    sessions,
)
from studio_platform.repository import Repository


class PlatformAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(
            "sqlite:///" + (Path(self.temp.name) / "auth.sqlite3").as_posix()
        )
        self.addCleanup(self.repo.close)
        self.now = 1000.0
        self.auth = Auth(self.repo.engine, clock=lambda: self.now)
        self.password = secrets.token_urlsafe(18)

    def provision(self):
        for who in ("superdan", "supervan"):
            self.auth.set_password(who, self.password)

    def test_workflow_browser_grant_does_not_renew_and_logout_revokes_it(self):
        self.provision()
        token = self.auth.login("superdan", self.password)
        principal = self.auth.session(token)
        grant = {
            key: getattr(principal, key)
            for key in (
                "actor_id",
                "machine",
                "auth_mode",
                "password_version",
                "session_hash",
            )
        }
        with self.repo.engine.connect() as connection:
            expires = connection.execute(select(sessions.c.expires)).scalar_one()
        self.now += 7200
        self.assertEqual(
            self.auth.workflow_principal("superdan", grant).owner, "superdan"
        )
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(sessions.c.expires)).scalar_one(), expires
            )
        with self.assertRaises(AuthenticationError):
            self.auth.workflow_principal("supervan", grant)
        self.auth.logout(token)
        with self.assertRaises(AuthenticationError):
            self.auth.workflow_principal("superdan", grant)

    def test_workflow_key_grant_rechecks_current_scope_and_revocation(self):
        self.provision()
        token = self.auth.login("superdan", self.password)
        key = self.auth.create_key(
            "superdan",
            authenticated_session=self.auth.session(token),
            name="workflow-test",
            scopes=["projects:read", "jobs:read", "jobs:write"],
            project_ids=["owned-story"],
        )
        principal = self.auth.bearer(key["api_key"])
        grant = {
            field: getattr(principal, field)
            for field in (
                "actor_id",
                "machine",
                "auth_mode",
                "password_version",
                "session_hash",
            )
        }
        current = self.auth.workflow_principal("superdan", grant)
        self.assertTrue(current.allows("owned-story", "jobs:write"))
        self.assertFalse(current.allows("foreign-story", "jobs:write"))
        self.auth.revoke_key("superdan", key["id"])
        with self.assertRaises(AuthenticationError):
            self.auth.workflow_principal("superdan", grant)

    def test_unprovisioned_and_partial_provision_do_not_authenticate(self):
        self.assertFalse(self.auth.ready())
        self.auth.set_password("superdan", self.password)
        self.assertFalse(self.auth.ready())
        with self.assertRaises(AuthenticationError):
            self.auth.login("superdan", self.password)

    def test_real_password_token_hash_rotation_and_expiry(self):
        self.provision()
        token = self.auth.login("superdan", self.password)
        self.assertEqual(self.auth.session(token).owner, "superdan")
        with self.repo.engine.connect() as conn:
            row = conn.execute(select(sessions)).mappings().one()
            self.assertNotEqual(row["token_hash"], token)
            self.assertEqual(len(row["token_hash"]), 64)
        self.auth.set_password("superdan", secrets.token_urlsafe(18))
        self.assertIsNone(self.auth.session(token))
        van = self.auth.login("supervan", self.password, source="another")
        self.now += self.auth.session_seconds + 1
        self.assertIsNone(self.auth.session(van))

    def test_disabling_one_user_preserves_other_and_revokes_machine_access(self):
        self.provision()
        dan = self.auth.login("superdan", self.password)
        van = self.auth.login("supervan", self.password)
        machine = secrets.token_urlsafe(32)
        self.auth.register_client("machine", machine, "supervan", ["p"], ["jobs:read"])
        self.assertIsNotNone(self.auth.bearer(machine))
        with self.repo.engine.begin() as conn:
            conn.execute(
                update(accounts)
                .where(
                    accounts.c.tenant == "sixnine", accounts.c.username == "supervan"
                )
                .values(disabled=1)
            )
        self.assertIsNotNone(self.auth.session(dan))
        self.assertIsNone(self.auth.session(van))
        self.assertIsNone(self.auth.bearer(machine))

    def test_concurrent_rate_limit_is_not_bypassed(self):
        def take(_):
            try:
                self.auth.reserve_login("shared-source")
                return True
            except LoginLimited:
                return False

        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(take, range(20)))
        self.assertEqual(sum(outcomes), 5)
        self.now += 301
        self.auth.reserve_login("shared-source")

    def test_malformed_password_hash_fails_closed(self):
        self.provision()
        with self.repo.engine.begin() as conn:
            conn.execute(
                update(accounts)
                .where(accounts.c.username == "superdan")
                .values(password_hash="not-a-bcrypt-hash")
            )
        self.assertFalse(self.auth.ready())
        with self.assertRaises(AuthenticationError):
            self.auth.login("superdan", self.password)

    def test_local_sessions_cannot_upgrade_to_password_mode(self):
        local = Auth(self.repo.engine, mode="local-test")
        token = local.login("superdan", "")
        self.provision()
        self.assertIsNone(self.auth.session(token))

    def test_session_and_machine_checks_do_not_checkout_nested_connections(self):
        self.provision()
        token = self.auth.login("superdan", self.password)
        machine = secrets.token_urlsafe(32)
        self.auth.register_client(
            "small-pool", machine, "superdan", ["p"], ["jobs:read"]
        )
        engine = create_engine(
            self.repo.engine.url,
            pool_size=1,
            max_overflow=0,
            pool_timeout=0.1,
            connect_args={"check_same_thread": False},
        )
        self.addCleanup(engine.dispose)
        limited = Auth(engine, clock=lambda: self.now)

        def validate(index):
            return (
                limited.session(token) if index % 2 else limited.bearer(machine)
            ).owner

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(validate, range(20))), ["superdan"] * 20)
        with engine.begin() as conn:
            conn.execute(
                update(accounts)
                .where(accounts.c.username == "superdan")
                .values(disabled=1)
            )
        self.assertIsNone(limited.session(token))
        self.assertIsNone(limited.bearer(machine))


if __name__ == "__main__":
    unittest.main()
