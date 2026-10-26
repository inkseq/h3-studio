"""Review-loop proofs in a disposable ledger; no external calls or telemetry."""

import json
from types import SimpleNamespace
import uuid
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update, func

from studio_platform.auth import Principal
from studio_platform.identity_schema import metadata as identity_metadata
from studio_platform.identity_schema import users
from studio_platform.operator_capacity import OperatorError
from studio_platform.repository import Scope, artifacts, attempts, documents, jobs
from studio_platform.review_loop import register_routes, review_loop
from test_platform_repository import LedgerCase


class ReviewLoopTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = 10000.0

    def take(self, shot="shot", *, created=100, delivered=200, cost=10, scope=None):
        scope = scope or self.scope
        job = self.job()
        attempt, artifact = str(uuid.uuid4()), str(uuid.uuid4())
        with self.repo.engine.begin() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(
                    tenant_id=scope.tenant_id,
                    owner_id=scope.owner_id,
                    project_id=scope.project_id,
                    request={
                        "client_ref": {"shot_id": shot, "project_id": scope.project_id},
                        "request": {"mode": "fl", "prompt": "PRIVATE PROMPT"},
                    },
                    created_at=created,
                    status="succeeded",
                    current_attempt_id=attempt,
                    fence=2,  # complete() fences the finished lease once more
                    result={"artifact_ids": [artifact]},
                )
            )
            conn.execute(
                insert(attempts).values(
                    id=attempt,
                    job_id=job["id"],
                    number=1,
                    status="succeeded",
                    fence=1,
                    worker_id="private-worker",
                    created_at=created,
                    updated_at=delivered,
                    actual_cost_microusd=cost,
                )
            )
            conn.execute(
                insert(artifacts).values(
                    id=artifact,
                    job_id=job["id"],
                    attempt_id=attempt,
                    created_at=delivered,
                    metadata={"kind": "video", "url": "PRIVATE MEDIA"},
                )
            )
        return {"job": job["id"], "artifact": artifact, "attempt": attempt}

    def film(self, take, *, at=500, scope=None, phase="completed", results=None):
        scope = scope or self.scope
        output = self.take(
            "export-placeholder", created=at - 10, delivered=at, scope=scope
        )
        with self.repo.engine.begin() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == output["job"])
                .values(request={"request": {"mode": "render"}})
            )
        ident = str(uuid.uuid4())
        self.repo.put_document(
            scope,
            "film_run",
            ident,
            {
                "phase": phase,
                "export": {"job_id": output["job"], "artifact_id": output["artifact"]},
                "project": {"title": "PRIVATE TITLE"},
                "results": results
                or {
                    "shot": {
                        "artifact_id": take["artifact"],
                        "entity": {"url": "PRIVATE MEDIA"},
                    }
                },
            },
        )
        return ident, output

    def report(self, start=50, end=150, owner=None):
        return review_loop(
            self.repo,
            self.scope.tenant_id,
            owner or self.scope.owner_id,
            start=start,
            end=end,
            now=self.now,
        )

    def test_shipped_is_separate_from_explicit_usable_and_has_no_content(self):
        take = self.take()
        self.film(take)
        result = self.report()
        self.assertEqual(result["metrics"]["first_take_shipped_rate"]["value"], 1)
        self.assertIsNone(result["metrics"]["first_usable_rate"]["value"])
        self.assertIsNone(result["metrics"]["user_actions_per_usable_shot"]["value"])
        self.assertIsNone(
            result["metrics"]["full_cost_per_usable_shot_microusd"]["value"]
        )
        self.assertEqual(result["coverage"]["known_attempt_cost_microusd"], 10)
        text = json.dumps(result)
        for private in ("PRIVATE", take["job"], take["artifact"], "private-worker"):
            self.assertNotIn(private, text)

    def test_no_export_is_zero_but_no_deliveries_is_null(self):
        empty = self.report()["metrics"]["first_take_shipped_rate"]
        self.assertIsNone(empty["value"])
        self.assertEqual(empty["status"], "insufficient_data")
        self.take()
        self.assertEqual(
            self.report()["metrics"]["first_take_shipped_rate"]["value"], 0
        )

    def test_frozen_export_not_current_selection_and_repeated_exports_once(self):
        take = self.take()
        self.film(take)
        self.film(take, at=600)
        self.repo.put_document(
            Scope(self.scope.tenant_id, self.scope.owner_id, "__projects"),
            "project",
            self.scope.project_id,
            {"selectedAssetId": "different", "starred": True},
        )
        result = self.report()
        self.assertEqual(result["cohort"]["exported_shots"], 1)
        self.assertEqual(result["metrics"]["first_take_shipped_rate"]["numerator"], 1)

    def test_redo_before_export_false_but_after_first_export_does_not_rewrite_history(
        self,
    ):
        take = self.take()
        self.film(take)
        self.take(created=600, delivered=700)
        self.assertEqual(
            self.report()["metrics"]["first_take_shipped_rate"]["value"], 1
        )
        self.take(created=300, delivered=400)
        self.assertEqual(
            self.report()["metrics"]["first_take_shipped_rate"]["value"], 0
        )

    def test_later_export_of_first_take_does_not_erase_earlier_other_take(self):
        first = self.take()
        second = self.take(created=250, delivered=300)
        self.film(second, at=400)
        self.film(first, at=600)
        self.assertEqual(
            self.report()["metrics"]["first_take_shipped_rate"]["value"], 0
        )

    def test_initial_batch_alternatives_are_not_inferred_as_redo(self):
        first = self.take(created=100, delivered=200)
        self.take(created=101, delivered=210)
        self.film(first, at=400)
        self.assertEqual(
            self.report()["metrics"]["first_take_shipped_rate"]["value"], 1
        )

    def test_first_job_outside_window_never_reclassifies_a_redo(self):
        self.take(created=25, delivered=40)
        take = self.take(created=100, delivered=200)
        self.film(take)
        self.assertEqual(self.report()["cohort"]["observed_shots"], 0)

    def test_attempt_reclaim_is_not_redo_and_all_attempt_costs_are_counted(self):
        take = self.take()
        self.film(take)
        lost = str(uuid.uuid4())
        with self.repo.engine.begin() as conn:
            conn.execute(
                insert(attempts).values(
                    id=lost,
                    job_id=take["job"],
                    number=2,
                    status="attempt_lost",
                    fence=0,
                    worker_id="w",
                    created_at=110,
                    updated_at=120,
                    actual_cost_microusd=7,
                )
            )
            conn.execute(
                insert(artifacts).values(
                    id=str(uuid.uuid4()),
                    job_id=take["job"],
                    attempt_id=lost,
                    metadata={"kind": "video"},
                    created_at=150,
                )
            )
        result = self.report()
        self.assertEqual(result["metrics"]["first_take_shipped_rate"]["value"], 1)
        self.assertEqual(result["coverage"]["known_attempt_cost_microusd"], 17)
        self.assertEqual(result["coverage"]["attempts_observed"], 2)

    def test_unknown_actual_is_not_zero_or_reserved_quote(self):
        self.take(cost=None)
        result = self.report()
        self.assertIsNone(result["coverage"]["known_attempt_cost_microusd"])
        self.assertEqual(result["coverage"]["attempts_with_known_cost"], 0)
        self.take("second", cost=0)
        result = self.report()
        self.assertEqual(result["coverage"]["known_attempt_cost_microusd"], 0)
        self.assertEqual(result["coverage"]["attempts_with_known_cost"], 1)
        self.assertFalse(result["coverage"]["cost_components_complete"])

    def test_real_queue_completion_fence_and_accepted_artifact_receipt(self):
        from studio_platform.queue import TaskQueue
        from test_platform_queue import VALID_ARTIFACT

        self.now = 100
        job = self.job()
        queue = TaskQueue(self.repo)
        claim = queue.claim("worker", "test-pool")
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "upstream")
        queue.begin_collection(claim.lease)
        self.now = 110
        done = queue.complete(claim.lease, [VALID_ARTIFACT], actual_cost_microusd=9)
        self.assertGreater(done["fence"], claim.lease.fence)
        with self.repo.engine.begin() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(
                    request={
                        "request": {"mode": "fl"},
                        "client_ref": {
                            "project_id": self.scope.project_id,
                            "shot_id": "shot",
                        },
                    }
                )
            )
        self.now = 10000
        result = self.report()
        self.assertEqual(result["cohort"]["delivered_shots"], 1)
        with self.repo.engine.begin() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(result={"artifact_ids": []})
            )
        self.assertEqual(self.report()["cohort"]["delivered_shots"], 0)

    def test_tenant_owner_and_project_export_isolation(self):
        own = self.take()
        for scope in (
            self.other,
            Scope("other-tenant", self.scope.owner_id, self.scope.project_id),
            Scope(self.scope.tenant_id, self.scope.owner_id, "different-project"),
        ):
            other = self.take(scope=scope)
            self.film(other, scope=scope)
        result = self.report()
        self.assertEqual(
            result["cohort"]["observed_shots"], 2
        )  # same owner, two projects
        self.assertEqual(result["metrics"]["first_take_shipped_rate"]["value"], 0.5)
        # A document cannot launder another owner's final export or original take.
        foreign = self.take(scope=self.other)
        ident, output = self.film(own)
        with self.repo.engine.begin() as conn:
            conn.execute(
                update(documents)
                .where(documents.c.document_id == ident)
                .values(
                    payload={
                        "phase": "completed",
                        "export": {
                            "job_id": output["job"],
                            "artifact_id": output["artifact"],
                        },
                        "results": {"shot": {"artifact_id": foreign["artifact"]}},
                    }
                )
            )
        self.assertEqual(
            self.report()["metrics"]["first_take_shipped_rate"]["numerator"], 1
        )

    def test_invalid_export_proof_is_incomplete_not_shipped(self):
        take = self.take()
        _, output = self.film(take)
        with self.repo.engine.begin() as conn:
            conn.execute(
                update(jobs).where(jobs.c.id == output["job"]).values(status="failed")
            )
        result = self.report()
        self.assertIsNone(result["metrics"]["first_take_shipped_rate"]["value"])
        self.assertIn("export_receipt_unverified", result["coverage"]["reasons"])

    def test_preflight_does_not_count_and_migration_does_not_fake_complete_rate(self):
        take = self.take()
        self.film(take, phase="preflight")
        self.assertEqual(
            self.report()["metrics"]["first_take_shipped_rate"]["value"], 0
        )
        self.repo.put_document(self.scope, "quick_chat_story_migration", "receipt", {})
        result = self.report()
        self.assertIsNone(result["metrics"]["first_take_shipped_rate"]["value"])
        self.assertEqual(result["coverage"]["migrated_projects"], 1)

    def test_equal_delivery_timestamps_and_query_limits_are_explicit(self):
        self.take()
        self.take(created=101, delivered=200)
        result = self.report()
        self.assertIn("first_delivery_order_ambiguous", result["coverage"]["reasons"])
        with patch("studio_platform.review_loop.MAX_JOBS", 1):
            result = self.report()
        self.assertIn("jobs_limit", result["coverage"]["reasons"])
        self.assertIsNone(result["metrics"]["first_take_shipped_rate"]["value"])

    def test_read_does_not_write_and_empty_other_owner_is_empty(self):
        self.take()
        with self.repo.engine.connect() as conn:
            before = conn.execute(
                select(func.count()).select_from(documents)
            ).scalar_one()
        self.report()
        self.assertEqual(self.report(owner="absent")["cohort"]["observed_shots"], 0)
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                conn.execute(select(func.count()).select_from(documents)).scalar_one(),
                before,
            )

    def test_http_human_admin_required_utc_and_single_owner(self):
        identity_metadata.create_all(self.repo.engine)
        with self.repo.engine.begin() as conn:
            conn.execute(
                insert(users).values(
                    id=str(uuid.uuid4()),
                    tenant=self.scope.tenant_id,
                    owner="superdan",
                    role="admin",
                    status="active",
                    display_name="Owner",
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        app = FastAPI()
        app.state.repository = self.repo
        app.state.settings = SimpleNamespace(
            tenant_id=self.scope.tenant_id, operator_admin_owners=("superdan",)
        )
        caller = [Principal("superdan", "browser")]

        @app.middleware("http")
        async def principal(request: Request, call_next):
            request.state.principal = caller[0]
            return await call_next(request)

        @app.exception_handler(OperatorError)
        async def operator_error(request, exc):
            return JSONResponse({"code": exc.code}, status_code=exc.status)

        register_routes(app)
        params = {
            "owner_id": "superdan",
            "from": "1970-01-01T00:00:50Z",
            "to": "1970-01-01T00:02:30Z",
        }
        with TestClient(app) as client:
            result = client.get("/v1/operator/admin/review-loop", params=params)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.headers["cache-control"], "no-store")
            caller[0] = Principal("superdan", "pat:x", machine=True)
            self.assertEqual(
                client.get("/v1/operator/admin/review-loop", params=params).status_code,
                403,
            )
            caller[0] = Principal("supervan", "browser")
            self.assertEqual(
                client.get("/v1/operator/admin/review-loop", params=params).status_code,
                403,
            )
            caller[0] = Principal("superdan", "browser")
            self.assertEqual(
                client.get(
                    "/v1/operator/admin/review-loop",
                    params={k: v for k, v in params.items() if k != "owner_id"},
                ).status_code,
                422,
            )
            for changes in (
                {"from": "1970-01-01T00:00:50"},
                {"to": "1971-01-01T00:00:00Z"},
                {"from": params["to"]},
            ):
                self.assertEqual(
                    client.get(
                        "/v1/operator/admin/review-loop", params={**params, **changes}
                    ).status_code,
                    400,
                )
