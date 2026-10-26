"""Scoped review/CAS/privacy regressions; disposable ledgers, no provider calls."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from threading import Event
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import insert, select, update

from studio_platform.auth import Principal
from studio_platform.repository import (
    Conflict,
    NotFound,
    Scope,
    artifacts,
    attempts,
    documents,
    jobs,
)
from studio_platform.take_reviews import TakeReviews, TakeReviewPatch, register_routes
from test_platform_repository import LedgerCase
import test_review_loop as metrics_fixture
import test_universal_trash as purge_fixture


class TakeReviewTests(LedgerCase):
    take = metrics_fixture.ReviewLoopTests.take
    report = metrics_fixture.ReviewLoopTests.report

    def setUp(self):
        super().setUp()
        self.service = TakeReviews(self.repo, self.scope.tenant_id)
        self.principal = Principal(self.scope.owner_id, "browser")
        self.project_scope = Scope(
            self.scope.tenant_id, self.scope.owner_id, "__projects"
        )
        self.project = {
            "id": self.scope.project_id,
            "entities": [
                {"id": s, "type": "shot", "data": {}} for s in ("shot", "other-shot")
            ],
        }
        self.repo.put_document(
            self.project_scope, "project", self.scope.project_id, self.project
        )
        self.output = self.take()
        with self.repo.transaction() as conn:
            conn.execute(
                update(artifacts)
                .where(artifacts.c.id == self.output["artifact"])
                .values(metadata={"kind": "video", "duration_s": 5.0})
            )

    def mark(self, **changes):
        return {
            "id": "m1",
            "start_s": 1.0,
            "end_s": 2.0,
            "tags": ["camera"],
            "verdict": "change",
            "note": "PRIVATE REVIEW",
            **changes,
        }

    def read(self, **changes):
        args = dict(
            principal=self.principal,
            project_id=self.scope.project_id,
            shot_id="shot",
            artifact_id=self.output["artifact"],
        )
        return self.service.get(**{**args, **changes})

    def summaries(self, **changes):
        args = dict(
            principal=self.principal, project_id=self.scope.project_id, shot_id="shot"
        )
        return self.service.summaries(**{**args, **changes})

    def write(self, body=None, key="key-1", **changes):
        args = dict(
            principal=self.principal,
            project_id=self.scope.project_id,
            shot_id="shot",
            artifact_id=self.output["artifact"],
            body=body or {"expected_revision": 0, "verdict": "keep"},
            key=key,
        )
        return self.service.patch(**{**args, **changes})

    def test_partial_updates_retraction_replay_and_no_authoring_change(self):
        before = self.repo.get_document(
            self.project_scope, "project", self.scope.project_id
        )
        self.assertEqual(self.read()["revision"], 0)
        body = {"expected_revision": 0, "marks": [self.mark()]}
        first = self.write(body)
        self.assertIsNone(first["verdict"])
        self.assertNotIn("PRIVATE", json.dumps(first))
        second = self.write({"expected_revision": 1, "verdict": "keep"}, "key-2")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(self.read()["marks"], body["marks"])
        self.assertEqual(self.write(body), first)
        for invalid in (
            {"expected_revision": 0, "verdict": "redo"},
            {"expected_revision": 0, "marks": []},
        ):
            with self.assertRaises(Conflict):
                self.write(invalid)
        with self.assertRaisesRegex(Conflict, "revision_conflict"):
            self.write({"expected_revision": 0, "verdict": "redo"}, "fresh-key")
        self.write({"expected_revision": 2, "verdict": None}, "key-3")
        self.assertIsNone(self.read()["verdict"])
        self.write({"expected_revision": 3, "marks": []}, "key-4")
        self.assertEqual(self.read()["marks"], [])
        self.assertEqual(
            self.repo.get_document(
                self.project_scope, "project", self.scope.project_id
            ),
            before,
        )
        with self.repo.engine.connect() as conn:
            receipts = list(
                conn.execute(
                    select(documents.c.payload).where(
                        documents.c.kind == "take_review_receipt"
                    )
                ).scalars()
            )
        self.assertEqual(len(receipts), 4)
        self.assertNotIn("PRIVATE", json.dumps(receipts))
        self.assertNotIn("start_s", json.dumps(receipts))

    def test_range_bounds_and_closed_schema(self):
        invalid = [
            {"expected_revision": 0},
            {"expected_revision": True, "verdict": "keep"},
            {"expected_revision": 0, "actor_kind": "human", "verdict": "keep"},
            {"expected_revision": 0, "verdict": "change"},
            {"expected_revision": 0, "marks": None},
        ]
        bad_marks = [
            self.mark(start_s=-1),
            self.mark(end_s=1),
            self.mark(end_s=0),
            self.mark(start_s=float("nan")),
            self.mark(end_s=float("inf")),
            self.mark(start_s=True),
            self.mark(end_s="2"),
            self.mark(note="x" * 501),
            self.mark(tags=["unknown"]),
            self.mark(tags=["camera", "camera"]),
            self.mark(id="../unsafe"),
            self.mark(verdict="redo"),
        ]
        invalid += [{"expected_revision": 0, "marks": [m]} for m in bad_marks]
        invalid += [
            {"expected_revision": 0, "marks": [self.mark(), self.mark()]},
            {
                "expected_revision": 0,
                "marks": [self.mark(id=f"m-{i}") for i in range(51)],
            },
        ]
        for body in invalid:
            with self.subTest(body=str(body)[:100]), self.assertRaises(ValidationError):
                TakeReviewPatch.model_validate(body)
        with self.assertRaisesRegex(ValueError, "outside_duration"):
            self.write({"expected_revision": 0, "marks": [self.mark(end_s=5.01)]})
        self.assertEqual(self.read()["revision"], 0)
        self.write(
            {
                "expected_revision": 0,
                "marks": [
                    self.mark(start_s=0, end_s=5),
                    self.mark(id="overlap", start_s=2, end_s=3),
                ],
            }
        )
        self.assertEqual(len(self.read()["marks"]), 2)

    def test_unknown_duration_allows_verdict_not_ranges(self):
        with self.repo.transaction() as conn:
            conn.execute(
                update(artifacts)
                .where(artifacts.c.id == self.output["artifact"])
                .values(metadata={"kind": "video"})
            )
        self.write()
        with self.assertRaisesRegex(Conflict, "duration_unknown"):
            self.write({"expected_revision": 1, "marks": [self.mark()]}, "key-2")
        self.assertEqual(self.read()["revision"], 1)

    def test_scope_pat_and_selection_identity(self):
        for change in (
            {"principal": Principal("someone-else", "browser")},
            {"project_id": "other-project"},
            {"shot_id": "other-shot"},
        ):
            with self.subTest(change=change), self.assertRaises(NotFound):
                self.read(**change)
        with self.assertRaises(NotFound):
            TakeReviews(self.repo, "other-tenant").get(
                self.principal, self.scope.project_id, "shot", self.output["artifact"]
            )
        pat = Principal(
            self.scope.owner_id,
            "pat",
            machine=True,
            project_ids=(self.scope.project_id,),
            scopes=("projects:read",),
        )
        self.assertEqual(self.read(principal=pat)["revision"], 0)
        with self.assertRaises(NotFound):
            self.write(principal=pat)
        pat = replace(pat, scopes=("projects:read", "projects:write"))
        self.assertEqual(self.write(principal=pat)["actor_kind"], "agent")
        with self.assertRaises(NotFound):
            self.write(principal=replace(pat, project_ids=("different",)))
        with self.assertRaises(NotFound):
            self.write(
                principal=replace(self.principal, support_viewer="support-admin")
            )
        self.project["entities"][0]["data"].update(
            selectedAssetId="another", starredArtifactIds=["another"]
        )
        self.repo.put_document(
            self.project_scope,
            "project",
            self.scope.project_id,
            self.project,
            expected_version=1,
        )
        self.assertEqual(self.read()["verdict"], "keep")
        other = self.take(created=300, delivered=400)
        self.assertIsNone(self.read(artifact_id=other["artifact"])["verdict"])

    def test_rejected_attempt_and_forged_migration_are_not_reviewable(self):
        with self.repo.transaction() as conn:
            conn.execute(
                update(attempts)
                .where(attempts.c.id == self.output["attempt"])
                .values(status="attempt_lost")
            )
        with self.assertRaises(NotFound):
            self.write()
        with self.repo.transaction() as conn:
            conn.execute(
                update(attempts)
                .where(attempts.c.id == self.output["attempt"])
                .values(status="succeeded")
            )
            conn.execute(
                update(jobs)
                .where(jobs.c.id == self.output["job"])
                .values(result={"artifact_ids": []})
            )
        with self.assertRaises(NotFound):
            self.write()
        self.project["entities"][0]["data"]["migration"] = {
            "canonical": True,
            "source_session_id": "fake",
            "receipt_id": "fake",
        }
        self.repo.put_document(
            self.project_scope,
            "project",
            self.scope.project_id,
            self.project,
            expected_version=1,
        )
        with self.assertRaisesRegex(Conflict, "migration_binding_invalid"):
            self.read()

    def test_two_writers_one_wins_and_same_key_replays(self):
        def write(key):
            try:
                return self.write(key=key)
            except Conflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, ("key-a", "key-b")))
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(self.read()["revision"], 1)
        body = {"expected_revision": 1, "verdict": "redo"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.write(body, "same-key"), (1, 2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.read()["revision"], 2)

    def test_get_finishes_while_project_writer_holds_gate_and_row(self):
        from studio_platform.content_lifecycle import owner_reference_gate

        self.write()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.repo.transaction() as conn:
                owner_reference_gate(conn, self.scope.tenant_id, self.scope.owner_id)
                conn.execute(
                    update(documents)
                    .where(
                        *self.service._where(
                            self.project_scope, "project", self.scope.project_id
                        )
                    )
                    .values(version=documents.c.version + 1)
                )
                with (
                    patch.object(
                        self.repo, "_locked", side_effect=AssertionError("GET locked")
                    ),
                    patch(
                        "studio_platform.take_reviews.owner_reference_gate",
                        side_effect=AssertionError("GET gated"),
                    ),
                    patch(
                        "studio_platform.take_reviews.assert_project_writable",
                        side_effect=AssertionError("GET writable"),
                    ),
                ):
                    # PostgreSQL row/advisory locks and SQLite's writer reservation
                    # remain held until after this completed read.
                    result = pool.submit(self.read).result(timeout=3)
                    summaries = pool.submit(self.summaries).result(timeout=3)
                self.assertEqual(result["verdict"], "keep")
                self.assertEqual(result["revision"], 1)
                self.assertEqual(summaries[0]["verdict"], "keep")

    def test_project_trash_still_blocks_read_snapshot(self):
        from studio_platform.universal_trash import target_key

        target = {
            "kind": "project",
            "id": self.scope.project_id,
            "project_id": self.scope.project_id,
        }
        self.repo.put_document(
            Scope(self.scope.tenant_id, self.scope.owner_id, "__trash"),
            "trash_item",
            target_key(target),
            {
                "state": "trashed",
                "target": {
                    "kind": "project",
                    "id": self.scope.project_id,
                    "project_id": self.scope.project_id,
                },
            },
        )
        with self.assertRaisesRegex(Conflict, "project_in_trash"):
            self.read()
        with self.assertRaisesRegex(Conflict, "project_in_trash"):
            self.summaries()

    def test_summaries_defaults_order_scope_and_private_response(self):
        self.write({"expected_revision": 0, "verdict": "keep", "marks": [self.mark()]})
        later = self.take(created=300, delivered=400)
        self.take(shot="other-shot", created=350, delivered=450)
        self.take(
            scope=Scope("tenant-other", self.scope.owner_id, self.scope.project_id)
        )
        self.take(
            scope=Scope(self.scope.tenant_id, "owner-other", self.scope.project_id)
        )
        self.take(
            scope=Scope(self.scope.tenant_id, self.scope.owner_id, "project-other")
        )
        result = self.summaries()
        self.assertEqual(
            [r["artifact_id"] for r in result],
            [later["artifact"], self.output["artifact"]],
        )
        self.assertEqual(
            result[0],
            {
                "artifact_id": later["artifact"],
                "revision": 0,
                "verdict": None,
                "mark_count": 0,
                "actor_kind": None,
                "updated_at": None,
            },
        )
        self.assertEqual(result[1]["mark_count"], 1)
        self.assertEqual(result[1]["actor_kind"], "human")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("marks", json.dumps(result))
        for change in (
            {"principal": Principal("owner-other", "browser")},
            {"project_id": "project-other"},
            {
                "principal": replace(
                    self.principal,
                    machine=True,
                    scopes=(),
                    project_ids=(self.scope.project_id,),
                )
            },
        ):
            with self.subTest(change=change), self.assertRaises(NotFound):
                self.summaries(**change)
        pat = replace(
            self.principal,
            machine=True,
            scopes=("projects:read",),
            project_ids=(self.scope.project_id,),
        )
        self.assertEqual(self.summaries(principal=pat), result)

    def test_summaries_omit_unaccepted_lost_and_trashed_takes(self):
        rejected = self.take(created=300, delivered=400)
        lost = self.take(created=350, delivered=450)
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == rejected["job"])
                .values(result={"artifact_ids": []})
            )
            conn.execute(
                update(attempts)
                .where(attempts.c.id == lost["attempt"])
                .values(status="attempt_lost")
            )
        self.assertEqual(
            [r["artifact_id"] for r in self.summaries()], [self.output["artifact"]]
        )
        self.repo.put_document(
            Scope(self.scope.tenant_id, self.scope.owner_id, "__trash"),
            "trash_item",
            "artifact-trash",
            {
                "state": "purging",
                "target": {
                    "kind": "artifact",
                    "project_id": self.scope.project_id,
                    "id": self.output["artifact"],
                },
            },
        )
        self.assertEqual(self.summaries(), [])
        self.assertEqual(self.summaries(shot_id="other-shot"), [])

    def test_summary_overflow_refuses_instead_of_truncating(self):
        self.take(created=300, delivered=400)
        with patch("studio_platform.take_reviews.SUMMARY_LIMIT", 1):
            with self.assertRaisesRegex(Conflict, "take_review_summary_limit"):
                self.summaries()

    def test_trash_fence_blocks_even_original_idempotency_replay(self):
        self.write()
        self.repo.put_document(
            Scope(self.scope.tenant_id, self.scope.owner_id, "__trash"),
            "trash_item",
            "trash-1",
            {
                "state": "trashed",
                "target": {
                    "kind": "artifact",
                    "id": self.output["artifact"],
                    "project_id": self.scope.project_id,
                },
            },
        )
        for action in (self.read, self.write):
            with self.assertRaisesRegex(Conflict, "content_in_trash"):
                action()

    def test_http_model_header_and_no_store(self):
        app = FastAPI()
        app.state.repository, app.state.settings = (
            self.repo,
            SimpleNamespace(tenant_id=self.scope.tenant_id),
        )

        @app.middleware("http")
        async def principal(request: Request, next_call):
            request.state.principal = self.principal
            return await next_call(request)

        @app.exception_handler(Conflict)
        async def conflict(request, error):
            return JSONResponse({"error": str(error)}, status_code=409)

        register_routes(app)
        path = f"/v1/projects/{self.scope.project_id}/shots/shot/takes/{self.output['artifact']}/review"
        with TestClient(app) as client:
            self.assertEqual(client.get(path).headers["Cache-Control"], "no-store")
            body = {"expected_revision": 0, "verdict": "keep"}
            self.assertEqual(client.patch(path, json=body).status_code, 422)
            response = client.patch(
                path, json=body, headers={"Idempotency-Key": "http-key"}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            listed = client.get(
                f"/v1/projects/{self.scope.project_id}/shots/shot/take-reviews"
            )
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(listed.headers["Cache-Control"], "no-store")
            self.assertEqual(listed.json(), self.summaries())
            self.assertEqual(
                client.patch(
                    path, json=body, headers={"Idempotency-Key": "different-key"}
                ).status_code,
                409,
            )

    def test_inflight_write_cannot_cross_purge_intent(self):
        from studio_platform.content_lifecycle import owner_reference_gate

        started = Event()

        def write():
            started.set()
            return self.write()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.repo.transaction() as conn:
                owner_reference_gate(conn, self.scope.tenant_id, self.scope.owner_id)
                conn.execute(
                    insert(documents).values(
                        tenant_id=self.scope.tenant_id,
                        owner_id=self.scope.owner_id,
                        project_id="__trash",
                        kind="trash_item",
                        document_id="intent",
                        version=1,
                        updated_at=self.now,
                        payload={
                            "state": "purging",
                            "target": {
                                "kind": "artifact",
                                "id": self.output["artifact"],
                                "project_id": self.scope.project_id,
                            },
                        },
                    )
                )
                future = pool.submit(write)
                self.assertTrue(started.wait(5))
            with self.assertRaisesRegex(Conflict, "content_in_trash"):
                future.result(timeout=10)
        with self.repo.engine.connect() as conn:
            self.assertIsNone(
                conn.execute(
                    select(documents.c.document_id).where(
                        documents.c.kind == "take_review"
                    )
                ).first()
            )

    def test_metrics_later_take_keep_does_not_approve_first_and_bounds_are_honest(self):
        self.write({"expected_revision": 0, "verdict": "redo"})
        later = self.take(created=300, delivered=400)
        self.write(artifact_id=later["artifact"], key="later")
        result = self.report()
        self.assertEqual(result["metrics"]["first_usable_rate"]["value"], 0)
        self.assertEqual(result["coverage"]["human_usable_shots"], 1)
        with patch("studio_platform.review_loop.MAX_REVIEWS", 1):
            bounded = self.report()
        self.assertIsNone(bounded["metrics"]["first_usable_rate"]["value"])
        self.assertIn("reviews_limit", bounded["coverage"]["review_reasons"])
        self.assertEqual(bounded["metrics"]["first_take_shipped_rate"]["value"], 0)

    def test_metrics_only_explicit_whole_verdicts_with_revision_and_actor(self):
        self.write({"expected_revision": 0, "marks": [self.mark(verdict="keep")]})
        self.assertIsNone(self.report()["metrics"]["first_usable_rate"]["value"])
        self.now += 1
        self.write({"expected_revision": 1, "verdict": "keep"}, "key-2")
        at_keep = self.now
        self.assertEqual(self.report()["metrics"]["first_usable_rate"]["value"], 1)
        self.now += 1
        self.write({"expected_revision": 2, "verdict": "redo"}, "key-3")
        self.assertEqual(self.report()["metrics"]["first_usable_rate"]["value"], 0)
        later = self.now
        self.now = at_keep
        self.assertEqual(self.report()["metrics"]["first_usable_rate"]["value"], 1)
        self.now = later + 1
        self.write({"expected_revision": 3, "verdict": None}, "key-4")
        self.assertIsNone(self.report()["metrics"]["first_usable_rate"]["value"])
        pat = Principal(
            self.scope.owner_id,
            "agent",
            machine=True,
            all_projects=True,
            scopes=("projects:read", "projects:write"),
        )
        self.write({"expected_revision": 4, "verdict": "keep"}, "key-5", principal=pat)
        self.assertIsNone(self.report()["metrics"]["first_usable_rate"]["value"])
        self.assertEqual(
            self.report()["coverage"]["agent_reviewed_first_candidates"], 1
        )
        self.write({"expected_revision": 5, "verdict": "keep"}, "key-6")
        self.write({"expected_revision": 6, "marks": []}, "key-7", principal=pat)
        report = self.report()
        self.assertEqual(report["metrics"]["first_usable_rate"]["value"], 1)
        self.assertEqual(report["coverage"]["human_usable_shots"], 1)
        self.assertIsNone(
            report["metrics"]["full_cost_per_usable_shot_microusd"]["value"]
        )
        self.assertNotIn("PRIVATE", json.dumps(report))


class TakeReviewPurgeTests(LedgerCase):
    def setUp(self):
        from pathlib import Path
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        from studio_platform.guided import empty_project

        super().setUp()
        self.app = create_app(
            Settings(
                Path(self.temp.name) / "reviews",
                tenant_id=self.scope.tenant_id,
                auth_mode="local-test",
                database_url=self.url,
            ),
            repository=self.repo,
        )
        self.service = self.app.state.quick_chat
        self.trash = self.app.state.universal_trash
        self.principal = Principal(self.scope.owner_id, "browser")
        self.project = empty_project({"id": self.scope.project_id, "title": "Original"})
        self.repo.put_document(
            Scope(self.scope.tenant_id, self.scope.owner_id, "__projects"),
            "project",
            self.scope.project_id,
            self.project,
        )

    activate = purge_fixture.UniversalPurgeTests.activate
    body = purge_fixture.UniversalPurgeTests.body
    version = purge_fixture.UniversalPurgeTests.version
    purge = purge_fixture.UniversalPurgeTests.purge

    def test_original_purge_redacts_review_notes_preserves_receipts_and_foreign_rows(
        self,
    ):
        from pathlib import Path
        from studio_platform.artifact_writer import ArtifactWriter
        import test_creation_history_storage as storage_fixture

        self.activate()
        self.writer = ArtifactWriter(
            self.repo.engine,
            self.service.storage,
            Path(self.temp.name) / "collection",
            tenant=self.scope.tenant_id,
        )
        _, job, _, _, _, _ = storage_fixture.CreationHistoryStorageTests.prepared(
            self, settled=True
        )
        with self.repo.engine.connect() as conn:
            artifact = conn.execute(
                select(artifacts.c.id).where(artifacts.c.job_id == job["id"])
            ).scalar_one()
        review = {
            "artifact_id": artifact,
            "marks": [{"note": "PRIVATE PURGE NOTE"}],
            "verdict": "keep",
        }
        self.repo.put_document(self.scope, "take_review", artifact, review)
        feedback_scope = Scope(self.scope.tenant_id, self.scope.owner_id, "__feedback")
        self.repo.put_document(
            feedback_scope,
            "product_feedback",
            "report",
            {
                "body": {
                    "artifact_id": artifact,
                    "project_id": self.scope.project_id,
                    "summary": "PRIVATE PRODUCT REPORT",
                },
            },
        )
        self.repo.put_document(
            self.scope,
            "take_review_receipt",
            "receipt",
            {"artifact_id": artifact, "response": {"revision": 1, "verdict": "keep"}},
        )
        foreign = Scope("foreign-tenant", self.scope.owner_id, self.scope.project_id)
        # Deliberate ID collision in a disposable fixture: no media reference in
        # foreign payload, but the purge redactor must still not touch its row.
        self.repo.put_document(
            foreign, "take_review", artifact, {"marks": [{"note": "FOREIGN NOTE"}]}
        )
        moved = self.trash.apply(
            self.principal, self.body("artifact", artifact), "trash-reviewed-output"
        )
        pending, _ = self.purge(moved)
        self.service.process_history_cleanup()
        final = self.trash.get_action(self.principal, pending["operation_id"])
        self.assertEqual(final["state"], "completed", final)
        self.assertTrue(final["media_deleted"])
        self.assertEqual(
            self.repo.get_document(self.scope, "take_review", artifact)["payload"],
            {"content_deleted": True, "marks": [], "verdict": None},
        )
        self.assertEqual(
            self.repo.get_document(feedback_scope, "product_feedback", "report")[
                "payload"
            ],
            {"content_deleted": True},
        )
        self.assertEqual(
            self.repo.get_document(self.scope, "take_review_receipt", "receipt")[
                "payload"
            ]["response"]["verdict"],
            "keep",
        )
        self.assertIn(
            "FOREIGN NOTE",
            json.dumps(self.repo.get_document(foreign, "take_review", artifact)),
        )

    def test_project_purge_redacts_even_review_for_retained_shared_artifact(self):
        self.activate()
        feedback_scope = Scope(self.scope.tenant_id, self.scope.owner_id, "__feedback")
        self.repo.put_document(
            feedback_scope,
            "product_feedback",
            "report",
            {
                "body": {
                    "project_id": self.scope.project_id,
                    "summary": "PRIVATE PRODUCT REPORT",
                },
            },
        )
        self.repo.put_document(
            self.scope,
            "take_review",
            "kept-artifact",
            {"marks": [{"note": "PRIVATE PROJECT NOTE"}], "verdict": "keep"},
        )
        moved = self.trash.apply(
            self.principal, self.body("project", self.scope.project_id), "trash-project"
        )
        pending, _ = self.purge(moved)
        self.service.process_history_cleanup()
        self.assertEqual(
            self.trash.get_action(self.principal, pending["operation_id"])["state"],
            "completed",
        )
        self.assertNotIn(
            "PRIVATE",
            json.dumps(
                self.repo.get_document(self.scope, "take_review", "kept-artifact")
            ),
        )
        self.assertEqual(
            self.repo.get_document(feedback_scope, "product_feedback", "report")[
                "payload"
            ],
            {"content_deleted": True},
        )
