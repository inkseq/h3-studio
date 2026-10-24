"""Same-job lost-capacity recovery on disposable ledgers; no provider calls."""

import copy
from unittest.mock import patch

from sqlalchemy import insert, select, update

from studio_platform.capacity_reclaim import authorized_lost_attempt, history
from studio_platform.queue import TaskQueue
from studio_platform.repository import (
    Conflict,
    InvalidTransition,
    LeaseLost,
    NotFound,
    attempts,
    artifacts,
    budget_reservations,
    jobs,
    outbox,
)
from test_platform_repository import LedgerCase
from test_platform_queue import VALID_ARTIFACT


class CapacityReclaimTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.queue = TaskQueue(self.repo)

    def running(self, job=None, worker="worker"):
        job = self.job() if job is None else job
        claim = self.queue.claim(worker, "test-pool", job_ids=[job["id"]])
        self.queue.begin_submission(claim.lease)
        self.queue.record_submitted(claim.lease, "upstream-" + claim.lease.attempt_id)
        return claim

    def reclaim(self, claim, **changes):
        args = dict(
            expected_attempt_id=claim.lease.attempt_id,
            expected_fence=claim.lease.fence,
            reason="capacity_owner_recovery",
            evidence_id="proof-original-" + claim.lease.attempt_id,
        )
        args.update(changes)
        return self.queue.reclaim_capacity(self.scope, claim.lease.job_id, **args)

    def attempt(self, ident):
        with self.repo.engine.connect() as conn:
            return dict(
                conn.execute(select(attempts).where(attempts.c.id == ident))
                .mappings()
                .one()
            )

    def holds(self):
        with self.repo.engine.connect() as conn:
            return [
                dict(r) for r in conn.execute(select(budget_reservations)).mappings()
            ]

    def test_same_job_age_inputs_hold_and_upstream_evidence_survive_reclaim(self):
        claim = self.running()
        before = self.repo.get_job(self.scope, claim.job["id"])
        old_attempt, held = self.attempt(claim.lease.attempt_id), self.holds()
        self.now += 601
        receipt = self.reclaim(
            claim, reason="capacity_no_progress", last_progress_at=1000
        )
        after = self.repo.get_job(self.scope, claim.job["id"])
        self.assertEqual(receipt["state"], "requeued")
        for key in (
            "id",
            "created_at",
            "request",
            "request_hash",
            "execution_plan",
            "plan_id",
            "estimated_cost_microusd",
            "attempt_no",
        ):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(
            (
                after["status"],
                after["current_attempt_id"],
                after["lease_worker_id"],
                after["lease_expires_at"],
            ),
            ("queued", None, None, None),
        )
        lost = self.attempt(claim.lease.attempt_id)
        self.assertEqual(lost["status"], "attempt_lost")
        for key in (
            "id",
            "job_id",
            "number",
            "worker_id",
            "fence",
            "upstream_task_id",
            "submission_started_at",
            "upstream_stopped",
            "actual_cost_microusd",
        ):
            self.assertEqual(lost[key], old_attempt[key], key)
        self.assertTrue(authorized_lost_attempt(after, lost))
        self.assertEqual(self.holds(), held)
        with self.repo.engine.connect() as conn:
            event = (
                conn.execute(select(outbox).where(outbox.c.id == receipt["event_id"]))
                .mappings()
                .one()
            )
        self.assertEqual(event["event_type"], "capacity.attempt_lost")

    def test_trusted_provider_removal_preserves_unsettled_supplier_obligation(self):
        claim = self.running()
        original = self.attempt(claim.lease.attempt_id)
        held = self.holds()
        receipt = self.reclaim(claim, reason="capacity_provider_removed")
        job = self.repo.get_job(self.scope, claim.job["id"])
        lost = self.attempt(claim.lease.attempt_id)
        self.assertEqual(receipt["reason"], "capacity_provider_removed")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(history(job)[0], 1)
        self.assertTrue(authorized_lost_attempt(job, lost))
        self.assertEqual(lost["upstream_stopped"], original["upstream_stopped"])
        self.assertIsNone(lost["actual_cost_microusd"])
        self.assertEqual(self.holds(), held)
        self.assertEqual(
            self.reclaim(claim, reason="capacity_provider_removed"),
            {**receipt, "replayed": True},
        )

    def test_second_claim_is_original_job_and_late_result_cannot_attach(self):
        first = self.running()
        self.reclaim(first)
        second = self.running(first.job, worker="replacement")
        self.assertEqual(second.job["id"], first.job["id"])
        self.assertEqual(second.job["attempt_no"], 2)
        self.assertNotEqual(second.lease.attempt_id, first.lease.attempt_id)
        with self.assertLogs("studio_platform.capacity_reclaim", "WARNING") as logs:
            with self.assertRaises(LeaseLost):
                self.queue.complete(
                    first.lease, [VALID_ARTIFACT], actual_cost_microusd=1
                )
        self.assertIn("capacity_late_result_discarded", logs.output[0])
        with self.repo.engine.connect() as conn:
            self.assertEqual(list(conn.execute(select(artifacts))), [])
        self.queue.begin_collection(second.lease)
        done = self.queue.complete(
            second.lease, [VALID_ARTIFACT], actual_cost_microusd=None
        )
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(history(done)[0], 1)
        self.assertEqual(self.attempt(first.lease.attempt_id)["upstream_stopped"], 0)

    def test_confirmed_original_cost_is_added_without_overwriting_attempt_costs(self):
        first = self.running()
        self.reclaim(first)
        with self.repo.transaction() as conn:
            conn.execute(
                update(attempts)
                .where(attempts.c.id == first.lease.attempt_id)
                .values(actual_cost_microusd=9, upstream_stopped=1)
            )
        second = self.running(first.job, worker="replacement")
        self.queue.begin_collection(second.lease)
        done = self.queue.complete(
            second.lease, [VALID_ARTIFACT], actual_cost_microusd=17
        )
        self.assertEqual(
            (done["result"]["actual_cost_microusd"], done["result"]["billing_status"]),
            (26, "settled"),
        )
        self.assertEqual(
            self.attempt(first.lease.attempt_id)["actual_cost_microusd"], 9
        )
        self.assertEqual(
            self.attempt(second.lease.attempt_id)["actual_cost_microusd"], 17
        )

    def test_observed_old_cost_without_stop_confirmation_cannot_settle_liability(self):
        first = self.running()
        held = self.holds()
        self.reclaim(first)
        with self.repo.transaction() as conn:
            conn.execute(
                update(attempts)
                .where(attempts.c.id == first.lease.attempt_id)
                .values(actual_cost_microusd=9)
            )
        second = self.running(first.job, worker="replacement")
        self.queue.begin_collection(second.lease)
        done = self.queue.complete(
            second.lease, [VALID_ARTIFACT], actual_cost_microusd=17
        )
        self.assertEqual(done["result"]["billing_status"], "pending")
        self.assertIsNone(done["result"]["actual_cost_microusd"])
        self.assertEqual(self.holds(), held)

    def test_receipt_replay_after_new_attempt_is_noop_and_changed_body_conflicts(self):
        first = self.running()
        original = self.reclaim(first)
        second = self.running(first.job, worker="replacement")
        before = self.repo.get_job(self.scope, first.job["id"])
        replay = self.reclaim(first)
        self.assertTrue(replay.pop("replayed"))
        self.assertEqual(replay, {k: v for k, v in original.items() if k != "replayed"})
        self.assertEqual(self.repo.get_job(self.scope, first.job["id"]), before)
        with self.assertRaises(Conflict):
            self.reclaim(first, evidence_id="another-proof")
        self.assertEqual(before["current_attempt_id"], second.lease.attempt_id)

    def test_concurrent_reclaim_is_one_epoch_and_one_event(self):
        # LedgerCase runs this same race on the existing PostgreSQL CI service.
        claim = self.running()
        original = self.repo.get_job(self.scope, claim.job["id"])
        held = self.holds()
        receipts = self.parallel(lambda _: self.reclaim(claim), 4)
        self.assertEqual(sum(not r["replayed"] for r in receipts), 1)
        self.assertEqual(len({r["event_id"] for r in receipts}), 1)
        recovered = self.repo.get_job(self.scope, claim.job["id"])
        self.assertEqual(recovered["fence"], claim.lease.fence + 1)
        for field in ("id", "created_at", "request_hash", "plan_id"):
            self.assertEqual(recovered[field], original[field])
        self.assertEqual(self.holds(), held)
        replacement = self.running(claim.job, worker="replacement")
        self.assertEqual(replacement.job["id"], original["id"])
        self.assertEqual(replacement.job["attempt_no"], 2)
        with self.assertLogs("studio_platform.capacity_reclaim", "WARNING"):
            with self.assertRaises(LeaseLost):
                self.queue.complete(
                    claim.lease, [VALID_ARTIFACT], actual_cost_microusd=1
                )
        with self.repo.engine.connect() as conn:
            self.assertEqual(len(list(conn.execute(select(jobs)))), 1)
            self.assertEqual(list(conn.execute(select(artifacts))), [])
            self.assertEqual(
                len(
                    list(
                        conn.execute(
                            select(outbox).where(
                                outbox.c.event_type == "capacity.attempt_lost"
                            )
                        )
                    )
                ),
                1,
            )
        self.assertEqual(self.holds(), held)

    def test_two_requeues_then_exhaustion_preserves_supplier_hold(self):
        claim = self.running()
        held = self.holds()
        for expected_count in (1, 2):
            self.assertEqual(self.reclaim(claim)["count"], expected_count)
            claim = self.running(claim.job, worker="worker-" + str(expected_count))
        receipt = self.reclaim(claim)
        job = self.repo.get_job(self.scope, claim.job["id"])
        self.assertEqual((receipt["state"], receipt["count"]), ("exhausted", 2))
        self.assertEqual(
            (job["status"], job["error_code"]),
            ("failed", "capacity_lost_retries_exhausted"),
        )
        self.assertEqual(len(history(job)[1]), 3)
        self.assertEqual(self.holds(), held)
        self.assertIsNone(self.queue.claim("fourth", "test-pool"))

    def test_timeout_uses_positive_progress_and_runtime_floor(self):
        claim = self.running()
        self.now = 1599
        with self.assertRaisesRegex(Conflict, "not_due"):
            self.reclaim(claim, reason="capacity_no_progress", last_progress_at=1000)
        self.now = 1601
        with self.repo.transaction() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.id == claim.job["id"])
                .values(
                    result={
                        "capacity_progress": {
                            "attempt_id": claim.lease.attempt_id,
                            "at": 1500,
                            "kind": "engine_running",
                        }
                    }
                )
            )
        with self.assertRaisesRegex(Conflict, "progress_changed"):
            self.reclaim(claim, reason="capacity_no_progress", last_progress_at=1000)
        self.now = 2800
        with self.assertRaisesRegex(Conflict, "not_due"):
            self.reclaim(claim, reason="capacity_runtime_exceeded")
        self.now += 1
        self.assertEqual(
            self.reclaim(claim, reason="capacity_runtime_exceeded")["state"], "requeued"
        )

    def test_owner_fence_and_cancel_boundaries(self):
        claim = self.running()
        with self.assertRaises(NotFound):
            self.queue.reclaim_capacity(
                self.other,
                claim.job["id"],
                expected_attempt_id=claim.lease.attempt_id,
                expected_fence=claim.lease.fence,
                reason="capacity_owner_recovery",
                evidence_id="original-proof",
            )
        with self.assertRaises(Conflict):
            self.reclaim(claim, expected_fence=claim.lease.fence + 1)
        self.repo.request_cancel(self.scope, claim.job["id"])
        with self.assertRaises(InvalidTransition):
            self.reclaim(claim)

    def test_claimed_without_submission_is_not_abandoned_as_paid_work(self):
        self.job()
        claim = self.queue.claim("worker", "test-pool")
        with self.assertRaises(InvalidTransition):
            self.reclaim(claim)

    def test_terminal_or_unknown_attempt_is_not_requeued_by_stale_running_job(self):
        for status in (
            "succeeded",
            "failed",
            "cancelled",
            "attempt_lost",
            "deferred",
            "lease_expired",
            "unknown",
        ):
            with self.subTest(status=status):
                claim = self.running()
                with self.repo.transaction() as conn:
                    conn.execute(
                        update(attempts)
                        .where(attempts.c.id == claim.lease.attempt_id)
                        .values(status=status)
                    )
                before = self.repo.get_job(self.scope, claim.job["id"])
                original = self.attempt(claim.lease.attempt_id)
                held = self.holds()
                with self.assertRaisesRegex(Conflict, "attempt_invalid"):
                    self.reclaim(claim)
                self.assertEqual(self.repo.get_job(self.scope, claim.job["id"]), before)
                self.assertEqual(self.attempt(claim.lease.attempt_id), original)
                self.assertEqual(self.holds(), held)

    def test_forged_lost_status_or_changed_identity_is_not_authority(self):
        claim = self.running()
        job = self.repo.get_job(self.scope, claim.job["id"])
        lost = {**self.attempt(claim.lease.attempt_id), "status": "attempt_lost"}
        self.assertFalse(authorized_lost_attempt(job, lost))
        self.reclaim(claim)
        job = self.repo.get_job(self.scope, claim.job["id"])
        self.assertTrue(authorized_lost_attempt(job, lost))
        for key, value in (
            ("worker_id", "different"),
            ("number", 99),
            ("fence", 99),
            ("job_id", "different"),
        ):
            self.assertFalse(authorized_lost_attempt(job, {**lost, key: value}), key)
        corrupt = copy.deepcopy(job)
        corrupt["result"]["capacity_reclaim_count"] = True
        self.assertFalse(authorized_lost_attempt(corrupt, lost))

    def test_outbox_failure_rolls_back_entire_reclaim(self):
        claim = self.running()
        before, attempt = (
            self.repo.get_job(self.scope, claim.job["id"]),
            self.attempt(claim.lease.attempt_id),
        )
        with patch.object(self.repo, "_emit", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                self.reclaim(claim)
        self.assertEqual(self.repo.get_job(self.scope, claim.job["id"]), before)
        self.assertEqual(self.attempt(claim.lease.attempt_id), attempt)

    def test_public_job_exposes_count_but_not_internal_receipt(self):
        from types import SimpleNamespace
        from studio_platform.generation_services import GenerationRead
        from studio_platform.quality_views import job as quality_job

        claim = self.running()
        self.reclaim(claim)
        row = self.repo.get_job(self.scope, claim.job["id"])
        view = GenerationRead(
            self.repo, SimpleNamespace(tenant_id=self.scope.tenant_id)
        ).public_job(row, cancellation={})
        self.assertEqual(view["capacity_reclaim_count"], 1)
        self.assertNotIn("capacity_reclaims", view["result"])
        self.assertEqual(quality_job(view)["capacity_reclaim_count"], 1)
        self.assertEqual(view["error_code"], "capacity_owner_recovery")
