"""Advice isolation, stale-results, concurrency and no-read-side-effect contracts."""

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from sqlalchemy import select

from studio_platform.auth import Principal
from studio_platform.inky_source import sources
from studio_platform.inky_store import InkyStore
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
