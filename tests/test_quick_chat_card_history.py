"""Card-level history on isolated local/CI databases; no inference or providers."""
import base64
import copy
import json
import secrets
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from studio_platform.auth import Principal
from studio_platform.api import create_app
from studio_platform.quick_chat import QuickChatError, cursor_encode, objects
from studio_platform.repository import NotFound
from studio_platform.settings import Settings
import test_platform_quick_chat as fixture
import test_platform_repository as ledger


class CardHistoryTests(ledger.LedgerCase):
    def setUp(self):
        super().setUp()
        self.settings = Settings(Path(self.temp.name)/"card-history", tenant_id=self.scope.tenant_id,
            auth_mode="local-test", generation_enabled=True, execution_backend="mock", database_url=self.url)
        self.app = create_app(self.settings, repository=self.repo)
        self.service = self.app.state.quick_chat
        self.principal = Principal("superdan", "browser")
        self.session = self.service.create_session(self.principal, {}, "session-create")["session"]
        self.sid = self.session["id"]

    card = fixture.QuickChatTests.card

    def test_event_heavy_66_cards_page_by_original_creation_with_ties_and_later_edits(self):
        # Fixed clock forces real ties, resolved by immutable card ID.
        self.repo.clock = lambda: 1000.0
        cards = [self.card(key="card-history-"+str(i))["card"] for i in range(66)]
        ordered = sorted(cards, key=lambda value: (value["created_at"], value["id"]))
        with self.repo.transaction() as connection:
            session = self.service._get(connection, self.principal, self.sid, self.sid, "session", lock=True)
            for _ in range(200):
                self.service._event(connection, self.principal, session, "session.updated", self.sid)
        timeline = self.service.timeline(self.principal, self.sid, limit=30)
        self.assertTrue(all(not e["type"].startswith("card.") for e in timeline["events"]))
        page = self.service.list_cards(self.principal, self.sid)
        self.assertEqual([c["id"] for c in page["cards"]], [c["id"] for c in ordered[-10:]])
        self.assertTrue(page["has_more"])
        self.assertFalse(page["start_reached"])
        self.assertEqual(page["session_id"], self.sid)
        # Updates do not move an older card; a newly appended card does not
        # shift the keyset boundary or enter already-requested older pages.
        self.repo.clock = lambda: 2000.0
        oldest = ordered[0]
        revision = self.service.get_revision(self.principal, self.sid, oldest["current_revision_id"])
        body = {k: copy.deepcopy(revision[k]) for k in ("recipe_id", "prompt", "controls", "inputs", "copies")}
        body["expected_card_version"] = oldest["version"]
        self.service.save_card(self.principal, self.sid, body, "edit-oldest", card_id=oldest["id"])
        later = self.card(key="new-after-first-page")["card"]
        seen = [c["id"] for c in page["cards"]]
        sizes = [len(page["cards"])]
        while page["next_cursor"]:
            page = self.service.list_cards(self.principal, self.sid, before_cursor=page["next_cursor"])
            self.assertGreater(len(page["cards"]), 0)
            sizes.append(len(page["cards"]))
            seen = [c["id"] for c in page["cards"]] + seen
        self.assertEqual(sizes, [10, 10, 10, 10, 10, 10, 6])
        self.assertEqual(seen, [c["id"] for c in ordered])
        self.assertNotIn(later["id"], seen)
        self.assertTrue(page["start_reached"])
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_cursor"])
        newest = self.service.list_cards(self.principal, self.sid)
        self.assertEqual(newest["cards"][-1]["id"], later["id"])

    def test_cursor_rejects_other_owner_session_tenant_namespace_and_invalid_boundaries(self):
        self.assertEqual(self.service.list_cards(self.principal, self.sid), {
            "session_id": self.sid, "cards": [], "limit": 10, "has_more": False,
            "next_cursor": None, "start_reached": True})
        self.card(key="one"); self.card(key="two")
        page = self.service.list_cards(self.principal, self.sid, limit=1)
        other = self.service.create_session(self.principal, {}, "other-session")["session"]
        with self.assertRaises(QuickChatError):
            self.service.list_cards(self.principal, other["id"], before_cursor=page["next_cursor"])
        original = json.loads(base64.urlsafe_b64decode(page["next_cursor"]+"="*((-len(page["next_cursor"])) % 4)))
        bad = ["", "not-base64", "x"*1025,
               cursor_encode(self.principal, self.sid, 1, "older")]
        for key, value in (("owner", "supervan"), ("direction", "newer")):
            changed = copy.deepcopy(original); changed[key] = value
            bad.append(base64.urlsafe_b64encode(json.dumps(changed).encode()).decode())
        for boundary in ({}, {"tenant": "other", "created_at": 1000, "id": "card-id"},
                         {"tenant": self.service.tenant, "created_at": True, "id": "card-id"},
                         {"tenant": self.service.tenant, "created_at": float("nan"), "id": "card-id"},
                         {"tenant": self.service.tenant, "created_at": float("inf"), "id": "card-id"},
                         {"tenant": self.service.tenant, "created_at": 1000, "id": "bad/id"}):
            bad.append(cursor_encode(self.principal, self.sid, boundary, "cards_older"))
        for cursor in bad:
            with self.subTest(cursor=cursor), self.assertRaises(QuickChatError) as error:
                self.service.list_cards(self.principal, self.sid, before_cursor=cursor)
            self.assertEqual(error.exception.code, "invalid_cursor")
        for limit in (True, 0, 51, "10"):
            with self.assertRaises(QuickChatError):
                self.service.list_cards(self.principal, self.sid, limit=limit)

    def test_read_authority_and_redacted_cards_preserve_existing_session_trash_rules(self):
        cards = [self.card(key="redacted-"+str(i))["card"] for i in range(3)]
        with self.repo.transaction() as connection:
            connection.execute(update(objects).where(objects.c.id == cards[1]["id"]).values(payload={"content_deleted": True}))
            original = dict(connection.execute(select(objects).where(objects.c.id == cards[0]["id"])).mappings().one())
            for ident, tenant, owner in (("card-foreign-owner", self.service.tenant, "supervan"),
                                         ("card-foreign-tenant", "other-tenant", "superdan")):
                connection.execute(insert(objects).values(**{**original, "id": ident, "tenant": tenant,
                    "owner": owner, "created_at": original["created_at"]+1,
                    "payload": {**original["payload"], "title": "Must never appear"}}))
        with self.repo.engine.connect() as connection:
            before = [dict(r) for r in connection.execute(select(objects)).mappings()]
        page = self.service.list_cards(self.principal, self.sid)
        self.assertEqual({c["id"] for c in page["cards"]}, {cards[0]["id"], cards[2]["id"]})
        project = self.service._access(self.principal, self.sid)["payload"]["project_id"]
        for principal in (Principal("supervan", "other"),
                          Principal("superdan", "denied", True, ("other-project",), ("projects:read",)),
                          Principal("superdan", "no-scope", True, (project,), ("jobs:read",))):
            with self.assertRaises(NotFound):
                self.service.list_cards(principal, self.sid)
        read_only = Principal("superdan", "read", True, (project,), ("projects:read",))
        narrowed = self.service.list_cards(read_only, self.sid)
        self.assertEqual([c["id"] for c in narrowed["cards"]], [c["id"] for c in page["cards"]])
        self.assertTrue(all(c["submission_ids"] is None and c["submissions_require_scope"] == "jobs:read" for c in narrowed["cards"]))
        with self.repo.engine.connect() as connection:
            self.assertEqual([dict(r) for r in connection.execute(select(objects)).mappings()], before)
        with self.repo.transaction() as connection:
            session = self.service._get(connection, self.principal, self.sid, self.sid, "session")
            payload = copy.deepcopy(session["payload"])
            payload["history"] = {"trash_state": "trashed"}
            connection.execute(update(objects).where(objects.c.id == self.sid).values(payload=payload))
        self.assertEqual(self.service.list_cards(read_only, self.sid)["cards"], narrowed["cards"])
        with self.repo.transaction() as connection:
            payload["history"] = {"trash_state": "purged"}
            connection.execute(update(objects).where(objects.c.id == self.sid).values(payload=payload))
        with self.assertRaises(QuickChatError) as error:
            self.service.list_cards(read_only, self.sid)
        self.assertEqual((error.exception.code, error.exception.status), ("session_purged", 410))

    def test_http_route_supports_existing_cookie_and_scoped_agent_read(self):
        card = self.card()["card"]
        project = self.service._access(self.principal, self.sid)["payload"]["project_id"]
        path = "/v1/quick-chat/sessions/"+self.sid+"/cards"
        with TestClient(self.app) as client:
            self.assertEqual(client.get(path).status_code, 401)
            client.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()
            result = client.get(path)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["cards"], [{**card, "submission_ids": []}])
            for params in ({"limit": 0}, {"limit": 51}, {"before_cursor": "bad"}):
                self.assertEqual(client.get(path, params=params).status_code, 422)
            token = secrets.token_urlsafe(32)
            self.app.state.auth.register_client("history-read", token, "superdan", [project], ["projects:read"])
            headers = {"Authorization": "Bearer "+token}
            client.post("/api/auth/login", json={"username": "supervan"}).raise_for_status()
            self.assertEqual(client.get(path).status_code, 404)
            self.assertEqual(client.get(path, headers=headers).json()["cards"],
                [{**card, "submission_ids": None, "submissions_require_scope": "jobs:read"}])

    def test_page_discovers_original_submissions_without_relying_on_timeline_events(self):
        value = self.card()
        revision = value["revision"]
        preflight = fixture.QuickChatTests.preflight(self, revision)
        submission = fixture.QuickChatTests.submit(self, revision, preflight)
        page = self.service.list_cards(self.principal, self.sid)
        self.assertEqual(page["cards"][0]["submission_ids"], [submission["id"]])
        self.assertEqual(self.service.get_submission(self.principal, self.sid,
            page["cards"][0]["submission_ids"][0])["revision_id"], revision["id"])
