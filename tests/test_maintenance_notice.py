"""#453 release notice: anonymous config, admin switch, audit and CLI path."""

from pathlib import Path
import tempfile
import unittest
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert, inspect, select

from studio_platform import maintenance_notice as notice
from studio_platform.api import create_app
from studio_platform.api_errors import register_handlers
from studio_platform.identity_schema import admin_audit, users
from studio_platform.repository import documents
from studio_platform.settings import Settings


class MaintenanceNoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        notice.forget_cache()
        self.addCleanup(notice.forget_cache)
        self.settings = Settings(Path(self.tmp.name), auth_mode="local-test")
        self.app = create_app(self.settings)
        self.repo = self.app.state.repository
        self.addCleanup(self.repo.engine.dispose)
        existing = set(inspect(self.repo.engine).get_table_names())
        for table in (users, admin_audit):
            if table.name not in existing:
                table.create(self.repo.engine)
        with self.repo.transaction() as conn:
            for owner, role in (("superdan", "admin"), ("supervan", "user")):
                conn.execute(
                    insert(users).values(
                        id=str(uuid.uuid4()),
                        tenant=self.settings.tenant_id,
                        owner=owner,
                        role=role,
                        status="active",
                        display_name=owner,
                        email=None,
                        email_verified=0,
                        created_at=self.repo.clock(),
                        updated_at=self.repo.clock(),
                        workos_user_id=None,
                    )
                )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def login(self, who):
        self.client.cookies.clear()
        self.client.post("/api/auth/login", json={"username": who}).raise_for_status()

    def config(self):
        self.client.cookies.clear()
        return self.client.get("/api/auth/config").json()["maintenance_notice"]

    def test_anonymous_config_shows_inactive_by_default(self):
        self.assertEqual(self.config(), notice.INACTIVE)

    def test_admin_turns_it_on_and_off_with_audit_and_cas(self):
        self.login("supervan")
        self.assertEqual(
            self.client.get("/v1/admin/maintenance-notice").status_code, 403
        )
        self.assertEqual(
            self.client.put(
                "/v1/admin/maintenance-notice",
                json={"expected_version": 0, "active": True, "minutes": 15},
            ).status_code,
            403,
        )
        self.login("superdan")
        for body in (
            {"expected_version": 0, "active": True},
            {"expected_version": 0, "active": True, "minutes": 0},
            {"expected_version": 0, "active": True, "minutes": 15, "severity": "x"},
            {"expected_version": 0, "active": "yes", "minutes": 15},
            {"active": True, "minutes": 15},
        ):
            with self.subTest(body=body):
                reply = self.client.put("/v1/admin/maintenance-notice", json=body)
                self.assertEqual(reply.status_code, 422, reply.text)
        on = self.client.put(
            "/v1/admin/maintenance-notice",
            json={
                "expected_version": 0,
                "active": True,
                "minutes": 15,
                "message": "Updating video generation",
            },
        )
        self.assertEqual(on.status_code, 200, on.text)
        value = on.json()
        self.assertTrue(value["active"])
        self.assertEqual(value["expected_end_at"] - value["started_at"], 900)
        public = self.config()
        self.assertTrue(public["active"])
        self.assertEqual(public["message"], "Updating video generation")
        self.assertNotIn("version", public)
        self.login("superdan")
        stale = self.client.put(
            "/v1/admin/maintenance-notice",
            json={"expected_version": 0, "active": False},
        )
        self.assertEqual(stale.status_code, 409)
        off = self.client.put(
            "/v1/admin/maintenance-notice",
            json={"expected_version": value["version"], "active": False},
        )
        self.assertEqual(off.status_code, 200, off.text)
        self.assertEqual(self.config(), notice.INACTIVE)
        with self.repo.engine.connect() as conn:
            actions = conn.execute(
                select(admin_audit.c.action).where(
                    admin_audit.c.action == "maintenance.notice"
                )
            ).all()
        self.assertEqual(len(actions), 2)

    def test_release_path_is_audited_and_off_twice_is_harmless(self):
        tenant = self.settings.tenant_id
        with self.repo.transaction() as conn:
            on = notice.release_write(
                conn, tenant, {"active": True, "minutes": 10}, 1000.0
            )
        self.assertEqual((on["started_at"], on["expected_end_at"]), (1000.0, 1600.0))
        with self.repo.transaction() as conn:
            notice.release_write(conn, tenant, {"active": False}, 1200.0)
            again = notice.release_write(conn, tenant, {"active": False}, 1300.0)
        self.assertFalse(again["active"])
        with self.repo.engine.connect() as conn:
            rows = conn.execute(
                select(admin_audit.c.actor_user_id, admin_audit.c.reason).where(
                    admin_audit.c.action == "maintenance.notice"
                )
            ).all()
        self.assertEqual(
            sorted(rows),
            [
                ("release-runner", "Release finished"),
                ("release-runner", "Release started"),
            ],
        )

    def test_malformed_row_reads_inactive_and_never_blocks_work(self):
        with self.repo.transaction() as conn:
            conn.execute(
                insert(documents).values(
                    tenant_id=self.settings.tenant_id,
                    owner_id=notice.OWNER,
                    project_id=notice.PROJECT,
                    kind=notice.KIND,
                    document_id=notice.ID,
                    payload={"active": True},
                    version=1,
                    updated_at=1.0,
                )
            )
            notice.require_open(conn, self.settings.tenant_id)
        self.assertEqual(self.config(), notice.INACTIVE)

    def test_refusal_shape_carries_expected_end(self):
        app = FastAPI()
        register_handlers(app, repository=None, settings=None)

        @app.post("/paid")
        def paid():
            raise notice.MaintenanceInProgress(4_000_000_000.0)

        reply = TestClient(app).post("/paid")
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(reply.json()["code"], "maintenance_in_progress")
        self.assertEqual(reply.json()["expected_end_at"], 4_000_000_000.0)
        self.assertEqual(reply.headers["retry-after"], "3600")


if __name__ == "__main__":
    unittest.main()
