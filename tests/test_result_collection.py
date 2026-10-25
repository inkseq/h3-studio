"""Run in the normal SQLite and isolated PostgreSQL ledger CI matrices."""

import copy
import json
import uuid
from unittest.mock import patch

from sqlalchemy import func, insert, select, update

from studio_platform.guided import empty_project, new_entity
from studio_platform.project_validation import validate_project
from studio_platform.queue import TaskQueue
from studio_platform.repository import (
    Conflict,
    LeaseLost,
    Scope,
    artifacts,
    documents,
    jobs,
    outbox,
)
from studio_platform.result_collection import collect
from studio_platform.source_snapshot import source_snapshot
from test_platform_queue import VALID_ARTIFACT
from test_platform_repository import LedgerCase


class ResultCollectionTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.queue = TaskQueue(self.repo)
        self.doc_scope = Scope(self.scope.tenant_id, self.scope.owner_id, "__projects")
        self.project = empty_project({"id": "project-1", "title": "Test"})
        new_entity(self.project, {"id": "chapter", "type": "chapter", "title": "C"})
        new_entity(
            self.project,
            {"id": "scene", "type": "scene", "parentId": "chapter", "title": "S"},
        )
        new_entity(
            self.project,
            {"id": "shot", "type": "shot", "parentId": "scene", "title": "Take"},
        )
        validate_project(self.project)
        self.save(self.project)

    def save(self, value, expected=None):
        if expected is None:
            from studio_platform.repository import NotFound

            try:
                expected = self.document()["version"]
            except NotFound:
                expected = 0
        return self.repo.put_document(
            self.doc_scope, "project", "project-1", value, expected_version=expected
        )

    def document(self):
        return self.repo.get_document(self.doc_scope, "project", "project-1")

    def make_job(self, *, simulation=False, scope=None):
        value = self.document()["payload"]
        shot = next(e for e in value["entities"] if e["id"] == "shot")
        request = {
            "client_ref": {
                "project_id": "project-1",
                "shot_id": "shot",
                "shot_version": shot["version"],
            },
            "server_source_hash": source_snapshot(value, "shot"),
            "simulation": simulation,
        }
        plan = self.repo.create_plan(
            scope or self.scope,
            request,
            {"pool": "test-pool", "expected_runtime_s": 120},
            expires_at=self.now + 1000,
            estimated_cost_microusd=100_000,
        )
        return self.repo.create_job(
            scope or self.scope,
            plan["id"],
            uuid.uuid4().hex,
            budget_account_ids=("owner-budget",)
            if scope is None
            else ("other-budget",),
        )

    def collecting(self, job):
        claim = self.queue.claim("worker", "test-pool", job_ids=[job["id"]])
        self.queue.begin_submission(claim.lease)
        self.queue.record_submitted(claim.lease, "upstream")
        self.queue.begin_collection(claim.lease)
        return claim

    def complete(self, claim, specs=None):
        return self.queue.complete(
            claim.lease, specs or [VALID_ARTIFACT], actual_cost_microusd=80_000
        )

    def shot(self, project=None):
        return next(
            e
            for e in (project or self.document()["payload"])["entities"]
            if e["id"] == "shot"
        )

    def test_completed_result_is_bound_without_browser_and_selected_only_once(self):
        claim = self.collecting(self.make_job())
        before = self.document()
        done = self.complete(claim)
        receipt = done["result"]["project_collection"]
        self.assertEqual(receipt["status"], "bound")
        self.assertEqual(receipt["project_version"], before["version"] + 1)
        self.assertEqual(
            receipt["selected_entity_id"], self.shot()["data"]["selectedAssetId"]
        )
        self.assertEqual(self.shot()["data"]["candidateIds"], receipt["entity_ids"])
        project = self.document()["payload"]
        media = next(
            e for e in project["entities"] if e["id"] == receipt["entity_ids"][0]
        )
        self.assertEqual(
            media["data"]["cloudArtifactId"], done["result"]["artifact_ids"][0]
        )
        self.assertNotIn("object_key", json.dumps(project))
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 80_000)
        with self.assertRaises(LeaseLost):
            self.complete(claim)
        self.assertEqual(self.document()["payload"], project)
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 80_000)
        with self.assertRaises(Conflict):
            self.save(before["payload"], before["version"])

    def test_later_edit_preserved_and_stale_result_not_selected(self):
        claim = self.collecting(self.make_job())
        changed = self.document()["payload"]
        self.shot(changed)["data"]["prompt"] = "new edit"
        self.shot(changed)["version"] += 1
        changed["title"] = "Preserve"
        self.save(changed)
        self.complete(claim)
        current = self.document()["payload"]
        self.assertEqual(current["title"], "Preserve")
        self.assertEqual(self.shot()["data"]["prompt"], "new edit")
        self.assertFalse(self.shot()["data"].get("selectedAssetId"))
        self.assertEqual(len(self.shot()["data"]["candidateIds"]), 1)

    def test_existing_selection_and_trim_are_not_replaced(self):
        value = self.document()["payload"]
        new_entity(
            value,
            {
                "id": "chosen",
                "type": "video",
                "data": {"fileId": "file", "duration": 5},
            },
        )
        self.shot(value)["data"].update(
            selectedAssetId="chosen",
            selectedVideoRange={
                "assetId": "chosen",
                "fileId": "file",
                "cloudAssetId": None,
                "cloudArtifactId": None,
                "start": 0,
                "end": 2,
            },
        )
        self.save(value)
        self.complete(self.collecting(self.make_job()))
        data = self.shot()["data"]
        self.assertEqual(data["selectedAssetId"], "chosen")
        self.assertEqual(
            data["selectedVideoRange"], self.shot(value)["data"]["selectedVideoRange"]
        )

    def test_newer_job_prevents_old_completion_selection(self):
        first = self.make_job()
        self.now += 1
        self.make_job()
        self.complete(self.collecting(first))
        self.assertFalse(self.shot()["data"].get("selectedAssetId"))

    def test_simulation_candidate_never_selected(self):
        self.complete(self.collecting(self.make_job(simulation=True)))
        self.assertFalse(self.shot()["data"].get("selectedAssetId"))
        self.assertEqual(len(self.shot()["data"]["candidateIds"]), 1)

    def test_completion_after_cancel_retains_result_without_auto_select(self):
        claim = self.collecting(self.make_job())
        self.repo.request_cancel(self.scope, claim.job["id"])
        done = self.complete(claim)
        self.assertTrue(done["result"]["completed_after_cancel_request"])
        self.assertFalse(self.shot()["data"].get("selectedAssetId"))

    def test_segments_and_soundtrack_survive_completion(self):
        from studio_platform.shot_segments import source_binding

        value = self.document()["payload"]
        video = new_entity(
            value,
            {
                "id": "chosen",
                "type": "video",
                "data": {"fileId": "file", "duration": 5},
            },
        )
        data = self.shot(value)["data"]
        data.update(
            selectedAssetId="chosen",
            videoSegments=[{"start": 0, "end": 1}, {"start": 2, "end": 3}],
            videoSegmentsSource=source_binding(video),
        )
        self.save(value)
        self.complete(self.collecting(self.make_job()))
        for key in ("selectedAssetId", "videoSegments", "videoSegmentsSource"):
            self.assertEqual(self.shot()["data"][key], data[key])

    def test_two_concurrent_completions_keep_both_candidates(self):
        first = self.make_job()
        self.now += 1
        second = self.make_job()
        claims = [self.collecting(first), self.collecting(second)]
        results = self.parallel(lambda index: self.complete(claims[index]), 2)
        expected = {
            ident
            for done in results
            for ident in done["result"]["project_collection"]["entity_ids"]
        }
        self.assertEqual(set(self.shot()["data"]["candidateIds"]), expected)
        self.assertEqual(len(expected), 2)

    def test_full_document_refuses_projection_but_finishes_billing_once(self):
        # Actual byte quota, not a mocked validator. Each string stays within
        # the existing per-string bound, and the original document is valid.
        value = self.document()["payload"]
        value["padding"] = ["x" * 200000 for _ in range(40)]
        target = 8 * 1024 * 1024 - 300
        while len(json.dumps(value, ensure_ascii=False).encode()) < target:
            remaining = target - len(json.dumps(value, ensure_ascii=False).encode())
            value["padding"].append("x" * min(199996, max(0, remaining - 4)))
        validate_project(value)
        self.save(value)
        done = self.complete(self.collecting(self.make_job()))
        self.assertEqual(
            done["result"]["project_collection"]["status"], "document_rejected"
        )
        self.assertEqual(self.document()["payload"], value)
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 80_000)

    def test_deleted_shot_is_not_resurrected(self):
        claim = self.collecting(self.make_job())
        value = self.document()["payload"]
        value["entities"] = [e for e in value["entities"] if e["id"] != "shot"]
        self.save(value)
        before = self.document()
        done = self.complete(claim)
        self.assertEqual(done["result"]["project_collection"]["status"], "missing_shot")
        self.assertEqual(self.document(), before)
        self.assertEqual(done["status"], "succeeded")

    def test_foreign_owner_same_project_id_never_modified(self):
        self.repo.configure_budget(
            "other-budget",
            tenant_id=self.other.tenant_id,
            owner_id=self.other.owner_id,
            limit_microusd=10_000_000,
        )
        before = self.document()
        done = self.complete(self.collecting(self.make_job(scope=self.other)))
        self.assertEqual(
            done["result"]["project_collection"]["status"], "missing_project"
        )
        self.assertEqual(self.document(), before)

    def test_managed_projection_keeps_immutable_source_hash(self):
        claim = self.collecting(self.make_job())
        with self.repo.transaction() as conn:
            value = copy.deepcopy(self.project)
            value["integration_kind"] = "quick_chat"
            conn.execute(
                update(documents)
                .where(documents.c.document_id == "project-1")
                .values(payload=value)
            )
        before = self.document()
        done = self.complete(claim)
        self.assertEqual(
            done["result"]["project_collection"]["status"], "managed_projection"
        )
        self.assertEqual(self.document(), before)

    def test_audio_is_attached_but_not_a_shot_choice_or_automatic_soundtrack(self):
        done = self.complete(
            self.collecting(self.make_job()),
            [{**VALID_ARTIFACT, "kind": "audio", "content_type": "audio/flac"}],
        )
        self.assertEqual(done["result"]["project_collection"]["status"], "bound")
        self.assertFalse(self.shot()["data"].get("selectedAssetId"))
        self.assertFalse(self.shot()["data"].get("candidateIds"))
        self.assertNotIn("soundTracks", self.document()["payload"]["journey"])

    def test_failure_after_projection_rolls_back_artifact_document_and_settlement(self):
        claim = self.collecting(self.make_job())
        before = self.document()
        original = self.repo._emit

        def fail(conn, event, ident, payload):
            if event == "job.succeeded":
                raise RuntimeError("test commit failure")
            return original(conn, event, ident, payload)

        with (
            patch.object(self.repo, "_emit", side_effect=fail),
            self.assertRaises(RuntimeError),
        ):
            self.complete(claim)
        self.assertEqual(self.document(), before)
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 0)
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                conn.scalar(select(func.count()).select_from(artifacts)), 0
            )
            self.assertEqual(
                conn.scalar(
                    select(func.count())
                    .select_from(outbox)
                    .where(outbox.c.event_type == "project.results_collected")
                ),
                0,
            )
        self.assertEqual(self.complete(claim)["status"], "succeeded")

    def test_document_limit_refusal_does_not_lose_completed_artifact(self):
        claim = self.collecting(self.make_job())
        before = self.document()
        with patch(
            "studio_platform.result_collection.validate_project",
            side_effect=[None, ValueError("quota")],
        ):
            done = self.complete(claim)
        self.assertEqual(
            done["result"]["project_collection"]["status"], "document_rejected"
        )
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(len(done["result"]["artifact_ids"]), 1)
        self.assertEqual(self.document(), before)

    def test_projection_retry_is_idempotent_and_keeps_custom_adopted_identity(self):
        job = self.make_job()
        ident = str(uuid.uuid4())
        value = self.document()["payload"]
        new_entity(
            value, {"id": "custom", "type": "video", "data": {"cloudArtifactId": ident}}
        )
        self.save(value)
        with self.repo.transaction() as conn:
            first = collect(self.repo, conn, job, [ident], [VALID_ARTIFACT])
        self.assertEqual(first["entity_ids"], ["custom"])
        before = self.document()
        with self.repo.transaction() as conn:
            second = collect(self.repo, conn, job, [ident], [VALID_ARTIFACT])
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(self.document(), before)

    def test_trashed_project_skips_without_resurrection(self):
        claim = self.collecting(self.make_job())
        before = self.document()
        with patch(
            "studio_platform.result_collection.assert_project_writable",
            side_effect=Conflict("project_in_trash"),
        ):
            done = self.complete(claim)
        self.assertEqual(
            done["result"]["project_collection"]["status"], "project_unavailable"
        )
        self.assertEqual(self.document(), before)

    def test_guided_schema_advertises_read_only_browser_refresh(self):
        from studio_platform.guided_schema import contract

        value = contract()["result_collection"]
        self.assertEqual(value["version"], 1)
        self.assertIn("Do not PUT", value["read"])

    def test_frozen_film_generation_never_mutates_live_story(self):
        from sqlalchemy import update
        from studio_platform.repository import jobs

        job = self.make_job()
        request = {
            **job["request"],
            "film_source_ref": {"run_id": "film-original", "snapshot_hash": "a" * 64},
        }
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(request=request)
            )
        before = self.document()
        done = self.complete(self.collecting(job))
        self.assertEqual(
            done["result"]["project_collection"]["status"], "not_applicable"
        )
        self.assertEqual(self.document(), before)
