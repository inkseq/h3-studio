from studio_platform.queue import TaskQueue
from studio_platform.repository import Conflict, LeaseLost, Scope
from test_platform_repository import LedgerCase


VALID_ARTIFACT = {
    "kind": "video",
    "object_key": "test/results/output.mp4",
    "size_bytes": 100,
    "sha256": "a" * 64,
    "validated": True,
}


class QueueTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.queue = TaskQueue(self.repo)

    def running(self):
        self.job()
        claim = self.queue.claim("worker", "test-pool")
        self.queue.begin_submission(claim.lease)
        self.queue.record_submitted(claim.lease, "upstream-test-task")
        return claim

    def test_concurrent_claim_only_one_worker(self):
        job = self.job()
        results = self.parallel(
            lambda i: self.queue.claim("worker-" + str(i), "test-pool"), 12
        )
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)

    def test_claims_many_jobs_are_unique(self):
        for _ in range(12):
            self.job()
        results = self.parallel(
            lambda i: self.queue.claim("worker-" + str(i), "test-pool"), 12
        )
        self.assertEqual(len({r.job["id"] for r in results}), 12)

    def test_bulk_scheduling_reads_only_scalars_and_loads_selected_snapshot(self):
        from sqlalchemy import event

        plan = self.plan(cost=0, prompt="snapshot-" + "x" * 16384)
        for i in range(64):
            self.repo.create_job(self.scope, plan["id"], "large-snapshot-" + str(i))
        selects = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            compiled = context.compiled
            sql = compiled.statement if compiled is not None else None
            if sql is not None and getattr(sql, "is_select", False):
                columns = set(sql.selected_columns.keys())
                if "request" in columns and "pool" in columns:
                    selects.append(statement)

        event.listen(self.repo.engine, "before_cursor_execute", capture)
        try:
            claim = self.queue.claim("scalar-worker", "test-pool")
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", capture)
        self.assertIsNotNone(claim)
        self.assertTrue(claim.job["request"]["prompt"].startswith("snapshot-"))
        self.assertEqual(
            len(selects), 2
        )  # One row lock/read plus final one-row returned job.
        self.assertTrue(all("platform_jobs.id =" in sql for sql in selects))

    def test_selected_candidate_rechecks_cancelled_state_before_new_attempt(self):
        from sqlalchemy import update
        from studio_platform.repository import jobs
        from unittest.mock import patch

        first, second = self.job(cost=0), self.job(cost=0)
        original = self.repo._job
        seen = []

        def cancel_between_selection_and_row_lock(
            connection, job_id, scope=None, *, lock=False
        ):
            if lock and not seen:
                seen.append(job_id)
                # Simulates the READ COMMITTED result of a separately committed
                # cancellation without a second nested SQLite write transaction.
                connection.execute(
                    update(jobs).where(jobs.c.id == job_id).values(status="cancelled")
                )
            return original(connection, job_id, scope, lock=lock)

        with patch.object(
            self.repo, "_job", side_effect=cancel_between_selection_and_row_lock
        ):
            claim = self.queue.claim("scalar-worker", "test-pool")
        self.assertNotEqual(claim.job["id"], seen[0])
        self.assertEqual(self.repo.get_job(self.scope, seen[0])["attempt_no"], 0)
        self.assertEqual(claim.job["attempt_no"], 1)

    def test_owner_fairness_assigns_other_user_next_slot(self):
        for _ in range(5):
            self.job()
        self.now += 1
        self.job(scope=self.other, cost=0)
        first = self.queue.claim("worker-a", "test-pool")
        second = self.queue.claim("worker-b", "test-pool")
        self.assertEqual(
            {first.job["owner_id"], second.job["owner_id"]}, {"superdan", "supervan"}
        )

    def test_long_wait_is_not_starved_by_new_owner_with_less_historical_work(self):
        first = self.running()
        self.queue.fail(
            first.lease,
            "finished-failure",
            actual_cost_microusd=0,
            upstream_stopped=True,
        )
        old = self.job()
        self.now += 901
        self.job(scope=self.other, cost=0)
        next_job = self.queue.claim("worker", "test-pool")
        self.assertEqual(next_job.job["id"], old["id"])

    def test_owner_tuple_keys_do_not_collide(self):
        self.assertNotEqual(
            self.queue._owner_key({"tenant_id": "a:b", "owner_id": "c"}),
            self.queue._owner_key({"tenant_id": "a", "owner_id": "b:c"}),
        )

    def _assert_aged_owner_job_precedes_fresh_without_changing_existing_attempt(
        self, *, held
    ):
        from sqlalchemy import update
        from studio_platform.repository import jobs

        existing = self.running()
        if held:
            # A restored/administratively held attempt keeps its upstream and
            # reservation evidence; it is never itself a generation candidate.
            with self.repo.transaction() as connection:
                connection.execute(
                    update(jobs)
                    .where(jobs.c.id == existing.job["id"])
                    .values(
                        status="recovery_hold",
                        lease_worker_id=None,
                        lease_expires_at=None,
                        fence=existing.lease.fence + 1,
                    )
                )
        waiting = self.job()
        self.now += 900
        self.job(scope=self.other, cost=0)
        before_job = self.repo.get_job(self.scope, existing.job["id"])
        before_attempt = self.queue.get_attempt(self.scope, existing.job["id"])
        before_budget = self.repo.get_budget("owner-budget")
        claimed = self.queue.claim("another-free-slot", "test-pool")
        self.assertEqual(claimed.job["id"], waiting["id"])
        self.assertEqual(self.repo.get_job(self.scope, existing.job["id"]), before_job)
        self.assertEqual(
            self.queue.get_attempt(self.scope, existing.job["id"]), before_attempt
        )
        self.assertEqual(self.repo.get_budget("owner-budget"), before_budget)
        self.assertEqual(before_budget["reserved_microusd"], 200_000)

    def test_aged_job_precedes_fresh_owner_despite_recovery_hold(self):
        self._assert_aged_owner_job_precedes_fresh_without_changing_existing_attempt(
            held=True
        )

    def test_aged_job_precedes_fresh_owner_despite_running_attempt(self):
        self._assert_aged_owner_job_precedes_fresh_without_changing_existing_attempt(
            held=False
        )

    def test_fresh_job_just_below_aging_threshold_keeps_owner_fairness(self):
        self.running()
        self.job()
        self.now += 899.999
        fresh = self.job(scope=self.other, cost=0)
        claimed = self.queue.claim("another-free-slot", "test-pool")
        self.assertEqual(claimed.job["id"], fresh["id"])

    def test_fresh_jobs_with_equal_occupancy_prefer_less_historical_work(self):
        previous = self.running()
        self.queue.fail(
            previous.lease,
            "test-finished",
            actual_cost_microusd=0,
            upstream_stopped=True,
        )
        self.job()
        self.now += 1
        fresh = self.job(scope=self.other, cost=0)
        claimed = self.queue.claim("free-slot", "test-pool")
        self.assertEqual(claimed.job["id"], fresh["id"])

    def test_aged_jobs_are_fifo_then_id_even_with_different_owner_occupancy(self):
        self.running()
        oldest = self.job()
        self.now += 1
        tied = [self.job(scope=self.other, cost=0), self.job()]
        self.now += 900
        expected = [oldest["id"], *sorted(job["id"] for job in tied)]
        claimed = [
            self.queue.claim("free-slot-" + str(i), "test-pool").job["id"]
            for i in range(3)
        ]
        self.assertEqual(claimed, expected)

    def test_expired_pre_submit_can_requeue_old_worker_fenced(self):
        job = self.job()
        first = self.queue.claim("worker-a", "test-pool", lease_seconds=10)
        self.now += 11
        self.assertEqual(self.queue.recover_expired()[0]["status"], "queued")
        second = self.queue.claim("worker-b", "test-pool")
        self.assertEqual(second.job["id"], job["id"])
        self.assertEqual(second.job["attempt_no"], 2)
        with self.assertRaises(LeaseLost):
            self.queue.begin_submission(first.lease)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def test_submission_intent_only_once_and_expiry_unknown_never_reposts(self):
        self.job()
        claim = self.queue.claim("worker-a", "test-pool", lease_seconds=10)
        self.queue.begin_submission(claim.lease)
        with self.assertRaises(Conflict):
            self.queue.begin_submission(claim.lease)
        self.now += 11
        self.assertEqual(
            self.queue.recover_expired()[0]["status"], "submission_unknown"
        )
        self.assertIsNone(self.queue.claim("worker-b", "test-pool"))
        reconciler = self.queue.claim("reconcile", "test-pool", purpose="reconcile")
        with self.assertRaises(Conflict):
            self.queue.begin_submission(reconciler.lease)
        self.queue.record_submitted(reconciler.lease, "found-existing-task")
        self.assertEqual(
            self.queue.get_attempt(self.scope, claim.job["id"])["upstream_task_id"],
            "found-existing-task",
        )
        self.assertEqual(
            self.repo.get_job(self.scope, claim.job["id"])["attempt_no"], 1
        )

    def test_unknown_response_preserves_budget_and_requires_reconciliation(self):
        self.job()
        claim = self.queue.claim("worker", "test-pool")
        self.queue.begin_submission(claim.lease)
        self.queue.mark_submission_unknown(claim.lease)
        self.assertIsNone(self.queue.claim("worker-again", "test-pool"))
        with self.assertRaises(LeaseLost):
            self.queue.record_submitted(claim.lease, "late-response")
        reconcile = self.queue.claim("poller", "test-pool", purpose="reconcile")
        self.queue.record_submitted(reconcile.lease, "reconciled-task")
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def test_running_expiry_retains_task_and_never_generates_again(self):
        claim = self.running()
        self.now += 100
        self.assertEqual(self.queue.recover_expired()[0]["status"], "running")
        self.assertIsNone(self.queue.claim("other", "test-pool"))
        reconciler = self.queue.claim("poller", "test-pool", purpose="reconcile")
        self.queue.begin_collection(reconciler.lease)
        self.queue.complete(
            reconciler.lease, [VALID_ARTIFACT], actual_cost_microusd=80_000
        )
        self.assertEqual(
            self.repo.get_job(self.scope, claim.job["id"])["attempt_no"], 1
        )

    def test_collection_retry_uses_original_attempt_and_fences_stale_result(self):
        claim = self.running()
        self.queue.begin_collection(claim.lease)
        self.queue.collection_failed(claim.lease, retry_after_s=2)
        self.assertIsNone(self.queue.claim("generator", "test-pool"))
        self.assertIsNone(self.queue.claim("collector", "test-pool", purpose="collect"))
        self.now += 3
        retry = self.queue.claim("collector", "test-pool", purpose="collect")
        self.assertEqual(retry.lease.attempt_id, claim.lease.attempt_id)
        self.assertGreater(retry.lease.fence, claim.lease.fence)
        with self.assertRaises(LeaseLost):
            self.queue.complete(
                claim.lease, [VALID_ARTIFACT], actual_cost_microusd=80_000
            )
        done = self.queue.complete(
            retry.lease, [VALID_ARTIFACT], actual_cost_microusd=80_000
        )
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["attempt_no"], 1)
        self.assertEqual(
            self.queue.get_attempt(self.scope, done["id"])["collection_failures"], 1
        )
        self.assertEqual(len(self.repo.list_artifacts(self.scope, done["id"])), 1)
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 80_000)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)
        with self.assertRaises(Conflict):
            self.queue.complete(
                retry.lease, [VALID_ARTIFACT], actual_cost_microusd=80_000
            )

    def test_verified_output_available_while_billing_pending_then_settles_once(self):
        claim = self.running()
        self.queue.begin_collection(claim.lease)
        done = self.queue.complete(
            claim.lease, [VALID_ARTIFACT], actual_cost_microusd=None
        )
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["result"]["billing_status"], "pending")
        self.assertEqual(len(self.repo.list_artifacts(self.scope, done["id"])), 1)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 0
        )  # Invoice remains pending; terminal admission hold is released.
        settled = self.repo.settle_completed_job(
            self.scope, done["id"], actual_cost_microusd=80_000
        )
        self.assertEqual(settled["result"]["billing_status"], "settled")
        self.repo.settle_completed_job(
            self.scope, done["id"], actual_cost_microusd=80_000
        )
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 80_000)
        with self.assertRaises(Conflict):
            self.repo.settle_completed_job(
                self.scope, done["id"], actual_cost_microusd=90_000
            )

    def test_release_keeps_stage_and_unsubmitted_defer_never_submits(self):
        self.job()
        claim = self.queue.claim("worker", "test-pool")
        deferred = self.queue.defer_unsubmitted(claim.lease, retry_after_s=0)
        self.assertEqual(deferred["status"], "queued")
        with self.assertRaises(LeaseLost):
            self.queue.begin_submission(claim.lease)
        claim = self.queue.claim("worker", "test-pool")
        self.queue.begin_submission(claim.lease)
        self.queue.record_submitted(claim.lease, "task")
        released = self.queue.release(claim.lease, retry_after_s=0)
        self.assertEqual(released["status"], "running")
        self.assertIsNone(self.queue.claim("generator", "test-pool"))
        self.assertEqual(
            self.queue.claim(
                "poller", "test-pool", purpose="reconcile"
            ).lease.attempt_id,
            claim.lease.attempt_id,
        )

    def test_heartbeat_extends_lease_but_expired_heartbeat_rejected(self):
        self.job()
        claim = self.queue.claim("worker", "test-pool", lease_seconds=10)
        self.now += 5
        extended = self.queue.heartbeat(claim.lease, lease_seconds=20)
        self.assertEqual(extended.fence, claim.lease.fence)
        self.now += 10
        self.assertEqual(self.queue.recover_expired(), [])
        self.now += 11
        with self.assertRaises(LeaseLost):
            self.queue.heartbeat(extended)

    def test_cancel_queued_or_claimed_releases_and_invalidates_worker(self):
        queued = self.job()
        self.assertEqual(
            self.repo.request_cancel(self.scope, queued["id"])["status"], "cancelled"
        )
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)
        claimed_job = self.job()
        claim = self.queue.claim("worker", "test-pool")
        self.repo.request_cancel(self.scope, claimed_job["id"])
        with self.assertRaises(LeaseLost):
            self.queue.begin_submission(claim.lease)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)

    def test_cancel_running_holds_reservation_until_confirmed_cost(self):
        claim = self.running()
        job = self.repo.request_cancel(self.scope, claim.job["id"])
        self.assertEqual(job["status"], "cancel_requested")
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )
        with self.assertRaises(Conflict):
            self.queue.confirm_cancel(claim.lease)
        cancelled = self.queue.confirm_cancel(
            claim.lease, upstream_stopped=True, actual_cost_microusd=30_000
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.repo.get_budget("owner-budget")["spent_microusd"], 30_000)

    def test_cancel_response_unknown_does_not_resubmit_or_release(self):
        self.job()
        claim = self.queue.claim("worker", "test-pool")
        self.queue.begin_submission(claim.lease)
        self.repo.request_cancel(self.scope, claim.job["id"])
        unknown = self.queue.mark_submission_unknown(claim.lease)
        self.assertEqual(unknown["status"], "cancel_requested")
        self.assertEqual(unknown["cancel_from_status"], "submission_unknown")
        self.assertIsNone(self.queue.claim("retry-generator", "test-pool"))
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def test_cancel_too_late_preserves_completed_candidate_and_actual_cost(self):
        claim = self.running()
        self.repo.request_cancel(self.scope, claim.job["id"])
        self.queue.begin_collection(claim.lease)
        completed = self.queue.complete(
            claim.lease, [VALID_ARTIFACT], actual_cost_microusd=100_000
        )
        self.assertEqual(completed["status"], "succeeded")
        self.assertTrue(completed["result"]["completed_after_cancel_request"])
        self.assertEqual(len(self.repo.list_artifacts(self.scope, claim.job["id"])), 1)

    def test_unknown_submission_cannot_be_failed_and_freed_without_proof(self):
        self.job()
        claim = self.queue.claim("worker", "test-pool")
        self.queue.begin_submission(claim.lease)
        with self.assertRaises(Conflict):
            self.queue.fail(claim.lease, "timeout", actual_cost_microusd=0)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def test_unsubmitted_failure_zero_cost_and_actual_overrun_not_hidden(self):
        job = self.job()
        claim = self.queue.claim("worker", "test-pool")
        with self.assertRaises(Conflict):
            self.queue.fail(claim.lease, "bad-input", actual_cost_microusd=1)
        self.queue.fail(claim.lease, "bad-input", actual_cost_microusd=0)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "failed")
        claim = self.running()
        self.queue.begin_collection(claim.lease)
        self.queue.complete(claim.lease, [VALID_ARTIFACT], actual_cost_microusd=150_000)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["spent_microusd"], 150_000
        )

    def test_unvalidated_or_signed_url_artifact_does_not_complete(self):
        claim = self.running()
        self.queue.begin_collection(claim.lease)
        for spec in (
            {**VALID_ARTIFACT, "validated": False},
            {
                **VALID_ARTIFACT,
                "signed_url": "https://example.invalid/file?signature=test",
            },
            {
                **VALID_ARTIFACT,
                "object_key": "https://example.invalid/file?signature=test",
            },
            {**VALID_ARTIFACT, "object_key": "../outside.mp4"},
        ):
            with self.assertRaises(ValueError):
                self.queue.complete(claim.lease, [spec], actual_cost_microusd=80_000)
        self.assertEqual(
            self.repo.get_job(self.scope, claim.job["id"])["status"], "collecting"
        )
        self.assertEqual(self.repo.list_artifacts(self.scope, claim.job["id"]), [])

    def test_wrong_worker_or_owner_cannot_access_attempt(self):
        from dataclasses import replace

        claim = self.running()
        with self.assertRaises(LeaseLost):
            self.queue.heartbeat(replace(claim.lease, worker_id="intruder"))
        from studio_platform.repository import NotFound

        with self.assertRaises(NotFound):
            self.queue.get_attempt(self.other, claim.job["id"])
