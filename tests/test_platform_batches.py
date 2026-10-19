"""Crash windows and partial admission: durable job links precede execution."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import event, select

from studio_platform.api import create_app
from studio_platform.content_policy import ContentPolicyError
from studio_platform.credits import CreditError
from studio_platform.repository import BudgetExceeded, NotFound, jobs
from studio_platform.queue import TaskQueue
import test_platform_api as api_tests


class BatchCrashTests(unittest.TestCase):
    setUp = api_tests.ApiTests.setUp
    login = api_tests.ApiTests.login
    setup_project = api_tests.ApiTests.setup_project
    make_plan = api_tests.ApiTests.make_plan

    def ready(self):
        self.app = create_app(
            replace(self.settings, generation_enabled=True, execution_backend="mock"),
            repository=self.app.state.repository,
            storage=self.app.state.storage,
        )
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.setup_project()
        plan = self.make_plan()
        self.body = {"client_project_id": "story-one", "plan_ids": [plan["plan_id"]]}
        self.headers = {"Idempotency-Key": "recover-same-batch"}

    def rows(self):
        with self.app.state.repository.engine.connect() as connection:
            return list(connection.execute(select(jobs.c.id, jobs.c.status)).mappings())

    def test_crash_after_task_before_link_never_exposes_runnable_orphan(self):
        self.ready()
        original_record = self.app.state.batches.record_item

        def crash_on_job_link(principal, batch_id, index, value):
            if value.get("job_id"):
                raise RuntimeError("synthetic process crash")
            return original_record(principal, batch_id, index, value)

        with patch.object(
            self.app.state.batches, "record_item", side_effect=crash_on_job_link
        ):
            response = self.client.post(
                "/v1/batches", json=self.body, headers=self.headers
            )
        self.assertEqual(response.status_code, 500)
        before = self.rows()
        self.assertEqual(len(before), 1)
        self.assertEqual(before[0]["status"], "planned")
        response = self.client.post("/v1/batches", json=self.body, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["counts"], {"queued": 1})
        self.assertEqual(self.rows()[0]["id"], before[0]["id"])

    def test_crash_after_link_before_enqueue_resumes_same_task(self):
        self.ready()
        with patch.object(
            self.app.state,
            "enqueue_planned",
            side_effect=RuntimeError("synthetic process crash"),
        ):
            response = self.client.post(
                "/v1/batches", json=self.body, headers=self.headers
            )
        self.assertEqual(response.status_code, 500)
        before = self.rows()
        self.assertEqual(before[0]["status"], "planned")
        response = self.client.post("/v1/batches", json=self.body, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["items"][0]["job_id"], before[0]["id"])
        self.assertEqual(response.json()["counts"], {"queued": 1})

    def test_budget_race_after_link_reports_blocked_and_can_recover(self):
        self.ready()
        with patch.object(
            self.app.state,
            "enqueue_planned",
            side_effect=BudgetExceeded("synthetic concurrent reservation"),
        ):
            response = self.client.post(
                "/v1/batches", json=self.body, headers=self.headers
            )
            self.assertEqual(response.status_code, 202, response.text)
            item = response.json()["items"][0]
            self.assertIsNotNone(item["job_id"])
            self.assertEqual(item["status"], "admission_blocked")
            self.assertEqual(item["error_code"], "budget_exceeded")
            second = self.client.post(
                "/v1/batches", json=self.body, headers=self.headers
            )
            self.assertEqual(second.status_code, 202)
            self.assertEqual(second.json()["items"][0]["job_id"], item["job_id"])
        response = self.client.post("/v1/batches", json=self.body, headers=self.headers)
        self.assertEqual(response.json()["counts"], {"queued": 1})
        self.assertIsNone(response.json()["items"][0]["error_code"])
        self.assertEqual(len(self.rows()), 1)

    def test_known_spending_refusals_keep_diagnostics_and_original_task(self):
        self.ready()
        original_job_id = None
        for code in (
            "connection_daily_limit_reached",
            "user_daily_limit_reached",
            "spending_usage_unconfirmed",
        ):
            with (
                self.subTest(code=code),
                patch.object(
                    self.app.state, "enqueue_planned", side_effect=BudgetExceeded(code)
                ),
            ):
                response = self.client.post(
                    "/v1/batches", json=self.body, headers=self.headers
                )
                self.assertEqual(response.status_code, 202, response.text)
                item = response.json()["items"][0]
                self.assertEqual(item["status"], "admission_blocked")
                self.assertEqual(item["error_code"], code)
                original_job_id = original_job_id or item["job_id"]
                self.assertEqual(item["job_id"], original_job_id)
                self.assertEqual(len(self.rows()), 1)
                self.assertEqual(self.rows()[0]["status"], "planned")
        recovered = self.client.post(
            "/v1/batches", json=self.body, headers=self.headers
        ).json()
        self.assertEqual(recovered["counts"], {"queued": 1})
        self.assertEqual(recovered["items"][0]["job_id"], original_job_id)
        self.assertEqual(len(self.rows()), 1)

    def test_typed_admission_refusals_preserve_partial_batch_and_original_jobs(self):
        self.ready()
        original = self.app.state.enqueue_planned
        for index, error in enumerate(
            (
                ContentPolicyError("terms_acceptance_required"),
                CreditError("insufficient_credits", 402),
            )
        ):
            with self.subTest(code=error.code):
                body = {
                    "client_project_id": "story-one",
                    "plan_ids": [self.make_plan()["plan_id"] for _ in range(3)],
                }
                headers = {"Idempotency-Key": f"typed-refusal-{index}"}
                call_count = 0

                def fail_middle(*args, **kwargs):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 2:
                        raise error
                    return original(*args, **kwargs)

                before = len(self.rows())
                with patch.object(
                    self.app.state, "enqueue_planned", side_effect=fail_middle
                ):
                    response = self.client.post(
                        "/v1/batches", json=body, headers=headers
                    )
                self.assertEqual(response.status_code, 202, response.text)
                items = response.json()["items"]
                self.assertEqual(
                    [item["status"] for item in items],
                    ["queued", "admission_blocked", "queued"],
                )
                self.assertEqual(items[1]["error_code"], error.code)
                original_ids = [item["job_id"] for item in items]
                self.assertTrue(all(original_ids))
                self.assertEqual(len(self.rows()), before + 3)
                recovered = self.client.post("/v1/batches", json=body, headers=headers)
                self.assertEqual(recovered.status_code, 202, recovered.text)
                self.assertEqual(recovered.json()["counts"], {"queued": 3})
                self.assertEqual(
                    [item["job_id"] for item in recovered.json()["items"]], original_ids
                )
                self.assertEqual(len(self.rows()), before + 3)

    def test_cancel_after_link_prevents_late_activation(self):
        self.ready()
        with patch.object(
            self.app.state,
            "enqueue_planned",
            side_effect=RuntimeError("synthetic process crash"),
        ):
            self.client.post("/v1/batches", json=self.body, headers=self.headers)
        batches = self.client.get("/v1/batches?client_project_id=story-one").json()[
            "batches"
        ]
        ident = batches[0]["id"]
        self.assertEqual(
            self.client.post(f"/v1/batches/{ident}/cancel").status_code, 200
        )
        response = self.client.post("/v1/batches", json=self.body, headers=self.headers)
        self.assertEqual(response.json()["counts"], {"cancelled": 1})
        self.assertEqual(self.rows()[0]["status"], "cancelled")


class BatchReadBoundaryTests(unittest.TestCase):
    """SQL/HTTP limits, not provider execution; synthetic ledger outputs only."""

    setUp = api_tests.ApiTests.setUp
    login = api_tests.ApiTests.login
    setup_project = api_tests.ApiTests.setup_project
    make_plan = api_tests.ApiTests.make_plan
    ready = BatchCrashTests.ready

    def seed_batch(self, *, count=6, key="read-boundary", large=True):
        plans = [
            self.make_plan(
                prompt=(
                    "private-prompt-canary-" + "x" * 10000
                    if large
                    else "A traveller walks."
                )
            )["plan_id"]
            for _ in range(count)
        ]
        response = self.client.post(
            "/v1/batches",
            json={"client_project_id": "story-one", "plan_ids": plans},
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def capture(self):
        statements = []

        def record(connection, cursor, statement, parameters, context, executemany):
            compiled = context.compiled
            sql = compiled.statement if compiled is not None else None
            if sql is not None and getattr(sql, "is_select", False):
                statements.append((statement, set(sql.selected_columns.keys())))

        event.listen(self.app.state.repository.engine, "before_cursor_execute", record)
        self.addCleanup(
            event.remove,
            self.app.state.repository.engine,
            "before_cursor_execute",
            record,
        )
        return statements

    def test_list_is_status_only_one_scalar_query_and_never_returns_large_prompts(self):
        self.ready()
        first, second = (
            self.seed_batch(key="summary-a"),
            self.seed_batch(key="summary-b"),
        )
        statements = self.capture()
        response = self.client.get(
            "/v1/batches", params={"client_project_id": "story-one", "limit": 2}
        )
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(
            {row["id"] for row in value["batches"]}, {first["id"], second["id"]}
        )
        self.assertLess(len(response.content), 16000)
        self.assertNotIn("private-prompt-canary", response.text)
        self.assertNotIn("effective_request", response.text)
        self.assertNotIn('"job":', response.text)
        for row in value["batches"]:
            self.assertTrue(row["summary"])
            self.assertEqual(row["counts"], {"queued": 6})
            self.assertTrue(
                all(item["job_id"] and item["plan_id"] for item in row["items"])
            )
        job_queries = [columns for sql, columns in statements if "platform_jobs" in sql]
        self.assertEqual(job_queries, [{"id", "status"}])
        # Includes two fixed PostgreSQL identity-schema probes at authentication;
        # business reads remain scalar and independent of the number of jobs.
        self.assertLessEqual(len(statements), 7)

    def test_detail_uses_bounded_dto_and_bulk_artifacts_without_n_plus_one(self):
        self.ready()
        record = self.seed_batch(count=12)
        repo, queue = self.app.state.repository, TaskQueue(self.app.state.repository)
        for index in range(12):
            # Trusted synthetic ledger fixture only; no backend is invoked.
            row = repo.get_job_for_owner(
                self.settings.tenant_id, "superdan", record["items"][index]["job_id"]
            )
            claim = queue.claim(
                "synthetic-read-worker", row["pool"], job_ids=[row["id"]]
            )
            self.assertIsNotNone(claim)
            queue.begin_submission(claim.lease)
            queue.record_submitted(claim.lease, "synthetic-task-" + str(index))
            queue.begin_collection(claim.lease)
            queue.complete(
                claim.lease,
                [
                    {
                        "kind": "video",
                        "object_key": "owners/superdan/test-read/output-"
                        + str(index)
                        + ".mp4",
                        "size_bytes": 1,
                        "sha256": "a" * 64,
                        "validated": True,
                    }
                ],
                actual_cost_microusd=0,
            )
        expected = {
            item["job_id"]: self.app.state.public_job(
                repo.get_job_for_owner(
                    self.settings.tenant_id, "superdan", item["job_id"]
                )
            )
            for item in record["items"]
        }
        statements = self.capture()
        response = self.client.get("/v1/batches/" + record["id"])
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertFalse(value["summary"])
        self.assertEqual(value["counts"], {"succeeded": 12})
        self.assertEqual(
            {item["job_id"]: item["job"] for item in value["items"]}, expected
        )
        self.assertTrue(
            all(len(item["job"]["artifacts"]) == 1 for item in value["items"])
        )
        projected = [
            columns for _, columns in statements if "_summary_request" in columns
        ]
        self.assertEqual(len(projected), 1)
        self.assertFalse(
            any(
                "request" in columns or "execution_plan" in columns
                for _, columns in statements
            )
        )
        # Original bounded reads plus one three-query cancellation proof batch,
        # independent of item count. Never add one proof read per job.
        proof_queries = [
            columns for _, columns in statements if "_actual_cost_microusd" in columns
        ]
        self.assertEqual(len(proof_queries), 1)
        # The same two fixed identity probes also precede this request.
        self.assertLessEqual(len(statements), 12)

    def test_list_pages_ten_batches_and_rejects_oversize_page(self):
        self.ready()
        records = [
            self.seed_batch(count=1, key="page-" + str(index), large=False)
            for index in range(13)
        ]
        first = self.client.get(
            "/v1/batches", params={"client_project_id": "story-one"}
        ).json()
        self.assertEqual(len(first["batches"]), 10)
        self.assertEqual(
            (first["limit"], first["offset"], first["has_more"], first["next_offset"]),
            (10, 0, True, 10),
        )
        second = self.client.get(
            "/v1/batches",
            params={"client_project_id": "story-one", "offset": first["next_offset"]},
        ).json()
        self.assertEqual(len(second["batches"]), 3)
        self.assertEqual((second["has_more"], second["next_offset"]), (False, None))
        self.assertFalse(
            {r["id"] for r in first["batches"]} & {r["id"] for r in second["batches"]}
        )
        self.assertEqual(
            {r["id"] for r in first["batches"] + second["batches"]},
            {r["id"] for r in records},
        )
        for params in (
            {"limit": 11},
            {"limit": 100},
            {"limit": 0},
            {"offset": -1},
            {"offset": 100001},
        ):
            with self.subTest(params=params):
                response = self.client.get(
                    "/v1/batches", params={"client_project_id": "story-one", **params}
                )
                self.assertEqual(response.status_code, 422)

    def test_bulk_dto_and_status_enforce_exact_owner_tenant_project_and_empty_mapping(
        self,
    ):
        self.ready()
        record = self.seed_batch(count=2)
        repo = self.app.state.repository
        pairs = {item["job_id"]: "story-one" for item in record["items"]}
        for method in (
            repo.get_job_statuses_for_owner,
            repo.get_job_summaries_for_owner,
        ):
            self.assertEqual(
                set(method(self.settings.tenant_id, "superdan", pairs)), set(pairs)
            )
            self.assertEqual(method(self.settings.tenant_id, "superdan", {}), {})
            with self.assertRaises(NotFound):
                method(self.settings.tenant_id, "supervan", pairs)
            with self.assertRaises(NotFound):
                method("other-tenant", "superdan", pairs)
            with self.assertRaises(NotFound):
                method(
                    self.settings.tenant_id,
                    "superdan",
                    {ident: "other-project" for ident in pairs},
                )
            with self.assertRaises(ValueError):
                method(self.settings.tenant_id, "superdan", None)
        self.login("supervan")
        self.client.post(
            "/v1/projects", json={"project": api_tests.project()}
        ).raise_for_status()
        response = self.client.get(
            "/v1/batches", params={"client_project_id": "story-one"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batches"], [])
        self.assertEqual(
            self.client.get("/v1/batches/" + record["id"]).status_code, 404
        )


if __name__ == "__main__":
    unittest.main()
