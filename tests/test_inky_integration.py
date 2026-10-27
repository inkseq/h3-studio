"""Real HTTP commit boundaries; no credentials, model calls or generation."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from studio_platform.auth import Principal
from studio_platform.inky_hooks import PENDING, handler, queue_refresh
from studio_platform.repository import Scope
from sqlalchemy import select
from studio_platform.repository import documents
import test_platform_api as fixtures
import test_universal_trash as trash_fixtures
import test_account_deletion as deletion_fixtures


class InkyIntegrationTests(unittest.TestCase):
    setUp = fixtures.ApiTests.setUp
    login = fixtures.ApiTests.login
    setup_project = fixtures.ApiTests.setup_project

    def attach(self):
        self.setup_project()
        worker = Mock()
        self.app.state.inky_worker = worker
        return worker

    def edit(self, version=1, key="edit"):
        return self.client.post(
            "/v1/projects/story-one/actions",
            headers={"Idempotency-Key": key},
            json={
                "expected_version": version,
                "actions": [
                    {
                        "op": "entity.update",
                        "entity_id": "shot-one",
                        "patch": {"description": "Rain falls on the platform."},
                    }
                ],
            },
        )

    def test_committed_edit_only_replay_conflict_and_full_save(self):
        worker = self.attach()
        versions = []

        def observe(principal, project_id):
            # A separate connection sees the committed version before advice.
            row = self.app.state.repository.get_document(
                Scope(self.settings.tenant_id, principal.owner, "__projects"),
                "project",
                project_id,
            )
            versions.append(row["version"])

        worker.refresh.side_effect = observe
        first = self.edit()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(versions, [2])
        self.assertEqual(self.edit().json(), first.json())
        self.assertEqual(self.edit(key="stale").status_code, 409)
        self.assertEqual(versions, [2])
        saved = self.client.put(
            "/v1/projects/story-one",
            json={
                "expected_version": 2,
                "project": first.json()["project"],
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(versions, [2, 3])

    def test_mounted_reads_and_preferences_never_evaluate(self):
        worker = self.attach()
        for path in (
            "/v1/inky/suggestions?project_id=story-one",
            "/v1/inky/settings?project_id=story-one",
            "/v1/projects/story-one",
            "/v1/guided-schema",
            "/v1/agent-guide",
        ):
            result = self.client.get(path)
            self.assertEqual(result.status_code, 200, result.text)
        schema = self.client.get("/v1/guided-schema").json()["inky"]
        self.assertEqual(schema, self.client.get("/v1/agent-guide").json()["inky"])
        self.assertEqual(schema["max_items"], 3)
        result = self.client.patch(
            "/v1/inky/settings?project_id=story-one",
            json={"expected_revision": 0, "muted": True},
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(
            self.client.patch(
                "/v1/inky/settings?project_id=story-one",
                json={"expected_revision": 0, "muted": False},
            ).status_code,
            409,
        )
        worker.refresh.assert_not_called()
        self.login("supervan")
        self.assertEqual(
            self.client.get("/v1/inky/suggestions?project_id=story-one").status_code,
            404,
        )
        self.client.cookies.clear()
        self.assertEqual(
            self.client.get("/v1/inky/suggestions?project_id=story-one").status_code,
            401,
        )
        worker.refresh.assert_not_called()

    def test_explicit_preflight_success_only(self):
        worker = self.attach()
        response = self.client.post(
            "/v1/generation-plans", json=fixtures.generation_request()
        )
        self.assertEqual(response.status_code, 201, response.text)
        worker.refresh.assert_called_once()
        failed = self.client.post(
            "/v1/generation-plans", json=fixtures.generation_request("missing")
        )
        self.assertEqual(failed.status_code, 404, failed.text)
        worker.refresh.assert_called_once()

    def test_optional_failure_preserves_save_and_hides_exception(self):
        worker = self.attach()
        worker.refresh.side_effect = RuntimeError("private creator text and credential")
        with self.assertLogs("studio_platform.inky_hooks", level="WARNING") as logs:
            result = self.edit()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(
            logs.output,
            ["WARNING:studio_platform.inky_hooks:inky_background_unavailable"],
        )


class InkyBoundaryTests(unittest.TestCase):
    def test_asgi_stream_background_preserved_dedup_and_context_reset(self):
        events = []
        principal = Principal("owner", "browser")
        worker = SimpleNamespace(refresh=lambda p, i: events.append(("advice", i)))
        app = SimpleNamespace(state=SimpleNamespace(inky_worker=worker))

        async def original(scope, receive, send):
            queue_refresh(principal, "project")
            queue_refresh(principal, "project")
            await send({"type": "http.response.start", "status": 200})
            await send(
                {"type": "http.response.body", "body": b"one", "more_body": True}
            )
            await send({"type": "http.response.body", "body": b"two"})
            events.append("original background")

        async def send(message):
            events.append(message)

        asyncio.run(handler(original, app)({"method": "POST"}, None, send))
        self.assertEqual(events[-2:], ["original background", ("advice", "project")])
        self.assertEqual(events[1]["body"], b"one")
        self.assertIsNone(PENDING.get())

    def test_failed_or_read_request_cannot_start_work(self):
        principal = Principal("owner", "browser")
        worker = Mock()
        app = SimpleNamespace(state=SimpleNamespace(inky_worker=worker))

        async def original(scope, receive, send):
            queue_refresh(principal, "project")
            await send({"type": "http.response.start", "status": scope["status"]})

        async def send(message):
            pass

        for method, status in (("POST", 409), ("POST", 500), ("GET", 200)):
            asyncio.run(
                handler(original, app)({"method": method, "status": status}, None, send)
            )
        worker.refresh.assert_not_called()
        self.assertIsNone(PENDING.get())


class InkyPurgeTests(trash_fixtures.UniversalPurgeTests):
    def test_proven_project_purge_erases_cached_text_only(self):
        self.activate()
        self.repo.put_document(
            Scope(self.scope.tenant_id, self.scope.owner_id, "__projects"),
            "project",
            self.scope.project_id,
            fixtures.project(self.scope.project_id),
            expected_version=1,
        )
        store = self.app.state.inky_store
        source = store.claim(self.principal, self.scope.project_id, "shot-one")
        store.publish(
            self.principal,
            self.scope.project_id,
            source,
            [{"kind": "vague_action", "example": "Rain wets the ticket."}],
        )
        with self.repo.engine.connect() as conn:
            claim = (
                conn.execute(
                    select(documents).where(documents.c.kind == "inky_evaluation")
                )
                .mappings()
                .one()
            )
        moved = self.trash.apply(
            self.principal, self.body("project", self.scope.project_id), "trash"
        )
        pending, _ = self.purge(moved)
        self.service.process_history_cleanup()
        final = self.trash.get_action(self.principal, pending["operation_id"])
        self.assertEqual(final["state"], "completed", final)
        with self.repo.engine.connect() as conn:
            cache = conn.scalar(
                select(documents.c.payload).where(documents.c.kind == "inky_cache")
            )
            after = (
                conn.execute(
                    select(documents).where(documents.c.kind == "inky_evaluation")
                )
                .mappings()
                .one()
            )
        self.assertEqual(cache, {"content_deleted": True, "items": []})
        self.assertEqual(dict(claim), dict(after))


class InkyAccountPurgeTests(deletion_fixtures.AccountDeletionTests):
    def test_existing_account_erasure_redacts_advice_and_fences_late_results(self):
        from studio_platform.repository import NotFound, Conflict
        from studio_platform.inky_budget import InkyBudget

        project_id = "advice-story"
        self.repo.put_document(
            Scope(self.scope.tenant_id, self.owner, "__projects"),
            "project",
            project_id,
            fixtures.project(project_id),
        )
        store = self.app.state.inky_store
        source = store.claim(self.principal, project_id, "shot-one")
        store.publish(
            self.principal,
            project_id,
            source,
            [{"kind": "vague_action", "example": "Rain wets the ticket."}],
        )
        budget = InkyBudget(self.repo)
        reserved = budget.reserve(
            tenant=self.scope.tenant_id,
            owner=self.owner,
            project=project_id,
            source_id=source["source_id"],
            max_cost_micros=250000,
        )
        self.assertTrue(reserved["allowed"])
        with self.repo.engine.connect() as conn:
            before = [
                dict(r)
                for r in conn.execute(
                    select(documents).where(documents.c.tenant_id == "__inky_global")
                ).mappings()
            ]
        self.assertTrue(before)
        original = self.confirm()
        self.assertTrue(self.finish(original["operation_id"])["done"])
        with self.repo.engine.connect() as conn:
            cache = conn.scalar(
                select(documents.c.payload).where(
                    documents.c.owner_id == self.owner, documents.c.kind == "inky_cache"
                )
            )
            after = [
                dict(r)
                for r in conn.execute(
                    select(documents).where(documents.c.tenant_id == "__inky_global")
                ).mappings()
            ]
        self.assertTrue(cache["content_deleted"])
        self.assertNotIn("Rain", str(cache))
        self.assertEqual(before, after)
        with self.assertRaises((NotFound, Conflict)):
            store.publish(
                self.principal, project_id, source, [{"kind": "vague_action"}]
            )


def load_tests(loader, tests, pattern):
    # Reuse disposable fixtures, not their unrelated inherited test inventory.
    return unittest.TestSuite(
        case(name)
        for case in (
            InkyIntegrationTests,
            InkyBoundaryTests,
            InkyPurgeTests,
            InkyAccountPurgeTests,
        )
        for name in case.__dict__
        if name.startswith("test_")
    )
