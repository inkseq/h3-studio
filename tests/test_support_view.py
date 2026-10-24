"""Read-only admin support view over a disposable ledger; no providers or GPU."""

from types import SimpleNamespace
import unittest
import uuid

from sqlalchemy import insert, select

from studio_platform.auth import Principal
from studio_platform.identity_schema import admin_audit, metadata, users
from studio_platform.support_view import (
    SupportViewError,
    end,
    refusal,
    resolve,
    start,
)
from test_platform_repository import LedgerCase


class SupportViewTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = 1_800_000_000.0
        with self.repo.transaction() as connection:
            metadata.create_all(connection)
            self.ids = {}
            for owner, role in (
                ("superdan", "admin"),
                ("supervan", "user"),
                ("boss", "admin"),
            ):
                self.ids[owner] = str(uuid.uuid4())
                connection.execute(
                    insert(users).values(
                        id=self.ids[owner],
                        tenant=self.scope.tenant_id,
                        owner=owner,
                        role=role,
                        status="active",
                        display_name=owner,
                        email=None,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
        self.settings = SimpleNamespace(tenant_id=self.scope.tenant_id)
        self.admin = Principal("superdan", "browser:superdan")
        self.user = Principal("supervan", "browser:supervan")

    def view(self, **body):
        return start(
            self.repo,
            self.settings,
            self.admin,
            {"user_id": self.ids["supervan"], "reason": "stuck generation", **body},
        )

    def test_admin_sees_the_user_read_only_and_it_is_audited(self):
        view = self.view()
        self.assertTrue(view["read_only"])
        seen = resolve(self.repo, self.settings, self.admin, view["id"])
        self.assertEqual(seen.owner, "supervan")
        self.assertEqual(seen.support_viewer, "superdan")
        self.assertFalse(seen.machine)
        self.assertEqual(seen.role, "user")
        end(self.repo, self.settings, self.admin, view["id"])
        self.assertIsNone(resolve(self.repo, self.settings, self.admin, view["id"]))
        with self.repo.engine.connect() as connection:
            actions = [
                r.action for r in connection.execute(select(admin_audit.c.action))
            ]
        self.assertEqual(
            sorted(actions), ["support view ended", "support view started (read-only)"]
        )

    def test_view_expires_and_is_bound_to_its_admin(self):
        view = self.view()
        other = Principal("boss", "browser:boss")
        self.assertIsNone(resolve(self.repo, self.settings, other, view["id"]))
        self.assertIsNone(resolve(self.repo, self.settings, self.user, view["id"]))
        machine = Principal("superdan", "pat:x", machine=True)
        self.assertIsNone(resolve(self.repo, self.settings, machine, view["id"]))
        self.now += 1801
        self.assertIsNone(resolve(self.repo, self.settings, self.admin, view["id"]))
        self.assertIsNone(resolve(self.repo, self.settings, self.admin, "not-an-id"))

    def test_only_admins_start_and_never_onto_admins_or_with_a_view(self):
        with self.assertRaises(SupportViewError):
            start(
                self.repo,
                self.settings,
                self.user,
                {"user_id": self.ids["supervan"], "reason": "x reason"},
            )
        with self.assertRaises(SupportViewError):
            self.view(user_id=self.ids["boss"])
        with self.assertRaises(SupportViewError):
            self.view(reason=" ")
        seen = resolve(self.repo, self.settings, self.admin, self.view()["id"])
        with self.assertRaises(SupportViewError):
            start(
                self.repo,
                self.settings,
                seen,
                {"user_id": self.ids["supervan"], "reason": "nested"},
            )

    def test_writes_and_sensitive_paths_are_refused(self):
        self.assertIsNone(refusal("GET", "/v1/projects"))
        self.assertIsNone(refusal("GET", "/v1/jobs"))
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(refusal(method, "/v1/projects"), "support_view_read_only")
        for path in (
            "/api/auth/session",
            "/v1/admin/users",
            "/v1/operator/admin/jobs",
            "/v1/api-keys",
        ):
            self.assertEqual(refusal("GET", path), "support_view_path_blocked")


class SupportViewHttpTests(unittest.TestCase):
    """The middleware swaps identity only with a live view and refuses every write."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from fastapi.testclient import TestClient
        from studio_platform.api import create_app
        from studio_platform.settings import Settings

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = create_app(
            Settings(Path(tmp.name), auth_mode="local-test", story_chat_enabled=True)
        )
        self.addCleanup(self.app.state.repository.engine.dispose)
        repo = self.app.state.repository
        self.ids = {}
        with repo.engine.begin() as connection:
            metadata.create_all(connection)
            for owner, role in (("superdan", "admin"), ("supervan", "user")):
                self.ids[owner] = str(uuid.uuid4())
                connection.execute(
                    insert(users).values(
                        id=self.ids[owner],
                        tenant=self.app.state.settings.tenant_id,
                        owner=owner,
                        role=role,
                        status="active",
                        display_name=owner,
                        email=None,
                        created_at=repo.clock(),
                        updated_at=repo.clock(),
                    )
                )
        self.client = TestClient(self.app)

    def login(self, who):
        self.assertEqual(
            self.client.post("/api/auth/login", json={"username": who}).status_code, 200
        )

    def test_header_views_the_user_and_refuses_writes(self):
        self.login("supervan")
        made = self.client.post("/v1/story-chats", json={"language": "en"})
        self.assertEqual(made.status_code, 201, made.text)
        pid = made.json()["project_id"]
        self.client.post("/api/auth/logout")
        self.login("superdan")
        view = self.client.post(
            "/v1/admin/support-views",
            json={"user_id": self.ids["supervan"], "reason": "check stuck chat"},
        )
        self.assertEqual(view.status_code, 201, view.text)
        head = {"X-Support-View": view.json()["id"]}
        seen = self.client.get("/v1/story-chats", headers=head)
        self.assertEqual(seen.status_code, 200, seen.text)
        self.assertIn(pid, [row["project_id"] for row in seen.json()["story_chats"]])
        own = self.client.get("/v1/story-chats")
        self.assertNotIn(pid, [row["project_id"] for row in own.json()["story_chats"]])
        write = self.client.post("/v1/story-chats", json={}, headers=head)
        self.assertEqual(
            (write.status_code, write.json()["code"]), (403, "support_view_read_only")
        )
        me = self.client.get("/api/auth/me", headers=head).json()
        self.assertEqual(me["username"], "supervan")
        self.assertEqual(me["support_view"], {"viewer": "superdan", "read_only": True})
        self.assertNotIn("support_view", self.client.get("/api/auth/me").json())
        admin = self.client.get("/v1/admin/users", headers=head)
        self.assertEqual(admin.json()["code"], "support_view_path_blocked")
        bogus = self.client.get("/v1/story-chats", headers={"X-Support-View": "0" * 32})
        self.assertEqual(bogus.json()["code"], "support_view_invalid")
        ended = self.client.post(
            f"/v1/admin/support-views/{head['X-Support-View']}/end"
        )
        self.assertEqual(ended.status_code, 200, ended.text)
        after = self.client.get("/v1/story-chats", headers=head)
        self.assertEqual(after.status_code, 403)
