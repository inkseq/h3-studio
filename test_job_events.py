"""Original event history, authorization, pagination and closed public payload."""
import secrets
import uuid
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from test_platform_repository import LedgerCase
from studio_platform.api import create_app
from studio_platform.auth import Principal
from studio_platform.job_events import JobEvents
from studio_platform.repository import Scope, outbox
from studio_platform.settings import Settings


class JobEventTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.feed = JobEvents(self.repo, self.scope.tenant_id)
        self.owner = Principal("superdan", "browser:superdan")
        self.foreign = Principal("supervan", "browser:supervan")

    def test_pages_do_not_advance_over_another_owner_and_ack_does_not_hide(self):
        own1 = self.job()
        other = self.job(scope=self.other, cost=0)
        own2 = self.job()
        page1 = self.feed.read(self.owner, limit=1)
        self.assertTrue(page1["has_more"])
        self.assertEqual(page1["events"][0]["job_id"], own1["id"])
        self.repo.acknowledge_event(page1["events"][0]["event_id"])
        self.assertEqual(self.feed.read(self.owner, limit=1)["events"], page1["events"])
        page2 = self.feed.read(self.owner, since=page1["next_cursor"], limit=1)
        self.assertEqual(page2["events"][0]["job_id"], own2["id"])
        self.assertFalse(page2["has_more"])
        empty = self.feed.read(self.owner, since=page2["next_cursor"])
        self.assertEqual(empty["events"], [])
        self.assertEqual(empty["next_cursor"], page2["next_cursor"])
        self.assertEqual([v["job_id"] for v in self.feed.read(self.foreign)["events"]], [other["id"]])

    def test_foreign_cursor_and_revoked_pat_project_have_same_response(self):
        own = self.job()
        self.job(scope=self.other, cost=0)
        cursor = self.feed.read(self.foreign)["next_cursor"]
        allowed = Principal("superdan", "pat:read", True, (self.scope.project_id,), ("jobs:read",))
        scoped_out = Principal("superdan", "pat:elsewhere", True, ("another",), ("jobs:read",))
        own_cursor = self.feed.read(allowed)["next_cursor"]
        for principal, value in ((self.owner, cursor), (scoped_out, own_cursor),
                                  (self.owner, "je1." + str(uuid.uuid4())), (self.owner, "invalid")):
            with self.subTest(actor=principal.actor_id, cursor=value), self.assertRaises(HTTPException) as caught:
                self.feed.read(principal, since=value)
            self.assertEqual(caught.exception.status_code, 410)
            self.assertEqual(caught.exception.detail, "job_event_cursor_unavailable")
        self.assertEqual(self.feed.read(scoped_out)["events"], [])
        self.assertEqual([r["job_id"] for r in self.feed.read(allowed)["events"]], [own["id"]])
        with self.assertRaises(HTTPException) as caught:
            self.feed.read(Principal("superdan", "pat:write", True, (), ("jobs:write",), True))
        self.assertEqual(caught.exception.status_code, 403)

    def test_historical_status_and_closed_reason_no_raw_request_or_actor(self):
        original = self.job()
        self.repo.request_cancel(self.scope, original["id"])
        events = self.feed.read(self.owner)["events"]
        self.assertEqual(events[0]["status"], "queued")
        self.assertEqual(events[-1]["status"], "cancelled")
        self.assertEqual(events[-1]["cancelled_by"], "owner")
        self.assertTrue(events[-1]["retry_advice"]["safe_to_retry"])
        self.assertIs(events[-1]["retry_advice"]["charged"], False)
        text = str(events)
        for secret in ("prompt", "actor_user_id", "request_hash", "job_event_seq", "payload", "owner-budget"):
            self.assertNotIn(secret, text)

    def test_legacy_null_history_is_preserved_and_excluded(self):
        original = self.job()
        identity = str(uuid.uuid4())
        with self.repo.transaction() as connection:
            connection.execute(insert(outbox).values(id=identity, event_type="job.failed",
                aggregate_id=original["id"], payload={"private": "legacy"}, created_at=1.0))
        self.assertEqual(len(self.feed.read(self.owner)["events"]), 1)
        with self.repo.engine.connect() as connection:
            row = connection.execute(select(outbox).where(outbox.c.id == identity)).mappings().one()
            self.assertIsNone(row["job_event_seq"])
            self.assertEqual(row["payload"], {"private": "legacy"})

    def test_http_scope_bounds_and_public_static_discovery(self):
        original = self.job()
        app = create_app(Settings(Path(self.temp.name), tenant_id=self.scope.tenant_id,
            auth_mode="local-test", database_url=self.url), repository=self.repo)
        key = secrets.token_urlsafe(32)
        app.state.auth.register_client("read-events", key, "superdan", [self.scope.project_id], ["jobs:read"])
        with TestClient(app) as client:
            self.assertEqual(client.get("/v1/job-events").status_code, 401)
            headers = {"Authorization": "Bearer " + key}
            response = client.get("/v1/job-events", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.json()["events"][0]["job_id"], original["id"])
            self.assertEqual(client.get("/v1/job-events?limit=101", headers=headers).status_code, 422)
            guide = client.get("/for-agents/guide.json").json()
            self.assertEqual(guide["job_changes"]["path"], "/v1/job-events")
            self.assertIn("retry_of_execution_id", guide["job_changes"]["retry"]["body"])
