"""Preserved authoring restore and inert-plan safety; isolated offline data only."""
import contextlib
import json
import sqlite3
import unittest
from sqlalchemy import insert
from studio_platform.backup import _quarantine_restored_database
from studio_platform.quick_chat import objects
from studio_platform.repository import Conflict, attempts
import test_platform_quick_chat_admission as admission_test


class InertPlanHistoryReview(admission_test.LedgerCase):
    """The canonical attempt ledger must outweigh missing summary counters."""

    setUp = admission_test.QuickAdmissionTests.setUp

    def test_hidden_attempt_history_prevents_inert_plan_refresh(self):
        admission = self.app.state.generation_admission
        job = admission.create_planned(self.principal, self.plan["plan_id"], "review-original")
        old = self.app.state.owned_plan(self.principal, self.plan["plan_id"])
        from studio_platform.repository import Scope
        scope = Scope("sixnine", "superdan", "story-one", "quick-chat-execution")
        fresh = self.repo.create_plan(scope, old["request"], old["execution_plan"],
            expires_at=self.repo.clock()+600, estimated_cost_microusd=old["estimated_cost_microusd"])
        # Deliberately simulate inconsistent recovered/legacy summary fields:
        # the job says no attempt, but the authoritative ledger records an
        # upstream submission whose stop has not been confirmed.
        with self.repo.engine.begin() as conn:
            conn.execute(insert(attempts).values(id="review-unknown-attempt", job_id=job["id"],
                number=1, status="submission_unknown", fence=1, worker_id="review-worker",
                created_at=1, updated_at=1, submission_started_at=1,
                upstream_task_id="review-upstream-task", upstream_stopped=0))
        with self.assertRaisesRegex(Conflict, "upstream_stop_unconfirmed"):
            admission.refresh_planned(self.principal, job["id"], fresh["id"])
        self.assertEqual(self.repo.get_job(scope, job["id"])["plan_id"], job["plan_id"])

    def test_restore_holds_jobless_executions_and_unknown_assistant_calls(self):
        rows = [
            ("review-exec-no-job", "execution", {"status": "pending_admission", "job_id": None}),
            ("review-exec-unknown", "execution", {"status": "unknown", "job_id": None}),
            ("review-running-turn", "turn", {"status": "running", "assistant_run": {"fence": 9}}),
            ("review-preflight", "preflight", {"status": "ready", "expires_at": 9999999999}),
        ]
        with self.repo.engine.begin() as conn:
            for ident, kind, payload in rows:
                conn.execute(insert(objects).values(id=ident, tenant="sixnine", owner="superdan",
                    session_id="review-session", kind=kind, payload=payload,
                    version=1, created_at=1, updated_at=1))
        from pathlib import Path
        database = Path(self.temp.name) / "ledger.sqlite3"
        _quarantine_restored_database(database)
        with contextlib.closing(sqlite3.connect(database)) as db:
            result = {ident: json.loads(raw) for ident, raw in db.execute(
                "SELECT id,payload FROM platform_quick_chat_objects WHERE id LIKE 'review-%'")}
        for ident in ("review-exec-no-job", "review-exec-unknown"):
            self.assertEqual(result[ident]["status"], "recovery_hold")
            self.assertIsNone(result[ident]["job_id"])
        self.assertEqual(result["review-running-turn"]["status"], "unknown")
        self.assertEqual(result["review-running-turn"]["assistant_run"]["fence"], 10)
        self.assertEqual(result["review-preflight"]["status"], "blocked")
        self.assertEqual(result["review-preflight"]["expires_at"], 0)
