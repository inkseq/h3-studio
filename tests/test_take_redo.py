"""Redo previews on disposable ledgers; no models, preflight or mutation."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update

from studio_platform.auth import Principal
from studio_platform.repository import (
    Conflict,
    NotFound,
    Scope,
    artifacts,
    attempts,
    documents,
    jobs,
    plans,
)
from studio_platform.take_reviews import TakeReviews
from studio_platform.take_redo import (
    TakeRedo,
    RedoPreview,
    frozen_recipe,
    prompt_diff,
    register_routes,
    PATH,
)
from test_platform_repository import LedgerCase
import test_review_loop as fixture


class RedoTests(LedgerCase):
    take = fixture.ReviewLoopTests.take

    def setUp(self):
        super().setUp()
        self.principal = Principal(self.scope.owner_id, "browser")
        self.project_scope = Scope(
            self.scope.tenant_id, self.scope.owner_id, "__projects"
        )
        self.repo.put_document(
            self.project_scope,
            "project",
            self.scope.project_id,
            {
                "id": self.scope.project_id,
                "entities": [{"id": "shot", "type": "shot", "data": {}}],
            },
        )
        self.output = self.take()
        self.recipe = {
            "recipe_id": "h3-fl-v1",
            "prompt": "人物🙂走向门口，缓慢推镜。",
            "controls": {"seed": "42", "duration": 5, "steps": 5},
            "inputs": {
                "images": [],
                "videos": [],
                "audios": [],
                "first_frame": {"asset_id": "original-reference"},
            },
        }
        self.stored = {
            "client_ref": {"project_id": self.scope.project_id, "shot_id": "shot"},
            "authoring_recipe": self.recipe,
            "request": {"mode": "fl", "seed": 42, "prompt": self.recipe["prompt"]},
            "authoring_context": {"private": "DO NOT RETURN CONTEXT"},
            "worker_secret": "DO NOT RETURN JOB",
        }
        self.store()
        with self.repo.transaction() as conn:
            conn.execute(
                update(artifacts)
                .where(artifacts.c.id == self.output["artifact"])
                .values(metadata={"kind": "video", "duration_s": 5})
            )
        self.reviews = TakeReviews(self.repo, self.scope.tenant_id)
        self.redo = TakeRedo(self.repo, self.scope.tenant_id)

    def store(self):
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == self.output["job"])
                .values(request=copy.deepcopy(self.stored))
            )

    def mark(self, **changes):
        return {
            "id": "change",
            "start_s": 1,
            "end_s": 3,
            "tags": ["camera"],
            "verdict": "change",
            "note": "Move camera more slowly",
            **changes,
        }

    def write(self, marks=None):
        return self.reviews.patch(
            self.principal,
            self.scope.project_id,
            "shot",
            self.output["artifact"],
            {"expected_revision": 0, "marks": marks or [self.mark()]},
            "review",
        )

    def edit(self, text="缓慢", replacement="轻柔", **changes):
        start = self.recipe["prompt"].index(text)
        return {
            "mark_id": "change",
            "start": start,
            "end": start + len(text),
            "replacement": replacement,
            **changes,
        }

    def preview(self, *, revision=1, version=1, edits=None, principal=None, **changes):
        return self.redo.preview(
            principal or self.principal,
            self.scope.project_id,
            "shot",
            self.output["artifact"],
            {
                "expected_project_version": version,
                "expected_review_revision": revision,
                "edits": [] if edits is None else edits,
                **changes,
            },
        )

    def snapshot(self):
        with self.repo.engine.connect() as conn:
            return {
                table.name: [
                    dict(row) for row in conn.execute(select(table)).mappings()
                ]
                for table in (jobs, attempts, artifacts, documents, plans)
            }

    def test_baseline_unicode_diff_preserves_original_recipe_and_database(self):
        self.write(
            [self.mark(id="keep", start_s=0, end_s=1, verdict="keep"), self.mark()]
        )
        before = self.snapshot()
        empty = self.preview()
        self.assertEqual(empty["status"], "needs_edits")
        self.assertEqual(empty["baseline"], empty["candidate"])
        edit = self.edit()
        value = self.preview(edits=[edit])
        self.assertEqual(value, self.preview(edits=[edit]))
        self.assertEqual(value["status"], "preview")
        self.assertEqual(
            value["candidate"]["prompt"], self.recipe["prompt"].replace("缓慢", "轻柔")
        )
        self.assertEqual(value["preserved"]["seed"], "42")
        self.assertFalse(value["applied"])
        self.assertFalse(value["executable"])
        self.assertEqual(value["pixel_preservation"], "not_guaranteed")
        self.assertEqual(value["preflight_status"], "not_checked")
        self.assertEqual(value["diff"][0]["before"], "缓慢")
        self.assertEqual(value["diff"][0]["start"], edit["start"])
        self.assertNotIn("DO NOT RETURN", json.dumps(value))
        baseline = frozen_recipe(self.stored)
        candidate, _ = prompt_diff(
            baseline,
            value["marks"],
            RedoPreview(
                expected_project_version=1, expected_review_revision=1, edits=[edit]
            ).edits,
        )
        self.assertEqual(
            {k: v for k, v in candidate.items() if k != "prompt"},
            {k: v for k, v in baseline.items() if k != "prompt"},
        )
        self.assertEqual(self.snapshot(), before)

    def test_multiple_edits_use_original_offsets_and_do_not_silently_apply_notes(self):
        self.write()
        edits = [self.edit(), self.edit("人物", "一个演员")]
        value = self.preview(edits=edits)
        self.assertEqual(value["candidate"]["prompt"], "一个演员🙂走向门口，轻柔推镜。")
        self.assertEqual([x["before"] for x in value["diff"]], ["人物", "缓慢"])
        self.assertNotIn("Move camera", value["candidate"]["prompt"])

    def test_mark_boundaries_unknown_suggestions_and_overlaps_refuse_atomically(self):
        self.write(
            [self.mark(id="keep", start_s=0, end_s=2, verdict="keep"), self.mark()]
        )
        before = self.snapshot()
        for mark in ("missing", "keep", "suggested-unknown"):
            with self.assertRaisesRegex(Conflict, "change_mark_required"):
                self.preview(edits=[self.edit(mark_id=mark)])
        with self.assertRaisesRegex(Conflict, "keep_change_overlap"):
            self.preview(edits=[self.edit()])
        self.assertEqual(before, self.snapshot())

    def test_invalid_edit_shape_bounds_noop_and_limits(self):
        self.write()
        for edit in (
            self.edit(start=True),
            self.edit(start=-1),
            self.edit(end=0),
            self.edit(start=3, end=3),
            self.edit(replacement="x" * 12001),
            {**self.edit(), "controls": {"seed": "99"}},
        ):
            with self.assertRaises(ValidationError):
                self.preview(edits=[edit])
        for edits in (
            [self.edit(end=10000)],
            [self.edit(), self.edit()],
            [self.edit(replacement="缓慢")],
            [self.edit(start=0, end=len(self.recipe["prompt"]), replacement=" ")],
            [self.edit(replacement="x" * 12000)],
        ):
            with self.assertRaises(ValueError):
                self.preview(edits=edits)
        with self.assertRaises(ValidationError):
            self.preview(edits=[self.edit()] * 51)
        with self.assertRaises(ValidationError):
            self.preview(version=True)

    def test_stale_versions_actual_marks_only_and_no_auto_verdict(self):
        empty = self.preview(revision=0)
        self.assertEqual(empty["status"], "no_change_marks")
        self.write()
        for kwargs in ({"revision": 0}, {"version": 2}):
            with self.assertRaisesRegex(Conflict, "conflict"):
                self.preview(**kwargs)
        self.reviews.patch(
            self.principal,
            self.scope.project_id,
            "shot",
            self.output["artifact"],
            {"expected_revision": 1, "marks": [], "verdict": "redo"},
            "clear",
        )
        self.assertEqual(self.preview(revision=2)["status"], "no_change_marks")
        with self.assertRaisesRegex(Conflict, "change_mark_required"):
            self.preview(revision=2, edits=[self.edit()])

    def test_unknown_recipe_or_seed_and_window_recipes_never_guess_defaults(self):
        self.write()
        for change in (
            {"authoring_recipe": None},
            {"authoring_recipe": {**self.recipe, "controls": {"seed": None}}},
            {"request": {"seed": 43}},
            {"request": {"seed": True}},
            {"request": None},
            {"request": {"seed": 42, "window_seconds": 5}},
            {"request": {"seed": 42, "multi_prompts_gen_type": "PW"}},
            {
                "authoring_recipe": {
                    **self.recipe,
                    "controls": {"seed": "42", "script_window_prompts": ["one", "two"]},
                }
            },
        ):
            with self.subTest(change=change):
                original = copy.deepcopy(self.stored)
                self.stored.update(change)
                self.store()
                with self.assertRaises(Conflict):
                    self.preview(edits=[self.edit()])
                self.stored = original
        self.store()

    def test_account_project_and_job_scopes_and_acceptance_proof(self):
        self.write()
        agent = Principal(
            self.principal.owner,
            "connection",
            machine=True,
            scopes=("projects:read", "jobs:read"),
            project_ids=(self.scope.project_id,),
        )
        self.assertEqual(self.preview(principal=agent)["status"], "needs_edits")
        for principal in (
            replace(agent, scopes=("projects:read",)),
            replace(agent, scopes=("jobs:read",)),
            replace(agent, project_ids=("elsewhere",)),
            replace(self.principal, owner="foreign", role="admin"),
        ):
            with self.assertRaises(NotFound):
                self.preview(principal=principal)
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == self.output["job"])
                .values(result={"artifact_ids": []})
            )
        with self.assertRaises(NotFound):
            self.preview()

    def test_deleted_take_refuses_without_returning_recipe_or_marks(self):
        self.write()
        with self.repo.transaction() as conn:
            conn.execute(
                update(artifacts)
                .where(artifacts.c.id == self.output["artifact"])
                .values(metadata={"kind": "video", "content_deleted": True})
            )
        with self.assertRaises(NotFound):
            self.preview()

    def test_http_preview_is_no_store_and_validates_without_a_write_key(self):
        self.write()
        app = FastAPI()
        app.state.repository = self.repo
        app.state.settings = SimpleNamespace(tenant_id=self.scope.tenant_id)

        @app.middleware("http")
        async def principal(request: Request, call_next):
            request.state.principal = self.principal
            return await call_next(request)

        @app.exception_handler(Conflict)
        async def conflict(request, error):
            return JSONResponse({"error": str(error)}, status_code=409)

        register_routes(app)
        path = PATH.format(
            project_id=self.scope.project_id,
            shot_id="shot",
            artifact_id=self.output["artifact"],
        )
        with TestClient(app) as client:
            response = client.post(
                path,
                json={
                    "expected_project_version": 1,
                    "expected_review_revision": 1,
                    "edits": [self.edit()],
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.json()["status"], "preview")
            self.assertEqual(
                client.post(
                    path,
                    json={"expected_project_version": 1, "expected_review_revision": 0},
                ).status_code,
                409,
            )
            self.assertEqual(client.post(path, json={"edits": []}).status_code, 422)
