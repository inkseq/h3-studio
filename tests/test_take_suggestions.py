"""Suggested ranges require explicit human decisions; no model/provider calls."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update

from studio_platform.auth import Principal
from studio_platform.repository import Conflict, NotFound, Scope, artifacts, documents
from studio_platform.take_reviews import (
    TakeReviews,
    TakeSuggestions,
    TakeSuggestionDecisions,
    register_routes,
)
from test_platform_repository import LedgerCase
import test_take_reviews as fixture


class TakeSuggestionTests(LedgerCase):
    take = fixture.TakeReviewTests.take
    report = fixture.TakeReviewTests.report
    mark = fixture.TakeReviewTests.mark
    read = fixture.TakeReviewTests.read
    write = fixture.TakeReviewTests.write

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
                {"id": ident, "type": "shot", "data": {}}
                for ident in ("shot", "other-shot")
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

    def invoke(self, method, body=None, key="suggestion-key", **changes):
        args = dict(
            principal=self.principal,
            project_id=self.scope.project_id,
            shot_id="shot",
            artifact_id=self.output["artifact"],
        )
        if method != "suggestions":
            args.update(body=body, key=key)
        return getattr(self.service, method)(**{**args, **changes})

    def propose(self, revision=0, marks=None, **changes):
        return self.invoke(
            "propose",
            {
                "expected_revision": revision,
                "marks": marks if marks is not None else [self.mark()],
            },
            **changes,
        )

    def decide(self, revision=1, accepts=None, rejects=None, **changes):
        return self.invoke(
            "decide",
            {
                "expected_revision": revision,
                "accept_ids": accepts or [],
                "reject_ids": rejects or [],
            },
            key=changes.pop("key", "decision-key"),
            **changes,
        )

    def agent(self, **changes):
        return Principal(
            self.scope.owner_id,
            "agent-client",
            machine=True,
            all_projects=True,
            scopes=("projects:read", "projects:write"),
            **changes,
        )

    def test_propose_decide_and_later_edit_preserve_independent_whole_verdict(self):
        before = self.repo.get_document(
            self.project_scope, "project", self.scope.project_id
        )
        initial = self.invoke("suggestions")
        self.assertEqual(
            initial,
            {
                "revision": 0,
                "suggestion_revision": None,
                "producer_kind": None,
                "marks": [],
            },
        )
        self.write(
            {
                "expected_revision": 0,
                "verdict": "redo",
                "marks": [self.mark(id="original")],
            }
        )
        proposed = self.propose(
            1, [self.mark(), self.mark(id="m2", verdict="keep")], principal=self.agent()
        )
        self.assertEqual(
            (proposed["revision"], proposed["mark_count"], proposed["pending_count"]),
            (2, 1, 2),
        )
        self.assertEqual(self.read()["verdict"], "redo")
        suggested = self.invoke("suggestions")
        self.assertEqual(suggested["producer_kind"], "agent")
        self.assertEqual(suggested["suggestion_revision"], 2)
        self.assertTrue(all(m["status"] == "pending" for m in suggested["marks"]))
        decided = self.decide(2, ["m1"], ["m2"])
        self.assertEqual(
            (decided["revision"], decided["pending_count"], decided["mark_count"]),
            (3, 0, 2),
        )
        current = self.read()
        self.assertEqual(current["verdict"], "redo")
        self.assertEqual(current["marks"][0]["id"], "original")
        self.assertEqual(current["marks"][1]["note"], "PRIVATE REVIEW")
        self.assertEqual(
            current["marks"][1]["id"], decided["outcomes"][0]["review_mark_id"]
        )
        self.assertEqual(self.invoke("suggestions")["marks"][1]["status"], "rejected")
        self.assertEqual(self.report()["metrics"]["first_usable_rate"]["value"], 0)
        self.write({"expected_revision": 3, "marks": []}, "clear-actual")
        self.assertEqual(self.invoke("suggestions")["marks"][0]["status"], "accepted")
        self.propose(4, [self.mark(note="NEW NOTE")], key="next-report")
        self.assertEqual(self.invoke("suggestions")["suggestion_revision"], 5)
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
        serialized = json.dumps(receipts)
        for forbidden in ("PRIVATE", "NEW NOTE", "start_s", "end_s"):
            self.assertNotIn(forbidden, serialized)
        outcomes = [r for r in receipts if r.get("suggestion_action") == "decide"]
        self.assertEqual(outcomes[0]["response"]["outcomes"], decided["outcomes"])
        self.assertFalse(outcomes[0]["verdict_written"])

    def test_replay_cross_endpoint_conflict_and_pending_replacement(self):
        first = self.propose()
        self.assertEqual(self.propose(), first)
        for action in (
            lambda: self.propose(1, [], key="replace"),
            lambda: self.propose(0, [], key="suggestion-key"),
            lambda: self.write(
                {"expected_revision": 0, "marks": [self.mark()]}, "suggestion-key"
            ),
            lambda: self.decide(0, ["m1"]),
        ):
            with self.assertRaises(Conflict):
                action()
        self.assertEqual(self.invoke("suggestions")["revision"], 1)
        accepted = self.decide(1, ["m1"])
        self.assertEqual(self.decide(1, ["m1"]), accepted)
        self.assertEqual(self.propose(), first)
        with self.assertRaises(Conflict):
            self.decide(2, rejects=["m1"], key="different-decision")
        with self.assertRaises(Conflict):
            self.decide(1, rejects=["m1"])
        self.assertEqual(self.read()["revision"], 2)

    def test_bounds_unknown_duration_and_decision_validation_are_atomic(self):
        invalid = [
            {"expected_revision": True, "marks": []},
            {"expected_revision": 0, "marks": [self.mark(), self.mark()]},
            {"expected_revision": 0, "marks": [self.mark(start_s=2, end_s=1)]},
            {"expected_revision": 0, "marks": [self.mark(end_s=float("inf"))]},
            {"expected_revision": 0, "marks": [self.mark(tags=["unsafe"])]},
            {"expected_revision": 0, "marks": [], "model_verified": True},
        ]
        for body in invalid:
            with self.assertRaises(ValidationError):
                TakeSuggestions.model_validate(body)
        for body in (
            {"expected_revision": 0},
            {"expected_revision": 1, "accept_ids": ["m1"], "reject_ids": ["m1"]},
            {"expected_revision": 1, "accept_ids": ["m1", "m1"]},
            {"expected_revision": 1, "accept_ids": ["x" * 101]},
        ):
            with self.assertRaises(ValidationError):
                TakeSuggestionDecisions.model_validate(body)
        with self.assertRaisesRegex(ValueError, "range_outside_duration"):
            self.propose(marks=[self.mark(end_s=6)])
        self.assertEqual(self.invoke("suggestions")["revision"], 0)
        with self.repo.transaction() as conn:
            conn.execute(
                update(artifacts)
                .where(artifacts.c.id == self.output["artifact"])
                .values(metadata={"kind": "video"})
            )
        with self.assertRaisesRegex(Conflict, "duration_unknown"):
            self.propose()
        self.propose(marks=[])
        self.assertEqual(self.read()["marks"], [])
        self.assertIsNone(self.report()["metrics"]["first_usable_rate"]["value"])

    def test_scope_provenance_and_human_decision_gate(self):
        self.propose(principal=self.agent())
        for principal in (
            Principal("foreign", "browser", role="admin"),
            replace(self.agent(), project_ids=("other",), all_projects=False),
            replace(self.agent(), scopes=()),
        ):
            with self.assertRaises(NotFound):
                self.invoke("suggestions", principal=principal)
        with self.assertRaises(NotFound):
            self.invoke("suggestions", shot_id="other-shot")
        with self.assertRaises(NotFound):
            TakeReviews(self.repo, "other-tenant").suggestions(
                self.principal, self.scope.project_id, "shot", self.output["artifact"]
            )
        for principal in (
            replace(self.agent(), scopes=("projects:read",)),
            replace(self.principal, support_viewer=True),
        ):
            with self.assertRaises(NotFound):
                self.decide(1, ["m1"], principal=principal)
        with self.assertRaises(HTTPException) as denied:
            self.decide(1, ["m1"], principal=self.agent())
        self.assertEqual(denied.exception.status_code, 403)
        self.assertEqual(self.read()["revision"], 1)
        self.assertEqual(self.read()["marks"], [])

    def test_total_mark_limit_rejects_entire_decision_and_can_reject_instead(self):
        self.write(
            {
                "expected_revision": 0,
                "marks": [self.mark(id=f"existing-{i}") for i in range(50)],
            }
        )
        self.propose(1)
        with self.assertRaisesRegex(Conflict, "mark_limit"):
            self.decide(2, ["m1"])
        self.assertEqual(self.invoke("suggestions")["marks"][0]["status"], "pending")
        self.assertEqual(self.read()["revision"], 2)
        self.decide(2, rejects=["m1"])
        self.assertEqual(len(self.read()["marks"]), 50)

    def test_decision_and_manual_edit_share_one_revision_under_concurrency(self):
        self.propose()

        def decide():
            try:
                return self.decide(1, ["m1"])
            except Conflict:
                return None

        def edit():
            try:
                return self.write(
                    {"expected_revision": 1, "marks": [self.mark(id="manual")]},
                    "concurrent-edit",
                )
            except Conflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(decide), pool.submit(edit)
            results = [a.result(timeout=10), b.result(timeout=10)]
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.read()["revision"], 2)
        self.assertEqual(len(self.read()["marks"]), 1)

    def test_trash_blocks_reads_proposals_decisions_and_original_replay(self):
        self.propose()
        self.decide(1, ["m1"])
        self.repo.put_document(
            Scope(self.scope.tenant_id, self.scope.owner_id, "__trash"),
            "trash_item",
            "blocked",
            {
                "state": "purging",
                "target": {
                    "kind": "artifact",
                    "id": self.output["artifact"],
                    "project_id": self.scope.project_id,
                },
            },
        )
        for action in (
            lambda: self.invoke("suggestions"),
            self.propose,
            lambda: self.decide(1, ["m1"]),
        ):
            with self.assertRaises(Conflict):
                action()

    def test_suggestion_read_uses_no_authoring_lock(self):
        self.propose()
        with (
            patch(
                "studio_platform.take_reviews.owner_reference_gate",
                side_effect=AssertionError("read lock"),
            ),
            patch.object(self.repo, "_locked", side_effect=AssertionError("row lock")),
        ):
            self.assertEqual(len(self.invoke("suggestions")["marks"]), 1)

    def test_mixed_unknown_id_does_not_partially_accept_and_source_is_rechecked(self):
        self.propose()
        with self.assertRaisesRegex(Conflict, "not_pending"):
            self.decide(1, ["m1"], ["missing"])
        self.assertEqual(self.read()["revision"], 1)
        self.assertEqual(self.read()["marks"], [])
        with self.repo.transaction() as conn:
            conn.execute(
                update(artifacts)
                .where(artifacts.c.id == self.output["artifact"])
                .values(
                    metadata={
                        "kind": "video",
                        "duration_s": 5.0,
                        "content_deleted": True,
                    }
                )
            )
        for action in (
            self.propose,
            lambda: self.invoke("suggestions"),
            lambda: self.decide(1, ["m1"]),
        ):
            with self.assertRaises(NotFound):
                action()

    def test_http_contract_and_untrusted_fields(self):
        app = FastAPI()
        app.state.repository = self.repo
        app.state.settings = SimpleNamespace(tenant_id=self.scope.tenant_id)

        @app.middleware("http")
        async def principal(request: Request, next_call):
            request.state.principal = self.principal
            return await next_call(request)

        register_routes(app)
        path = f"/v1/projects/{self.scope.project_id}/shots/shot/takes/{self.output['artifact']}/review/suggestions"
        with TestClient(app) as client:
            body = {"expected_revision": 0, "marks": [self.mark()]}
            self.assertEqual(client.post(path, json=body).status_code, 422)
            created = client.post(
                path, json=body, headers={"Idempotency-Key": "create"}
            )
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.headers["Cache-Control"], "no-store")
            read = client.get(path)
            self.assertEqual(read.status_code, 200)
            self.assertEqual(read.headers["Cache-Control"], "no-store")
            result = client.post(
                path + "/decisions",
                json={"expected_revision": 1, "reject_ids": ["m1"]},
                headers={"Idempotency-Key": "decide"},
            )
            self.assertEqual(result.status_code, 200, result.text)
            self.assertNotIn("PRIVATE", result.text)
            self.assertEqual(result.headers["Cache-Control"], "no-store")
            self.assertEqual(
                client.post(
                    path,
                    json={**body, "producer_kind": "human"},
                    headers={"Idempotency-Key": "forged"},
                ).status_code,
                422,
            )
