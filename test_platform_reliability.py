"""Authorization-before-pagination and disaster recovery holds, temporary DBs."""
from pathlib import Path

from sqlalchemy import event, select, update

from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.queue import TaskQueue
from studio_platform.repository import (Conflict, InvalidTransition, LeaseLost, Scope,
    attempts, budget_reservations, jobs, plans, registered_workers)
from test_platform_repository import LedgerCase


class ReliabilityTests(LedgerCase):
    def test_100_large_project_catalog_summaries_use_scalar_sql_without_drafts(self):
        scope = Scope(self.scope.tenant_id, self.scope.owner_id, "__projects")
        for i in range(100):
            self.repo.put_document(scope, "project", "project-"+str(i).zfill(3),
                {"title": "故事"+str(i), "private_draft": "large-private-prompt-"+"x"*(256*1024)})
        self.repo.put_document(Scope(scope.tenant_id, "supervan", scope.project_id), "project", "project-000",
            {"title": "other-owner", "private_draft": "foreign"})
        self.repo.put_document(Scope("other-tenant", scope.owner_id, scope.project_id), "project", "project-000",
            {"title": "other-tenant", "private_draft": "foreign"})
        queries, projection = [], []
        def capture(connection, cursor, statement, parameters, context, executemany):
            queries.append(statement)
            sql = context.compiled.statement if context.compiled is not None else None
            if sql is not None and getattr(sql, "is_select", False):
                projection.extend(sql.selected_columns.keys())
        event.listen(self.repo.engine, "before_cursor_execute", capture)
        try:
            values = self.repo.list_documents(scope, "project", summary=True, limit=100)
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", capture)
        self.assertEqual(len(queries), 1)
        self.assertNotIn("payload", projection, "The selected JSON column must never contain the whole project")
        self.assertIn("_title", projection)
        self.assertEqual(len(values), 100)
        self.assertEqual([row["payload"] for row in values], [{"title": "故事"+str(i)} for i in range(100)])
        self.assertTrue(all(row["owner_id"] == scope.owner_id and row["tenant_id"] == scope.tenant_id for row in values))
        self.assertNotIn("private_draft", repr(values))
        # This is a read projection: source documents are not truncated/rebuilt.
        full = self.repo.get_document(scope, "project", "project-000")
        self.assertIn("large-private-prompt-", full["payload"]["private_draft"])
        self.assertEqual(self.repo.list_documents(scope, "project", limit=1)[0], full)

    def test_project_summary_authorization_before_pagination_and_strict_opt_in(self):
        scope = Scope(self.scope.tenant_id, self.scope.owner_id, "__projects")
        for key in ("a-excluded", "b-excluded", "x-allowed", "z-allowed"):
            self.repo.put_document(scope, "project", key, {"title": key, "private_draft": "hidden"})
        self.repo.put_document(scope, "note", "x-allowed", {"title": "wrong-kind"})
        allowed = ("x-allowed", "z-allowed")
        for offset, expected in enumerate(allowed):
            rows = self.repo.list_documents(scope, "project", allowed_ids=allowed,
                limit=1, offset=offset, summary=True)
            self.assertEqual([row["document_id"] for row in rows], [expected])
            self.assertEqual(rows[0]["payload"], {"title": expected})
        self.assertEqual(self.repo.list_documents(scope, "project", allowed_ids=(), summary=True), [])
        for invalid in (1, "true", None):
            with self.assertRaisesRegex(ValueError, "invalid_document_summary"):
                self.repo.list_documents(scope, "project", summary=invalid)
        with self.assertRaisesRegex(ValueError, "invalid_document_summary"):
            self.repo.list_documents(scope, "note", summary=True)

    def test_project_catalog_excludes_purged_content_before_pagination_without_erasing_history(self):
        scope = Scope(self.scope.tenant_id, self.scope.owner_id, "__projects")
        source = {
            "a-purged": {"id": "a-purged", "title": "", "content_deleted": True},
            "b-managed": {"title": "Managed", "integration_kind": "quick_chat"},
            "c-active": {"title": "Active", "content_deleted": False},
            "d-null-title": {"title": None},
            "e-missing-title": {},
        }
        originals = {key: self.repo.put_document(scope, "project", key, payload)
                     for key, payload in source.items()}
        for foreign_scope in (Scope(scope.tenant_id, "supervan", scope.project_id),
                              Scope("other-tenant", scope.owner_id, scope.project_id)):
            self.repo.put_document(foreign_scope, "project", "a-purged", {"title": "Foreign live story"})
            self.repo.put_document(foreign_scope, "project", "c-active", {"content_deleted": True})
        allowed = ("a-purged", "c-active", "d-null-title", "e-missing-title")
        for summary in (False, True):
            for offset, expected in enumerate(("c-active", "d-null-title", "e-missing-title")):
                rows = self.repo.list_documents(scope, "project", exclude_managed=True,
                    summary=summary, allowed_ids=allowed, limit=1, offset=offset)
                self.assertEqual([row["document_id"] for row in rows], [expected])
                self.assertEqual(rows[0]["payload"].get("title"), source[expected].get("title"))
        self.assertEqual(self.repo.list_documents(scope, "project", exclude_managed=True,
            allowed_ids=("a-purged",), limit=1), [])
        for key, original in originals.items():
            self.assertEqual(self.repo.get_document(scope, "project", key), original)
        self.assertEqual(len(self.repo.list_documents(scope, "project")), len(source))

    def test_job_project_authorization_precedes_limit_offset_and_preserves_summaries(self):
        authorized = self.scope
        excluded = Scope(self.scope.tenant_id, self.scope.owner_id, "excluded-project")
        first = self.plan(authorized, cost=0)
        expected = [self.repo.create_job(authorized, first["id"], "permitted-"+str(i))["id"] for i in range(3)]
        self.now += 10
        newer = self.plan(excluded, cost=0)
        for i in range(110):
            self.repo.create_job(excluded, newer["id"], "excluded-"+str(i))
        self.repo.create_job(self.other, self.plan(self.other, cost=0)["id"], "other-owner")
        different_tenant = Scope("other-tenant", authorized.owner_id, authorized.project_id)
        self.repo.create_job(different_tenant, self.plan(different_tenant, cost=0)["id"], "other-tenant")
        records = []
        for offset in range(3):
            records += self.repo.list_jobs_for_owner(authorized.tenant_id, authorized.owner_id,
                project_ids=(authorized.project_id,), limit=1, offset=offset, summary=True)
        self.assertEqual({row["id"] for row in records}, set(expected))
        self.assertTrue(all(row["project_id"] == authorized.project_id for row in records))
        self.assertEqual(self.repo.list_jobs_for_owner(authorized.tenant_id, authorized.owner_id,
            project_id=excluded.project_id, project_ids=(authorized.project_id,), summary=True), [])
        self.assertEqual(self.repo.list_jobs_for_owner(authorized.tenant_id, authorized.owner_id,
            project_ids=(), summary=True), [])
        self.assertEqual(len(self.repo.list_jobs_for_owner(authorized.tenant_id, authorized.owner_id,
            project_ids=None, limit=100)), 100)

    def test_document_allowed_ids_precede_pagination_and_exact_scope(self):
        scope = Scope(self.scope.tenant_id, self.scope.owner_id, "__projects")
        for i in range(110):
            self.repo.put_document(scope, "project", "excluded-"+str(i).zfill(3), {"title": "excluded"})
        for doc in ("z-permitted-1", "z-permitted-2"):
            self.repo.put_document(scope, "project", doc, {"title": "permitted"})
            self.repo.put_document(Scope(scope.tenant_id, "supervan", scope.project_id), "project", doc, {"title": "other"})
            self.repo.put_document(Scope("other-tenant", scope.owner_id, scope.project_id), "project", doc, {"title": "other"})
        allowed = ("z-permitted-1", "z-permitted-2")
        self.assertEqual([row["document_id"] for row in self.repo.list_documents(scope, "project",
            allowed_ids=allowed, limit=1, offset=0)], [allowed[0]])
        self.assertEqual([row["document_id"] for row in self.repo.list_documents(scope, "project",
            allowed_ids=allowed, limit=1, offset=1)], [allowed[1]])
        self.assertEqual(self.repo.list_documents(scope, "project", allowed_ids=(), limit=100), [])

    def test_empty_authorization_is_not_none_and_invalid_sets_fail_closed(self):
        queries = []
        def record(*args):
            queries.append(args[2])
        event.listen(self.repo.engine, "before_cursor_execute", record)
        try:
            for allowed in ([], (), set(), frozenset()):
                self.assertEqual(self.repo.list_jobs_for_owner(self.scope.tenant_id, self.scope.owner_id,
                    project_ids=allowed), [])
                self.assertEqual(self.repo.list_documents(self.scope, "project", allowed_ids=allowed), [])
            for bad in ("project-1", ["good", None], ["x"]*4097):
                with self.assertRaises(ValueError):
                    self.repo.list_jobs_for_owner(self.scope.tenant_id, self.scope.owner_id, project_ids=bad)
                with self.assertRaises(ValueError):
                    self.repo.list_documents(self.scope, "project", allowed_ids=bad)
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", record)
        self.assertEqual(queries, [])

    def _hold(self, job):
        # Same quarantine fields used by backup restore; PG/SQLite tests cover
        # core behavior without touching any actual backup or production DB.
        with self.repo.transaction() as conn:
            execution = dict(job["execution_plan"])
            execution["enabled"] = False
            conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="recovery_hold",
                error_code="disaster_recovery_review_required", fence=jobs.c.fence+1,
                lease_worker_id=None, lease_expires_at=None, execution_plan=execution))
            conn.execute(update(plans).where(plans.c.id == job["plan_id"]).values(expires_at=0,
                execution_plan=execution))

    def test_held_unsubmitted_retry_cancel_and_enqueue_never_regenerate_or_release_money(self):
        job = self.job(key="held-zero-attempt")
        self._hold(job)
        before = self.repo.get_budget("owner-budget")
        for _ in range(2):
            response = self.repo.request_cancel(self.scope, job["id"])
            self.assertEqual(response["status"], "recovery_hold")
            self.assertTrue(response["result"]["recovery_cancel_requested"])
            retry = self.repo.create_job(self.scope, job["plan_id"], "held-zero-attempt")
            self.assertFalse(retry["created"])
            self.assertEqual(retry["id"], job["id"])
            with self.assertRaises(InvalidTransition):
                self.repo.enqueue(self.scope, job["id"], budget_account_ids=("owner-budget",))
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        for purpose in ("generate", "collect", "reconcile"):
            self.assertIsNone(TaskQueue(self.repo).claim("worker", "test-pool", purpose=purpose))
        with self.repo.engine.connect() as conn:
            self.assertEqual(len(list(conn.execute(select(budget_reservations)))), 1)
            self.assertEqual(len(list(conn.execute(select(attempts)))), 0)

    def test_held_submitted_attempt_keeps_original_upstream_cost_and_fences_old_worker(self):
        queue = TaskQueue(self.repo)
        job = self.job(key="held-running")
        claim = queue.claim("worker", "test-pool")
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "original-upstream-id")
        self._hold(self.repo.get_job(self.scope, job["id"]))
        self.repo.request_cancel(self.scope, job["id"])
        for operation in (lambda: queue.heartbeat(claim.lease),
                          lambda: queue.record_submitted(claim.lease, "another-upstream-id"),
                          lambda: queue.fail(claim.lease, "cancelled", actual_cost_microusd=0, upstream_stopped=True)):
            with self.assertRaises(LeaseLost):
                operation()
        for purpose in ("generate", "collect", "reconcile"):
            self.assertIsNone(queue.claim("new-worker", "test-pool", purpose=purpose))
        attempt = queue.get_attempt(self.scope, job["id"])
        self.assertEqual(attempt["upstream_task_id"], "original-upstream-id")
        self.assertIsNone(attempt["actual_cost_microusd"])
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000)
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 0)

    def test_held_job_cannot_renew_even_an_accidentally_retained_matching_lease(self):
        queue = TaskQueue(self.repo)
        job = self.job()
        claim = queue.claim("worker", "test-pool")
        # Defense independent of the restore operation incrementing fences.
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="recovery_hold"))
        with self.assertRaisesRegex(LeaseLost, "operator_review"):
            queue.heartbeat(claim.lease)
        with self.assertRaisesRegex(LeaseLost, "operator_review"):
            queue.defer_unsubmitted(claim.lease)

    def test_control_observe_hold_remains_unknown_and_occupied_not_healthy_capacity(self):
        control = WorkerControl(self.repo)
        spec = WorkerSpec("mock-worker", "test-pool", "mock", "mock-instance", ("mock-device",), (),
            "SIMULATION", "mock-config", backend="mock")
        control.register(spec)
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        job = self.repo.create_job(self.scope, self.plan(cost=0, execution={"pool": "test-pool", "backend": "mock",
            "enabled": True})["id"], "held-control")
        claim = control.claim(spec.worker_id, spec.pool)
        self.assertIsNotNone(claim)
        self._hold(job)
        observed = control.observe(spec.worker_id, job["id"])
        self.assertEqual(observed["state"], "unknown")
        self.assertEqual(observed["current_job_id"], job["id"])
        with self.assertRaises(Conflict):
            control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        self.assertIsNone(control.claim(spec.worker_id, spec.pool))

    def test_real_sqlite_restore_quarantine_compatible_with_cancel_and_all_claim_phases(self):
        if self.repo.engine.dialect.name != "sqlite":
            self.skipTest("local restore uses SQLite; PG core hold behavior covered separately")
        from studio_platform.backup import _quarantine_restored_database
        queue = TaskQueue(self.repo)
        job = self.job()
        claim = queue.claim("worker", "test-pool")
        queue.begin_submission(claim.lease)
        queue.record_submitted(claim.lease, "original-upstream-id")
        before = self.repo.get_budget("owner-budget")
        held = _quarantine_restored_database(Path(self.repo.engine.url.database))
        self.assertEqual(held, [{"job_id": job["id"], "previous_status": "running"}])
        result = self.repo.request_cancel(self.scope, job["id"])
        self.assertEqual(result["status"], "recovery_hold")
        self.assertFalse(result["execution_plan"]["enabled"])
        for purpose in ("generate", "collect", "reconcile"):
            self.assertIsNone(queue.claim("worker", "test-pool", purpose=purpose))
        self.assertEqual(self.repo.get_budget("owner-budget"), before)

    def test_erroneously_requeued_submitted_attempt_is_held_instead_of_double_submission(self):
        queue = TaskQueue(self.repo)
        original = self.job()
        claimed = queue.claim("worker", "test-pool")
        queue.begin_submission(claimed.lease)
        queue.record_submitted(claimed.lease, "original-upstream-id")
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == original["id"]).values(
                status="queued", lease_worker_id=None, lease_expires_at=None))
        # Make the suspect job the first candidate. An identical fake clock
        # otherwise leaves this assertion dependent on random UUID ordering:
        # claim legitimately returns the newer candidate before visiting it.
        self.now += 1
        next_job = self.job(cost=0)
        self.assertEqual(queue.claim("other-worker", "test-pool").job["id"], next_job["id"])
        held = self.repo.get_job(self.scope, original["id"])
        self.assertEqual(held["status"], "recovery_hold")
        self.assertEqual(held["attempt_no"], 1)
        self.assertEqual(queue.get_attempt(self.scope, original["id"])["upstream_task_id"], "original-upstream-id")
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000)
        self.assertIsNone(queue.claim("third-worker", "test-pool", purpose="generate"))
        self.assertEqual(self.repo.get_job(self.scope, original["id"])["attempt_no"], 1)

    def test_expired_claim_with_submission_evidence_never_returns_to_generation_queue(self):
        queue = TaskQueue(self.repo)
        original = self.job()
        claimed = queue.claim("worker", "test-pool", lease_seconds=1)
        queue.begin_submission(claimed.lease)
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == original["id"]).values(status="claimed"))
        self.now += 2
        recovered = queue.recover_expired(summary=True)[0]
        self.assertEqual(recovered["status"], "submission_unknown")
        self.assertNotIn("request", recovered)
        self.assertIsNone(queue.claim("other-worker", "test-pool", purpose="generate"))
        same = queue.claim("other-worker", "test-pool", purpose="reconcile")
        self.assertEqual(same.lease.attempt_id, claimed.lease.attempt_id)
        self.assertEqual(same.job["attempt_no"], 1)

    def test_cancel_erroneously_requeued_submitted_attempt_preserves_real_cost_evidence(self):
        queue = TaskQueue(self.repo)
        original = self.job()
        claimed = queue.claim("worker", "test-pool")
        queue.begin_submission(claimed.lease)
        queue.record_submitted(claimed.lease, "original-upstream-id")
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == original["id"]).values(
                status="queued", lease_worker_id=None, lease_expires_at=None))
        before = self.repo.get_budget("owner-budget")
        result = self.repo.request_cancel(self.scope, original["id"])
        self.assertEqual(result["status"], "recovery_hold")
        self.assertTrue(result["result"]["recovery_cancel_requested"])
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        self.assertEqual(queue.get_attempt(self.scope, original["id"])["upstream_task_id"], "original-upstream-id")
        self.assertEqual(queue.get_attempt(self.scope, original["id"])["status"], "running")
        for purpose in ("generate", "reconcile", "collect"):
            self.assertIsNone(queue.claim("other-worker", "test-pool", purpose=purpose))
        self.assertEqual(self.repo.request_cancel(self.scope, original["id"]), result)

    def test_cancel_claimed_missing_attempt_proof_never_assumes_zero_cost(self):
        queue = TaskQueue(self.repo)
        original = self.job()
        claimed = queue.claim("worker", "test-pool")
        with self.repo.transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == original["id"]).values(
                current_attempt_id="missing-attempt-evidence"))
        before = self.repo.get_budget("owner-budget")
        held = self.repo.request_cancel(self.scope, original["id"])
        self.assertEqual(held["status"], "recovery_hold")
        self.assertTrue(held["result"]["recovery_cancel_requested"])
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        with self.assertRaises(LeaseLost):
            queue.fail(claimed.lease, "failure", actual_cost_microusd=0)

    def test_100_expired_large_jobs_recover_using_only_scalar_projection(self):
        queue = TaskQueue(self.repo)
        plan = self.repo.create_plan(self.scope, {"hidden_sources": "x"*(256*1024)},
            {"pool": "test-pool"}, expires_at=self.now+1000)
        for i in range(100):
            self.repo.create_job(self.scope, plan["id"], "large-expired-"+str(i))
            queue.claim("worker-"+str(i), "test-pool", lease_seconds=1)
        self.now += 2
        selected_columns = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            sql = context.compiled.statement if context.compiled is not None else None
            if sql is not None and getattr(sql, "is_select", False):
                selected_columns.extend(sql.selected_columns.keys())
        event.listen(self.repo.engine, "before_cursor_execute", capture)
        try:
            recovered = queue.recover_expired(summary=True)
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", capture)
        self.assertEqual(len(recovered), 100)
        self.assertTrue(all(row["status"] == "queued" and "request" not in row for row in recovered))
        self.assertNotIn("request", selected_columns)
        self.assertNotIn("execution_plan", selected_columns)
