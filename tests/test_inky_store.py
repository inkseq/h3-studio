"""Advice isolation, stale-results, concurrency and no-read-side-effect contracts."""

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from sqlalchemy import select

from studio_platform.auth import Principal
from studio_platform.inky_source import sources
from studio_platform.inky_store import InkyStore
from studio_platform.inky_routes import register_routes
from studio_platform.repository import Conflict, NotFound, Scope, documents
from test_platform_repository import LedgerCase


class InkyStoreTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.store = InkyStore(self.repo, self.scope.tenant_id)
        self.principal = Principal(self.scope.owner_id, "browser")
        self.project_scope = Scope(
            self.scope.tenant_id, self.scope.owner_id, "__projects"
        )
        self.project = {
            "id": self.scope.project_id,
            "links": [],
            "entities": [
                {
                    "id": "scene",
                    "type": "scene",
                    "data": {"script": "雨夜车站", "cast": ["person"]},
                },
                {"id": "person", "type": "character", "title": "Lin", "data": {}},
                {"id": "ticket", "type": "prop", "title": "Ticket", "data": {}},
                {
                    "id": "shot",
                    "type": "shot",
                    "version": 1,
                    "parentId": "scene",
                    "data": {
                        "prompt": "Rain falls on the platform.",
                        "cast": [],
                        "storyboard": {"propIds": ["ticket"]},
                        "seconds": 5,
                    },
                },
            ],
        }
        self.save()

    def save(self):
        try:
            version = self.repo.get_document(
                self.project_scope, "project", self.scope.project_id
            )["version"]
        except NotFound:
            version = None
        self.repo.put_document(
            self.project_scope,
            "project",
            self.scope.project_id,
            self.project,
            expected_version=version,
        )

    def claim(self):
        return self.store.claim(self.principal, self.scope.project_id, "shot")

    def read(self, principal=None):
        return self.store.read(principal or self.principal, self.scope.project_id)

    def publish(self, source=None, kinds=("vague_action",)):
        return self.store.publish(
            self.principal,
            self.scope.project_id,
            source or self.claim(),
            [{"kind": k} for k in kinds],
        )

    def snapshot(self):
        with self.repo.engine.connect() as conn:
            return [dict(row) for row in conn.execute(select(documents)).mappings()]

    def test_minimal_source_cast_language_props_and_shared_context_freshness(self):
        source = self.claim()
        self.assertEqual(source["context"]["characters"], [])
        self.assertEqual(source["context"]["props"], ["Ticket"])
        self.assertEqual(source["context"]["lang"], "en")
        self.assertNotIn("storyboard", source["context"])
        self.publish(source)
        self.assertEqual(len(self.read()["items"]), 1)
        # Scene edits do not have to bump the shot version to hide old advice.
        self.project["entities"][0]["data"]["script"] = "雪天车站"
        self.save()
        self.assertEqual(self.read(), {"items": []})
        self.assertNotEqual(self.claim()["source_id"], source["source_id"])

    def test_get_is_pure_and_caps_open_items(self):
        self.publish(
            kinds=(
                "vague_action",
                "needs_keyframe",
                "emotion_not_shown",
                "action_too_long",
            )
        )
        before = copy.deepcopy(self.snapshot())
        self.assertEqual(len(self.read()["items"]), 3)
        self.assertEqual(self.snapshot(), before)

    def test_scope_and_support_view_do_not_leak_or_mutate(self):
        self.publish()
        for principal in (
            Principal("supervan", "browser"),
            replace(
                self.principal, machine=True, project_ids=(), scopes=("projects:read",)
            ),
            replace(self.principal, support_viewer="admin"),
        ):
            with self.subTest(principal=principal), self.assertRaises(NotFound):
                self.read(principal)
        other_tenant = InkyStore(self.repo, "other-tenant")
        with self.assertRaises(NotFound):
            other_tenant.read(self.principal, self.scope.project_id)

    def test_concurrent_claims_allow_one_and_unknown_never_retries(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(lambda _: self.claim(), range(10)))
        self.assertEqual(sum(v is not None for v in result), 1)
        self.assertIsNone(self.claim())
        self.assertEqual(self.read(), {"items": []})

    def test_late_results_and_double_publish_are_fenced(self):
        source = self.claim()
        self.project["entities"][-1]["data"]["prompt"] = "Snow falls."
        self.save()
        self.assertFalse(self.publish(source))
        fresh = self.claim()
        self.assertTrue(self.publish(fresh))
        self.assertFalse(self.publish(fresh, kinds=("needs_keyframe",)))
        self.assertEqual(self.read()["items"][0]["kind"], "vague_action")

    def test_ignore_is_idempotent_and_survives_new_version_without_story_edit(self):
        self.publish()
        ident = self.read()["items"][0]["id"]
        before = self.repo.get_document(
            self.project_scope, "project", self.scope.project_id
        )
        result = self.store.ignore(self.principal, self.scope.project_id, ident)
        self.assertEqual(result, {"id": ident, "ignored": True})
        self.assertEqual(self.read(), {"items": []})
        self.assertEqual(
            before,
            self.repo.get_document(
                self.project_scope, "project", self.scope.project_id
            ),
        )
        self.project["entities"][-1]["version"] += 1
        self.save()
        self.publish()
        self.assertEqual(self.read(), {"items": []})
        self.assertEqual(
            self.store.ignore(self.principal, self.scope.project_id, ident), result
        )
        with self.assertRaises(Conflict):
            self.store.ignore(self.principal, self.scope.project_id, "f" * 64)

    def test_tombstone_prevents_read_publish_and_claim(self):
        source = self.claim()
        self.project = {"id": self.scope.project_id, "content_deleted": True}
        self.save()
        for operation in (self.read, self.claim, lambda: self.publish(source)):
            with self.assertRaises(NotFound):
                operation()

    def test_invalid_context_is_not_fabricated(self):
        self.project["entities"][-1]["data"]["cast"] = ["missing"]
        self.assertEqual(sources(self.project), {})

    def client(self, principal=None):
        app = FastAPI()
        app.state.repository = self.repo
        app.state.settings = SimpleNamespace(tenant_id=self.scope.tenant_id)

        @app.middleware("http")
        async def auth(request: Request, call_next):
            request.state.principal = principal or self.principal
            return await call_next(request)

        @app.exception_handler(NotFound)
        async def missing(request, error):
            return JSONResponse({"detail": str(error)}, status_code=404)

        @app.exception_handler(Conflict)
        async def conflict(request, error):
            return JSONResponse({"detail": str(error)}, status_code=409)

        register_routes(app)
        return TestClient(app)

    def test_http_reader_ignore_and_read_only_principal(self):
        self.publish()
        before = self.snapshot()
        url = "/v1/inky/suggestions?project_id=" + self.scope.project_id
        with self.client() as client:
            result = client.get(url)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.headers["cache-control"], "no-store")
            self.assertEqual(before, self.snapshot())
            ident = result.json()["items"][0]["id"]
            path = f"/v1/inky/suggestions/{ident}/ignore?project_id={self.scope.project_id}"
            self.assertEqual(client.post(path).status_code, 200)
            self.assertEqual(client.post(path).status_code, 200)
            self.assertEqual(client.get(url).json(), {"items": []})
            self.assertEqual(client.get("/v1/inky/suggestions").status_code, 422)
        reader = replace(
            self.principal,
            machine=True,
            project_ids=(self.scope.project_id,),
            scopes=("projects:read",),
        )
        with self.client(reader) as client:
            self.assertEqual(client.get(url).status_code, 200)
            self.assertEqual(client.post(path).status_code, 404)

    def test_mute_cas_suppresses_views_and_evaluation_without_story_changes(self):
        self.publish()
        before = self.repo.get_document(
            self.project_scope, "project", self.scope.project_id
        )
        url = "/v1/inky/settings?project_id=" + self.scope.project_id
        with self.client() as client:
            self.assertEqual(client.get(url).json(), {"revision": 0, "muted": False})
            self.assertEqual(
                client.patch(
                    url, json={"expected_revision": 0, "muted": "true"}
                ).status_code,
                422,
            )
            self.assertEqual(
                client.patch(url, json={"expected_revision": 0, "muted": True}).json(),
                {"revision": 1, "muted": True},
            )
            self.assertEqual(
                client.patch(
                    url, json={"expected_revision": 0, "muted": False}
                ).status_code,
                409,
            )
        self.assertEqual(self.read(), {"items": []})
        self.assertEqual(
            before,
            self.repo.get_document(
                self.project_scope, "project", self.scope.project_id
            ),
        )
        self.project["entities"][-1]["data"]["prompt"] = "New action."
        self.save()
        self.assertIsNone(self.claim())

    def test_purge_redacts_example_but_preserves_text_free_receipts(self):
        from studio_platform.inky_store import redact_cache

        source = self.claim()
        self.store.publish(
            self.principal,
            self.scope.project_id,
            source,
            [{"kind": "vague_action", "example": "Rain wets the ticket."}],
        )
        with self.repo.transaction() as conn:
            redact_cache(
                conn,
                self.scope.tenant_id,
                self.scope.owner_id,
                (self.scope.project_id,),
                self.now,
            )
        self.assertEqual(self.read(), {"items": []})
        self.assertIsNone(self.claim())
        cache = [r for r in self.snapshot() if r["kind"] == "inky_cache"]
        self.assertEqual(cache[0]["payload"], {"content_deleted": True, "items": []})
