"""Crash windows and partial admission: durable job links precede execution."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import event, select

from studio_platform.api import create_app
from studio_platform.repository import BudgetExceeded, NotFound, jobs
from studio_platform.queue import TaskQueue
import test_platform_api as api_tests


class BatchCrashTests(unittest.TestCase):
    setUp = api_tests.ApiTests.setUp
    login = api_tests.ApiTests.login
    setup_project = api_tests.ApiTests.setup_project
    make_plan = api_tests.ApiTests.make_plan

    def ready(self):
        self.app = create_app(replace(self.settings, generation_enabled=True, execution_backend="mock"),
            repository=self.app.state.repository, storage=self.app.state.storage)
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
        with patch.object(self.app.state.batches, "record_item", side_effect=RuntimeError("synthetic process crash")):
            response = self.client.post("/v1/batches", json=self.body, headers=self.headers)
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
        with patch.object(self.app.state, "enqueue_planned", side_effect=RuntimeError("synthetic process crash")):
            response = self.client.post("/v1/batches", json=self.body, headers=self.headers)
        self.assertEqual(response.status_code, 500)
        before = self.rows()
        self.assertEqual(before[0]["status"], "planned")
        response = self.client.post("/v1/batches", json=self.body, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["items"][0]["job_id"], before[0]["id"])
        self.assertEqual(response.json()["counts"], {"queued": 1})

    def test_budget_race_after_link_reports_blocked_and_can_recover(self):
        self.ready()
        with patch.object(self.app.state, "enqueue_planned", side_effect=BudgetExceeded("synthetic concurrent reservation")):
            response = self.client.post("/v1/batches", json=self.body, headers=self.headers)
            self.assertEqual(response.status_code, 202, response.text)
            item = response.json()["items"][0]
            self.assertIsNotNone(item["job_id"])
            self.assertEqual(item["status"], "admission_blocked")
            self.assertEqual(item["error_code"], "budget_exceeded")
            second = self.client.post("/v1/batches", json=self.body, headers=self.headers)
            self.assertEqual(second.status_code, 202)
            self.assertEqual(second.json()["items"][0]["job_id"], item["job_id"])
        response = self.client.post("/v1/batches", json=self.body, headers=self.headers)
        self.assertEqual(response.json()["counts"], {"queued": 1})
        self.assertIsNone(response.json()["items"][0]["error_code"])
        self.assertEqual(len(self.rows()), 1)

    def test_cancel_after_link_prevents_late_activation(self):
        self.ready()
        with patch.object(self.app.state, "enqueue_planned", side_effect=RuntimeError("synthetic process crash")):
            self.client.post("/v1/batches", json=self.body, headers=self.headers)
        batches = self.client.get("/v1/batches?client_project_id=story-one").json()["batches"]
        ident = batches[0]["id"]
        self.assertEqual(self.client.post(f"/v1/batches/{ident}/cancel").status_code, 200)
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
        plans = [self.make_plan(prompt=("private-prompt-canary-"+"x"*10000 if large else "A traveller walks."))["plan_id"]
                 for _ in range(count)]
        response = self.client.post("/v1/batches", json={"client_project_id": "story-one", "plan_ids": plans},
            headers={"Idempotency-Key": key})
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
        self.addCleanup(event.remove, self.app.state.repository.engine, "before_cursor_execute", record)
        return statements

    def test_list_is_status_only_one_scalar_query_and_never_returns_large_prompts(self):
        self.ready()
        first, second = self.seed_batch(key="summary-a"), self.seed_batch(key="summary-b")
        statements = self.capture()
        response = self.client.get("/v1/batches", params={"client_project_id": "story-one", "limit": 2})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual({row["id"] for row in value["batches"]}, {first["id"], second["id"]})
        self.assertLess(len(response.content), 16000)
        self.assertNotIn("private-prompt-canary", response.text)
        self.assertNotIn("effective_request", response.text)
        self.assertNotIn('"job":', response.text)
        for row in value["batches"]:
            self.assertTrue(row["summary"])
            self.assertEqual(row["counts"], {"queued": 6})
            self.assertTrue(all(item["job_id"] and item["plan_id"] for item in row["items"]))
        job_queries = [columns for sql, columns in statements if "platform_jobs" in sql]
        self.assertEqual(job_queries, [{"id", "status"}])
        self.assertLessEqual(len(statements), 5)

    def test_detail_uses_bounded_dto_and_bulk_artifacts_without_n_plus_one(self):
        self.ready()
        record = self.seed_batch(count=12)
        repo, queue = self.app.state.repository, TaskQueue(self.app.state.repository)
        for index in range(12):
            # Trusted synthetic ledger fixture only; no backend is invoked.
            row = repo.get_job_for_owner(self.settings.tenant_id, "superdan", record["items"][index]["job_id"])
            claim = queue.claim("synthetic-read-worker", row["pool"], job_ids=[row["id"]])
            self.assertIsNotNone(claim)
            queue.begin_submission(claim.lease)
            queue.record_submitted(claim.lease, "synthetic-task-"+str(index))
            queue.begin_collection(claim.lease)
            queue.complete(claim.lease, [{"kind": "video", "object_key": "owners/superdan/test-read/output-"+str(index)+".mp4",
                "size_bytes": 1, "sha256": "a"*64, "validated": True}], actual_cost_microusd=0)
        expected = {item["job_id"]: self.app.state.public_job(repo.get_job_for_owner(self.settings.tenant_id,
            "superdan", item["job_id"])) for item in record["items"]}
        statements = self.capture()
        response = self.client.get("/v1/batches/"+record["id"])
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertFalse(value["summary"])
        self.assertEqual(value["counts"], {"succeeded": 12})
        self.assertEqual({item["job_id"]: item["job"] for item in value["items"]}, expected)
        self.assertTrue(all(len(item["job"]["artifacts"]) == 1 for item in value["items"]))
        projected = [columns for _, columns in statements if "_summary_request" in columns]
        self.assertEqual(len(projected), 1)
        self.assertFalse(any("request" in columns or "execution_plan" in columns for _, columns in statements))
        # Original bounded reads plus one three-query cancellation proof batch,
        # independent of item count. Never add one proof read per job.
        proof_queries = [columns for _, columns in statements if "_actual_cost_microusd" in columns]
        self.assertEqual(len(proof_queries), 1)
        self.assertLessEqual(len(statements), 10)

    def test_list_pages_ten_batches_and_rejects_oversize_page(self):
        self.ready()
        records = [self.seed_batch(count=1, key="page-"+str(index), large=False) for index in range(13)]
        first = self.client.get("/v1/batches", params={"client_project_id": "story-one"}).json()
        self.assertEqual(len(first["batches"]), 10)
        self.assertEqual((first["limit"], first["offset"], first["has_more"], first["next_offset"]), (10, 0, True, 10))
        second = self.client.get("/v1/batches", params={"client_project_id": "story-one", "offset": first["next_offset"]}).json()
        self.assertEqual(len(second["batches"]), 3)
        self.assertEqual((second["has_more"], second["next_offset"]), (False, None))
        self.assertFalse({r["id"] for r in first["batches"]} & {r["id"] for r in second["batches"]})
        self.assertEqual({r["id"] for r in first["batches"]+second["batches"]}, {r["id"] for r in records})
        for params in ({"limit": 11}, {"limit": 100}, {"limit": 0}, {"offset": -1}, {"offset": 100001}):
            with self.subTest(params=params):
                response = self.client.get("/v1/batches", params={"client_project_id": "story-one", **params})
                self.assertEqual(response.status_code, 422)

    def test_bulk_dto_and_status_enforce_exact_owner_tenant_project_and_empty_mapping(self):
        self.ready()
        record = self.seed_batch(count=2)
        repo = self.app.state.repository
        pairs = {item["job_id"]: "story-one" for item in record["items"]}
        for method in (repo.get_job_statuses_for_owner, repo.get_job_summaries_for_owner):
            self.assertEqual(set(method(self.settings.tenant_id, "superdan", pairs)), set(pairs))
            self.assertEqual(method(self.settings.tenant_id, "superdan", {}), {})
            with self.assertRaises(NotFound):
                method(self.settings.tenant_id, "supervan", pairs)
            with self.assertRaises(NotFound):
                method("other-tenant", "superdan", pairs)
            with self.assertRaises(NotFound):
                method(self.settings.tenant_id, "superdan", {ident: "other-project" for ident in pairs})
            with self.assertRaises(ValueError):
                method(self.settings.tenant_id, "superdan", None)
        self.login("supervan")
        self.client.post("/v1/projects", json={"project": api_tests.project()}).raise_for_status()
        response = self.client.get("/v1/batches", params={"client_project_id": "story-one"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["batches"], [])
        self.assertEqual(self.client.get("/v1/batches/"+record["id"]).status_code, 404)


if __name__ == "__main__":
    unittest.main()
