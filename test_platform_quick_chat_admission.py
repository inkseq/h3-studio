"""Queue namespace and inert-plan recovery; local SQL only, no providers."""
import tempfile
import time
from pathlib import Path
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from studio_platform.api import create_app
from studio_platform.auth import Principal, API_SCOPES
from studio_platform.repository import Scope, Conflict, jobs, budget_reservations
from studio_platform.settings import Settings
from test_platform_api import project, generation_request
from test_platform_repository import LedgerCase


class QuickAdmissionTests(LedgerCase):
    def setUp(self):
        LedgerCase.setUp(self)
        self.now = time.time()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(Settings(Path(self.tmp.name), auth_mode="local-test", database_url=self.url,
                                      generation_enabled=True, execution_backend="mock"), repository=self.repo)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()
        self.client.post("/v1/projects", json={"project": project()}).raise_for_status()
        response = self.client.post("/v1/generation-plans", json=generation_request())
        response.raise_for_status()
        self.plan = response.json()
        self.repo = self.app.state.repository
        row = self.repo.get_document(Scope("sixnine", "superdan", "__projects"), "project", "story-one")
        payload = dict(row["payload"], integration_kind="quick_chat")
        self.repo.put_document(Scope("sixnine", "superdan", "__projects"), "project", "story-one",
                                payload, expected_version=row["version"])
        self.principal = Principal("superdan", "agent-one", True, (), tuple(API_SCOPES), True)
        # Managed admission must have its original session authority. A marker
        # without that binding is deliberately fail-closed after lifecycle
        # cleanup, rather than an alternate route around a trashed session.
        from studio_platform.quick_chat import DEFAULT_MODEL, default_next_settings
        with self.repo.transaction() as connection:
            self.app.state.quick_chat._new(connection, self.principal, "session-admission-fixture", "session",
                {"project_id": "story-one", "title": "Original admission fixture", "model_id": DEFAULT_MODEL,
                 "next_settings": default_next_settings(self.app.state.quick_chat.settings), "bindings": [],
                 "latest_seq": 0, "active_turn_id": None,
                 "title_generation": self.app.state.quick_chat._initial_title_state(title_supplied=True)},
                ident="session-admission-fixture")

    def test_different_agents_resolve_one_execution_and_cannot_bypass_legacy(self):
        admission = self.app.state.generation_admission
        a = admission.create_planned(self.principal, self.plan["plan_id"], "execution-one")
        other = Principal("superdan", "agent-two", True, (), tuple(API_SCOPES), True)
        b = admission.create_planned(other, self.plan["plan_id"], "execution-one")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(a["actor_id"], "quick-chat-execution")
        self.assertFalse(b["created"])
        for path, body in [
            ("/v1/generation-plans", generation_request()),
            ("/v1/projects/story-one/actions", {"expected_version": 2, "actions": []}),
            ("/v1/jobs", {"plan_id": self.plan["plan_id"]}),
            ("/v1/batches", {"client_project_id": "story-one", "plan_ids": [self.plan["plan_id"]]}),
        ]:
            r = self.client.post(path, json=body, headers={"Idempotency-Key": "legacy-bypass"})
            self.assertEqual(r.status_code, 409, r.text)
            self.assertIn("quick_chat_managed_resource", r.text)
        self.assertEqual(self.client.get("/v1/projects").json()["projects"], [])

    def test_actual_caller_permission_is_required_for_business_namespace(self):
        restricted = Principal("superdan", "no-write", True, (), ("projects:read",), True)
        with self.assertRaises(Exception) as raised:
            self.app.state.generation_admission.create_planned(restricted, self.plan["plan_id"], "execution-one")
        self.assertEqual(type(raised.exception).__name__, "NotFound")
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(jobs.c.id)).all(), [])

    def test_refresh_inert_plan_keeps_job_identity_and_has_no_reservation(self):
        admission = self.app.state.generation_admission
        before = admission.create_planned(self.principal, self.plan["plan_id"], "execution-one")
        scope = Scope("sixnine", "superdan", "story-one", "quick-chat-execution")
        old = self.app.state.owned_plan(self.principal, self.plan["plan_id"])
        fresh = self.repo.create_plan(scope, old["request"], old["execution_plan"],
            expires_at=self.repo.clock()+600, estimated_cost_microusd=old["estimated_cost_microusd"])
        refreshed = admission.refresh_planned(self.principal, before["id"], fresh["id"])
        self.assertEqual(refreshed["id"], before["id"])
        self.assertEqual(refreshed["idempotency_key"], before["idempotency_key"])
        self.assertEqual(refreshed["plan_id"], fresh["id"])
        self.assertEqual(refreshed["attempt_no"], 0)
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(budget_reservations.c.id)).all(), [])
        with self.repo.engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == before["id"]).values(attempt_no=1))
        with self.assertRaisesRegex(Conflict, "upstream_stop_unconfirmed"):
            admission.refresh_planned(self.principal, before["id"], fresh["id"])


if __name__ == "__main__":
    unittest.main()
