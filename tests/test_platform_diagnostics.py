"""Aggregate correctness, owner isolation and strict read-only operational access."""
import contextlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import event, update

from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.diagnostics import job_activity, operator_snapshot, main
from studio_platform.repository import Scope, jobs
from test_platform_repository import LedgerCase


class DiagnosticTests(LedgerCase):
    def test_counts_and_ages_separate_first_attempt_from_retries_and_owners(self):
        first = self.job()
        retry = self.job()
        self.job(scope=self.other, cost=0)
        elsewhere = Scope(self.scope.tenant_id, self.scope.owner_id, "other-project")
        self.job(scope=elsewhere, cost=0)
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == retry["id"]).values(attempt_no=1, created_at=self.now-80))
            conn.execute(update(jobs).where(jobs.c.id == first["id"]).values(created_at=self.now-50))
        value = job_activity(self.repo, tenant_id=self.scope.tenant_id, owner_id=self.scope.owner_id,
                             project_id=self.scope.project_id)
        self.assertEqual(value["total"], 2)
        self.assertEqual(value["oldest_first_attempt_age_s"], 50)
        self.assertEqual(value["oldest_retry_age_s"], 80)
        self.assertEqual(value["oldest_queued_age_s"], 80)
        empty = job_activity(self.repo, tenant_id="absent")
        self.assertEqual(empty["total"], 0)
        self.assertIsNone(empty["oldest_queued_age_s"])

    def test_stale_is_unknown_and_terminal_pending_is_not_zero_bill(self):
        control = WorkerControl(self.repo)
        control.register(WorkerSpec("cpu-diag", "cpu-render", "local-cpu", "synthetic-host", (),
            ("chapter-roughcut-v1",), "sixnine-chapter-roughcut-v1", "cpu-render-v1", backend="cpu-render"))
        control.mark_ready("cpu-diag", upstream_idle_confirmed=True)
        job = self.job()
        from studio_platform.queue import TaskQueue
        queue = TaskQueue(self.repo)
        claim = queue.claim("operator-test", "test-pool")
        queue.begin_submission(claim.lease)
        queue.mark_submission_unknown(claim.lease)
        self.now += 200
        snapshot = operator_snapshot(self.repo, tenant_id=self.scope.tenant_id)
        self.assertEqual(snapshot["workers"]["unknown"], 1)
        self.assertEqual(snapshot["jobs"]["counts"]["submission_unknown"], 1)
        self.assertEqual(snapshot["budget_counters"]["reserved_microusd"], 100_000)
        # Normal reservations are not prematurely reported as final billing pending.
        self.assertEqual(snapshot["budget_counters"]["pending_reservations"], 0)
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="failed", result={"billing_status": "pending"}))
        snapshot = operator_snapshot(self.repo, tenant_id=self.scope.tenant_id)
        self.assertEqual(snapshot["budget_counters"]["pending_reservations"], 1)
        self.assertIn("billing_unsettled_keep_reservations", snapshot["alerts"])

    def test_waiting_for_boot_is_not_mixed_with_ready_queue_time(self):
        job = self.job()
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="waiting_capacity", created_at=self.now-920))
        snapshot = operator_snapshot(self.repo, tenant_id=self.scope.tenant_id)
        self.assertEqual(snapshot["jobs"]["counts"]["waiting_capacity"], 1)
        self.assertEqual(snapshot["jobs"]["oldest_capacity_wait_age_s"], 920)
        self.assertIsNone(snapshot["jobs"]["oldest_queued_age_s"])
        self.assertIn("capacity_wait_older_than_15_minutes_review_boot_and_qualification", snapshot["alerts"])

    def test_aggregate_never_reads_user_payload_or_writes(self):
        plan = self.plan(prompt="PRIVATE PROMPT SHOULD NEVER BE SELECTED")
        self.repo.create_job(self.scope, plan["id"], "sensitive", budget_account_ids=["owner-budget"])
        statements = []
        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
            self.assertTrue(statement.lstrip().upper().startswith("SELECT"), statement)
            compiled = context.compiled.statement if context.compiled is not None else None
            if compiled is not None:
                self.assertFalse({"request", "execution_plan", "payload", "record", "token_hash"} & set(compiled.selected_columns.keys()))
        event.listen(self.repo.engine, "before_cursor_execute", record)
        try:
            value = operator_snapshot(self.repo, tenant_id=self.scope.tenant_id)
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", record)
        text = json.dumps(value)
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn(self.scope.owner_id, text)
        self.assertTrue(statements)

    def test_cli_refuses_missing_database_without_creating_it(self):
        missing = Path(self.temp.name) / "never-created" / "platform.sqlite3"
        with patch.dict(os.environ, {"SIXNINE_DATABASE_URL": "sqlite:///"+missing.as_posix(),
                                    "SIXNINE_DATABASE_URL_FILE": ""}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([]), 1)
        self.assertFalse(missing.exists())

    def test_cli_existing_sqlite_is_read_only_and_no_auth_init(self):
        if self.repo.engine.dialect.name != "sqlite":
            self.skipTest("SQLite-specific read-only URI; aggregate queries also run on isolated PG")
        output = io.StringIO()
        with patch.dict(os.environ, {"SIXNINE_DATABASE_URL": self.url, "SIXNINE_DATABASE_URL_FILE": ""}), \
             patch("studio_platform.repository.Repository.create_schema", side_effect=AssertionError("No schema changes")), \
             contextlib.redirect_stdout(output):
            self.assertEqual(main([]), 0)
        self.assertEqual(json.loads(output.getvalue())["schema"], "sixnine-diagnostics-v1")
