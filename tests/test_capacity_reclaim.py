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

    def note_engine_failure(self, claim, code="wangp_generation_tensor_shape_mismatch"):
        from studio_platform.capacity_watchdog import record_engine_failure

        record_engine_failure(self.repo, self.queue, claim.lease, code)

    def test_a_published_engine_failure_ends_the_job_on_its_first_loss(self):
        # #1214: another machine would fail the same way.
        from studio_platform.capacity_watchdog import timeout_reason

        claim = self.running()
        held = self.holds()
        job = self.repo.get_job(self.scope, claim.job["id"])
        attempt = self.attempt(claim.lease.attempt_id)
        self.now += 60
        self.assertIsNone(timeout_reason(job, attempt, self.now))
        # Without the engine's own note the reason is refused under the lock.
        with self.assertRaisesRegex(Conflict, "not_due"):
            self.reclaim(claim, reason="capacity_engine_failed")
        self.note_engine_failure(claim)
        job = self.repo.get_job(self.scope, claim.job["id"])
        # An ordinary failure exits its thread within moments and is then read
        # as failed with the stop proven; only a note a minute old is stuck.
        self.assertIsNone(timeout_reason(job, attempt, self.now + 59))
        self.now += 59
        with self.assertRaisesRegex(Conflict, "not_due"):
            self.reclaim(claim, reason="capacity_engine_failed")
        self.assertEqual(
            timeout_reason(job, attempt, self.now + 1),
            ("capacity_engine_failed", None),
        )
        self.now += 1
        receipt = self.reclaim(claim, reason="capacity_engine_failed")
        self.assertEqual((receipt["state"], receipt["count"]), ("exhausted", 0))
        job = self.repo.get_job(self.scope, claim.job["id"])
        self.assertEqual(
            (job["status"], job["error_code"], job["current_attempt_id"]),
            ("failed", "wangp_generation_tensor_shape_mismatch", None),
        )
        lost = self.attempt(claim.lease.attempt_id)
        self.assertEqual(
            (lost["status"], lost["error_code"]),
            ("attempt_lost", "capacity_engine_failed"),
        )
        self.assertTrue(authorized_lost_attempt(job, lost))
        self.assertEqual(history(job)[0], 0)
        # The supplier's bill for the lost machine is still unknown.
        self.assertEqual(self.holds(), held)
        self.assertIsNone(self.queue.claim("second", "test-pool"))
        # The same request again returns the same receipt.
        again = self.reclaim(claim, reason="capacity_engine_failed")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["event_id"], receipt["event_id"])

    def test_engine_failure_after_a_requeue_ends_the_job_with_its_history_valid(self):
        claim = self.running()
        self.assertEqual(self.reclaim(claim)["count"], 1)
        claim = self.running(claim.job, worker="worker-1")
        self.note_engine_failure(claim, "wangp_generation_cuda_out_of_memory")
        self.now += 60
        receipt = self.reclaim(claim, reason="capacity_engine_failed")
        self.assertEqual((receipt["state"], receipt["count"]), ("exhausted", 1))
        job = self.repo.get_job(self.scope, claim.job["id"])
        self.assertEqual(
            (job["status"], job["error_code"]),
            ("failed", "wangp_generation_cuda_out_of_memory"),
        )
        self.assertEqual((history(job)[0], len(history(job)[1])), (1, 2))

    def test_an_engine_note_of_another_attempt_or_a_free_text_code_is_no_authority(
        self,
    ):
        claim = self.running()
        for note in (
            {"attempt_id": "another-attempt", "at": self.now, "code": "wangp_x"},
            {
                "attempt_id": claim.lease.attempt_id,
                "at": self.now,
                "code": "RuntimeError: private text",
            },
            {
                "attempt_id": claim.lease.attempt_id,
                "at": self.now - 10_000,
                "code": "wangp_generation_cuda_out_of_memory",
            },
        ):
            with self.repo.transaction() as conn:
                conn.execute(
                    update(jobs)
                    .where(jobs.c.id == claim.job["id"])
                    .values(result={"engine_failure": note})
                )
            with self.assertRaisesRegex(Conflict, "not_due"):
                self.reclaim(claim, reason="capacity_engine_failed")
        # An early end is only for the engine reason: other reasons keep the count rule.
        job = self.repo.get_job(self.scope, claim.job["id"])
        forged = copy.deepcopy(job)
        forged["result"] = {
            "capacity_reclaim_count": 0,
            "capacity_reclaims": [
                {
                    "version": 1,
                    "job_id": job["id"],
                    "attempt_id": claim.lease.attempt_id,
                    "number": 1,
                    "worker_id": "worker",
                    "attempt_fence": claim.lease.fence,
                    "previous_fence": claim.lease.fence,
                    "fence": claim.lease.fence + 1,
                    "reason": "capacity_no_progress",
                    "evidence_id": "proof",
                    "at": self.now,
                    "last_progress_at": None,
                    "state": "exhausted",
                    "count": 0,
                    "event_id": "event",
                }
            ],
        }
        with self.assertRaisesRegex(Conflict, "history_invalid"):
            history(forged)
        forged["result"]["capacity_reclaims"][0]["reason"] = "capacity_engine_failed"
        self.assertEqual(history(forged)[0], 0)

    def silent(self, claim):
        self.now += 601
        return self.reclaim(
            claim,
            reason="capacity_no_progress",
            last_progress_at=self.attempt(claim.lease.attempt_id)[
                "submission_started_at"
            ],
        )

    def alerts(self):
        with self.repo.engine.connect() as conn:
            return [
                row["payload"]
                for row in conn.execute(
                    select(outbox).where(outbox.c.event_type == "capacity.health_alert")
                ).mappings()
            ]

    def test_silence_on_a_second_machine_ends_the_job_with_one_alert(self):
        # #1214: the job is the suspect, not the machines; no third machine.
        claim = self.running()
        held = self.holds()
        self.assertEqual(self.silent(claim)["state"], "requeued")
        self.assertEqual(self.alerts(), [])
        claim = self.running(claim.job, worker="worker-1")
        receipt = self.silent(claim)
        self.assertEqual((receipt["state"], receipt["count"]), ("exhausted", 1))
        job = self.repo.get_job(self.scope, claim.job["id"])
        self.assertEqual(
            (job["status"], job["error_code"]),
            ("failed", "capacity_lost_retries_exhausted"),
        )
        self.assertEqual((history(job)[0], len(history(job)[1])), (1, 2))
        [alert] = self.alerts()
        self.assertEqual(
            (alert["code"], alert["diagnosis"], alert["resource_id"]),
            ("capacity_request_silent", "silent_on_two_machines", job["id"]),
        )
        self.assertEqual(self.holds(), held)
        self.assertIsNone(self.queue.claim("third", "test-pool"))
        # Replaying the receipt neither changes the job nor alerts again.
        self.assertTrue(
            self.reclaim(
                claim,
                reason="capacity_no_progress",
                last_progress_at=receipt["last_progress_at"],
            )["replayed"]
        )
        self.assertEqual(len(self.alerts()), 1)

    def test_silence_twice_on_the_same_machine_or_after_another_loss_keeps_the_count(
        self,
    ):
        # The same machine twice says nothing about the job.
        claim = self.running()
        self.assertEqual(self.silent(claim)["state"], "requeued")
        claim = self.running(claim.job)
        self.assertEqual(self.silent(claim)["state"], "requeued")
        self.assertEqual(self.alerts(), [])
        # A different kind of loss in between is not the same symptom.
        other = self.running(self.job())
        self.assertEqual(self.reclaim(other)["state"], "requeued")
        other = self.running(other.job, worker="worker-1")
        self.assertEqual(self.silent(other)["state"], "requeued")
        self.assertEqual(self.alerts(), [])

    def test_a_loss_caused_by_the_request_is_no_evidence_about_the_host(self):
        from studio_platform.capacity_reclaim import request_suspect

        nodes = ["00000000-0000-4000-8000-00000000000%d" % index for index in range(4)]
        workers = ["dstack-" + node.replace("-", "") for node in nodes]

        def suspect(node, **options):
            with self.repo.engine.connect() as conn:
                return request_suspect(conn, node, **options)

        claim = self.running(worker=workers[0])
        self.silent(claim)
        # The job is open: no verdict yet, so an earlier loss waits.
        self.assertFalse(suspect(nodes[0]))
        self.assertTrue(suspect(nodes[0], pending=True))
        claim = self.running(claim.job, worker=workers[1])
        self.silent(claim)
        # Silent on a second machine: both losses belong to the request.
        self.assertTrue(suspect(nodes[0]))
        self.assertTrue(suspect(nodes[1]))
        # A job that was lost once and then delivered elsewhere was fine:
        # that loss does count against its host.
        other = self.running(self.job(), worker=workers[2])
        self.silent(other)
        done = self.running(other.job, worker=workers[3])
        self.queue.begin_collection(done.lease)
        self.queue.complete(done.lease, [VALID_ARTIFACT], actual_cost_microusd=0)
        self.assertFalse(suspect(nodes[2], pending=True))
        self.assertFalse(suspect(nodes[3], pending=True))

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
        # The engine's note names an attempt and a time: internal, while the
        # job runs and after it ends. The failure class is public as error_code.
        claim = self.running(claim.job, worker="worker-1")
        self.note_engine_failure(claim)
        reader = GenerationRead(
            self.repo, SimpleNamespace(tenant_id=self.scope.tenant_id)
        )
        row = self.repo.get_job(self.scope, claim.job["id"])
        self.assertIn("engine_failure", row["result"])
        self.assertNotIn(
            "engine_failure", reader.public_job(row, cancellation={})["result"]
        )
        self.now += 60
        self.reclaim(claim, reason="capacity_engine_failed")
        view = reader.public_job(
            self.repo.get_job(self.scope, claim.job["id"]), cancellation={}
        )
        self.assertNotIn("engine_failure", view["result"])
        self.assertEqual(view["error_code"], "wangp_generation_tensor_shape_mismatch")
