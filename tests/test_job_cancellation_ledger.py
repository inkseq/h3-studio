"""Original cancellation, accounting, state snapshots and commit-order proofs."""

from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import insert, select, update

from studio_platform.job_cancellation import (
    cancel_eligibility,
    cancel_snapshot,
    cancellation_fields,
)
from studio_platform.queue import TaskQueue
from studio_platform.repository import (
    Conflict,
    NotFound,
    Scope,
    attempts,
    budget_accounts,
    budget_reservations,
    jobs,
    outbox,
    registered_workers,
)
import test_platform_repository as ledger_fixture
import test_platform_queue as queue_fixture
import test_platform_capacity as capacity_fixture


class JobCancellationLedgerTests(ledger_fixture.LedgerCase):
    def setUp(self):
        super().setUp()
        self.queue = TaskQueue(self.repo)

    def selection(self, job_id):
        with self.repo.engine.connect() as connection:
            row = (
                connection.execute(select(jobs).where(jobs.c.id == job_id))
                .mappings()
                .one()
            )
        return {"id": job_id, "expected_snapshot": cancel_snapshot(row)}

    def bulk(self, selected, reason="operator_queue_cleared"):
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            return self.repo.cancel_queued_jobs(
                connection,
                tenant_id=self.scope.tenant_id,
                selections=selected,
                actor_id="admin-browser",
                reason_code=reason,
            )

    def events(self, job_id=None):
        with self.repo.engine.connect() as connection:
            statement = select(outbox).where(outbox.c.job_event_seq.is_not(None))
            if job_id is not None:
                statement = statement.where(outbox.c.aggregate_id == job_id)
            return [
                dict(row)
                for row in connection.execute(
                    statement.order_by(outbox.c.job_event_seq)
                ).mappings()
            ]

    def advice(self, job_id):
        with self.repo.engine.connect() as connection:
            return cancellation_fields(connection, self.repo._job(connection, job_id))

    def test_atomic_operator_cancel_preserves_request_and_refunds_once(self):
        one, two = self.job(), self.job()
        before = self.repo.get_budget("owner-budget")
        rows = self.bulk([self.selection(two["id"]), self.selection(one["id"])])
        self.assertEqual({row["status"] for row in rows}, {"cancelled"})
        for original in (one, two):
            row = self.repo.get_job(self.scope, original["id"])
            self.assertEqual(row["request"], original["request"])
            self.assertEqual(row["execution_plan"], original["execution_plan"])
            self.assertEqual(row["fence"], original["fence"] + 1)
            fields = self.advice(row["id"])
            self.assertEqual(fields["cancelled_by"], "operator")
            self.assertEqual(fields["reason_code"], "operator_queue_cleared")
            self.assertEqual(
                fields["retry_advice"],
                {
                    "safe_to_retry": True,
                    "charged": False,
                    "upstream_still_running": False,
                },
            )
            self.assertNotIn("actor_id", fields)
            event = self.events(row["id"])[-1]
            self.assertEqual(event["event_type"], "job.cancel_requested")
            self.assertEqual(event["payload"]["public_state"]["status"], "cancelled")
            self.repo.request_cancel(self.scope, row["id"])
        self.assertEqual(
            self.repo.get_budget("owner-budget"), dict(before, reserved_microusd=0)
        )

    def test_original_capacity_waiter_cancel_preserves_rental_budget(self):
        fixture = capacity_fixture.CapacityTests(methodName="runTest")
        fixture.setUp()
        try:
            fixture.approve()
            waiting = fixture.waiting()
            rental_budget = fixture.repo.get_budget("capacity-budget")
            with fixture.repo.transaction() as connection:
                fixture.repo._lock_capacity(connection)
                result = fixture.repo.cancel_queued_jobs(
                    connection,
                    tenant_id=fixture.scope.tenant_id,
                    selections=[
                        {
                            "id": waiting["id"],
                            "expected_snapshot": cancel_snapshot(waiting),
                        }
                    ],
                    actor_id="admin-browser",
                    reason_code="capacity_unavailable",
                )
            self.assertEqual(result[0]["status"], "cancelled")
            with fixture.repo.engine.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        select(capacity_fixture.capacity_waiters.c.state).where(
                            capacity_fixture.capacity_waiters.c.job_id == waiting["id"]
                        )
                    ).scalar_one(),
                    "cancelled",
                )
            self.assertEqual(
                fixture.repo.get_budget("owner:superdan")["reserved_microusd"], 0
            )
            self.assertEqual(fixture.repo.get_budget("capacity-budget"), rental_budget)
            self.assertEqual(fixture.provider.creates, [])
        finally:
            fixture.tearDown()

    def test_owner_intent_retained_when_result_completes_after_cancel(self):
        original = self.job()
        claim = self.queue.claim("worker", "test-pool")
        self.queue.begin_submission(claim.lease)
        self.queue.record_submitted(claim.lease, "upstream-task")
        cancelled = self.repo.request_cancel(self.scope, original["id"])
        self.assertEqual(cancelled["status"], "cancel_requested")
        self.assertEqual(self.advice(original["id"])["cancelled_by"], "owner")
        self.assertFalse(self.advice(original["id"])["retry_advice"]["safe_to_retry"])
        self.assertIsNone(self.advice(original["id"])["retry_advice"]["charged"])
        self.queue.begin_collection(claim.lease)
        self.queue.complete(
            claim.lease, [queue_fixture.VALID_ARTIFACT], actual_cost_microusd=80_000
        )
        fields = self.advice(original["id"])
        self.assertEqual(fields["cancelled_by"], "owner")
        self.assertFalse(fields["retry_advice"]["safe_to_retry"])
        self.assertTrue(fields["retry_advice"]["charged"])
        self.assertTrue(
            self.repo.get_job(self.scope, original["id"])["result"][
                "completed_after_cancel_request"
            ]
        )

    def test_system_failure_and_missing_state_events_freeze_historical_phase(self):
        original = self.job()
        claim = self.queue.claim("worker", "test-pool")
        self.queue.begin_submission(claim.lease)
        self.queue.record_submitted(claim.lease, "upstream-task")
        self.queue.begin_collection(claim.lease)
        self.queue.fail(
            claim.lease,
            "machine_failed",
            actual_cost_microusd=None,
            upstream_stopped=True,
        )
        states = [
            row["payload"]["public_state"]["status"]
            for row in self.events(original["id"])
        ]
        self.assertEqual(
            states,
            ["queued", "claimed", "submitting", "running", "collecting", "failed"],
        )
        fields = self.advice(original["id"])
        self.assertEqual(fields["cancelled_by"], "system")
        self.assertEqual(fields["reason_code"], "machine_failed")
        self.assertEqual(
            fields["retry_advice"],
            {"safe_to_retry": True, "charged": None, "upstream_still_running": False},
        )
        # Settlement later updates advice, not the original failed-event fact.
        self.repo.settle_completed_job(
            self.scope, original["id"], actual_cost_microusd=50_000
        )
        self.assertIsNone(
            self.events(original["id"])[-2]["payload"]["public_state"]["retry_advice"][
                "charged"
            ]
        )
        self.assertTrue(self.advice(original["id"])["retry_advice"]["charged"])

    def test_bulk_stale_claim_or_unsafe_history_refuses_every_row(self):
        one, two = self.job(), self.job()
        selected = [self.selection(one["id"]), self.selection(two["id"])]
        claim = self.queue.claim("worker", "test-pool", job_ids=[two["id"]])
        budget = self.repo.get_budget("owner-budget")
        before = len(self.events())
        with self.assertRaisesRegex(Conflict, "job_cancel_snapshot_changed"):
            self.bulk(selected)
        self.assertEqual(self.repo.get_job(self.scope, one["id"])["status"], "queued")
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        self.assertEqual(len(self.events()), before)
        self.queue.begin_submission(claim.lease)
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs)
                .where(jobs.c.id == two["id"])
                .values(
                    status="queued",
                    lease_worker_id=None,
                    lease_expires_at=None,
                    fence=claim.lease.fence + 1,
                )
            )
        with self.assertRaisesRegex(Conflict, "job_not_safely_cancellable"):
            self.bulk([self.selection(one["id"]), self.selection(two["id"])])
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)

    def test_task_id_without_timestamp_worker_binding_and_orphan_history_block(self):
        original = self.job()
        claim = self.queue.claim("worker", "test-pool")
        self.queue.defer_unsubmitted(claim.lease, retry_after_s=0)
        with self.repo.transaction() as connection:
            connection.execute(
                update(attempts)
                .where(attempts.c.id == claim.lease.attempt_id)
                .values(upstream_task_id="uncertain-task")
            )
        with self.assertRaises(Conflict):
            self.bulk([self.selection(original["id"])])
        with self.repo.transaction() as connection:
            connection.execute(
                update(attempts)
                .where(attempts.c.id == claim.lease.attempt_id)
                .values(upstream_task_id=None)
            )
            connection.execute(
                insert(registered_workers).values(
                    id="bound-worker",
                    pool="test-pool",
                    provider="test",
                    instance_id="local-only",
                    spec={},
                    spec_hash="a" * 64,
                    state="draining",
                    current_job_id=original["id"],
                    fence=1,
                    expires_at=self.now - 1,
                    updated_at=self.now,
                )
            )
        with self.assertRaises(Conflict):
            self.bulk([self.selection(original["id"])])
        with self.repo.transaction() as connection:
            connection.execute(update(registered_workers).values(current_job_id=None))
            connection.execute(
                update(jobs)
                .where(jobs.c.id == original["id"])
                .values(current_attempt_id=str(uuid.uuid4()))
            )
        with self.assertRaises(Conflict):
            self.bulk([self.selection(original["id"])])

    def test_old_unsubmitted_history_does_not_loosen_quickchat_retry_gate(self):
        original = self.job()
        claim = self.queue.claim("worker", "test-pool")
        self.queue.defer_unsubmitted(claim.lease, retry_after_s=0)
        self.bulk([self.selection(original["id"])])
        fields = self.advice(original["id"])
        self.assertFalse(fields["retry_advice"]["safe_to_retry"])
        self.assertFalse(fields["retry_advice"]["charged"])
        self.assertFalse(fields["retry_advice"]["upstream_still_running"])

    def test_foreign_tenant_and_bad_selection_are_atomic_refusals(self):
        original = self.job()
        foreign = self.job(scope=Scope("foreign", "superdan", "project-1"), cost=0)
        before = self.repo.get_budget("owner-budget")
        with self.assertRaises(NotFound):
            self.bulk([self.selection(original["id"]), self.selection(foreign["id"])])
        for selection in (
            [],
            [self.selection(original["id"])] * 2,
            [{"id": original["id"], "expected_snapshot": "A" * 64}],
        ):
            with self.assertRaises(ValueError):
                self.bulk(selection)
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        self.assertEqual(
            self.repo.get_job(self.scope, original["id"])["status"], "queued"
        )

    def test_finalizer_failure_rolls_back_sequence_and_cancellation(self):
        original = self.job()
        before = self.events()
        sequence = self.repo._sequence_job_events

        def refuse(connection):
            sequence(connection)
            raise RuntimeError("isolated precommit failure")

        with patch.object(self.repo, "_sequence_job_events", side_effect=refuse):
            with self.assertRaises(RuntimeError):
                self.bulk([self.selection(original["id"])])
        self.assertEqual(self.events(), before)
        self.assertEqual(
            self.repo.get_job(self.scope, original["id"])["status"], "queued"
        )
        self.bulk([self.selection(original["id"])])
        self.assertEqual(
            self.events()[-1]["job_event_seq"], before[-1]["job_event_seq"] + 1
        )

    def test_public_read_race_never_combines_old_phase_with_new_retry_grant(self):
        from studio_platform.generation_services import GenerationAccess, GenerationRead

        original = self.job()
        reader = GenerationRead(
            self.repo, GenerationAccess(self.repo, self.scope.tenant_id)
        )
        read_fields = self.repo.get_job_cancellation_fields

        def race(*args, **kwargs):
            self.repo.request_cancel(self.scope, original["id"])
            return read_fields(*args, **kwargs)

        with patch.object(self.repo, "get_job_cancellation_fields", side_effect=race):
            stale = reader.public_job(original)
        self.assertEqual(stale["status"], "queued")
        self.assertIsNone(stale["cancelled_by"])
        self.assertIsNone(stale["reason_code"])
        self.assertEqual(
            stale["retry_advice"],
            {"safe_to_retry": False, "charged": None, "upstream_still_running": None},
        )
        fresh = reader.public_job(self.repo.get_job(self.scope, original["id"]))
        self.assertEqual(fresh["status"], "cancelled")
        self.assertEqual(fresh["cancelled_by"], "owner")
        self.assertTrue(fresh["retry_advice"]["safe_to_retry"])

    def test_postgres_late_business_transaction_gets_later_committed_sequence(self):
        if self.repo.engine.dialect.name != "postgresql":
            self.skipTest("isolated PostgreSQL required for overlapping transactions")
        one, two = self.job(), self.job()
        staged, release = threading.Event(), threading.Event()

        def late():
            with self.repo.transaction() as connection:
                self.repo._emit(
                    connection, "job.queued", one["id"], {"job_id": one["id"]}
                )
                staged.set()
                if not release.wait(5):
                    raise RuntimeError("late commit fixture timeout")

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(late)
            self.assertTrue(staged.wait(5))
            try:
                with self.repo.transaction() as connection:
                    self.repo._emit(
                        connection, "job.queued", two["id"], {"job_id": two["id"]}
                    )
                committed = self.events(two["id"])[-1]
            finally:
                release.set()
            pending.result(timeout=10)
        late_event = self.events(one["id"])[-1]
        self.assertEqual(late_event["created_at"], committed["created_at"])
        self.assertGreater(late_event["job_event_seq"], committed["job_event_seq"])

    def test_postgres_claim_winning_row_lock_invalidates_old_bulk_snapshot(self):
        if self.repo.engine.dialect.name != "postgresql":
            self.skipTest("isolated PostgreSQL required for overlapping transactions")
        original = self.job()
        selected = [self.selection(original["id"])]
        claimed, release = threading.Event(), threading.Event()

        def worker():
            with self.repo.transaction() as connection:
                result = self.queue.claim("worker", "test-pool", connection=connection)
                claimed.set()
                if not release.wait(5):
                    raise RuntimeError("claim fixture timeout")
                return result

        with ThreadPoolExecutor(max_workers=2) as executor:
            pending_claim = executor.submit(worker)
            self.assertTrue(claimed.wait(5))
            pending_cancel = executor.submit(self.bulk, selected)
            release.set()
            self.assertIsNotNone(pending_claim.result(timeout=10))
            with self.assertRaisesRegex(Conflict, "job_cancel_snapshot_changed"):
                pending_cancel.result(timeout=10)
        self.assertEqual(
            self.repo.get_job(self.scope, original["id"])["status"], "claimed"
        )
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"],
            original["estimated_cost_microusd"],
        )

    def test_postgres_cross_owner_bulk_prelocks_account_union_before_ordinary_settlement(
        self,
    ):
        if self.repo.engine.dialect.name != "postgresql":
            self.skipTest("isolated PostgreSQL required for account lock contention")
        for account in ("global-X", "global-Z"):
            self.repo.configure_budget(
                account, tenant_id=self.scope.tenant_id, limit_microusd=1_000_000
            )
        # Reserve via the real primitive after IDs exist, deliberately making
        # the earlier sorted job use Z and the later job X (the unsafe order).
        selected_jobs = sorted(
            [self.job(cost=0), self.job(scope=self.other, cost=0)],
            key=lambda row: row["id"],
        )
        ordinary = self.job(cost=0)
        with self.repo.transaction() as connection:
            for row, account in zip(selected_jobs, ("global-Z", "global-X")):
                scope = Scope(row["tenant_id"], row["owner_id"], row["project_id"])
                self.repo._reserve(
                    connection, scope, (account,), "job", row["id"], 100_000
                )
            self.repo._reserve(
                connection,
                self.scope,
                ("global-X", "global-Z"),
                "job",
                ordinary["id"],
                100_000,
            )
        claim = self.queue.claim("worker", "test-pool", job_ids=[ordinary["id"]])
        first_locked, bulk_z = threading.Event(), threading.Event()
        role = threading.local()
        locks = []
        original_lock = self.repo._locked

        def observe(connection, statement):
            row = original_lock(connection, statement)
            if row and budget_accounts in statement.get_final_froms():
                if (
                    getattr(role, "name", None) == "ordinary"
                    and row["id"] == "global-X"
                ):
                    first_locked.set()
                    bulk_z.wait(0.3)
                if getattr(role, "name", None) == "bulk":
                    locks.append(row["id"])
                    if row["id"] == "global-Z":
                        bulk_z.set()
            return row

        def settle():
            role.name = "ordinary"
            return self.queue.fail(
                claim.lease, "worker_not_ready", actual_cost_microusd=0
            )

        def cancel():
            role.name = "bulk"
            return self.bulk([self.selection(row["id"]) for row in selected_jobs])

        with (
            patch.object(self.repo, "_locked", side_effect=observe),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            pending = executor.submit(settle)
            self.assertTrue(first_locked.wait(5))
            cancelling = executor.submit(cancel)
            self.assertEqual(pending.result(timeout=10)["status"], "failed")
            self.assertEqual(
                {row["status"] for row in cancelling.result(timeout=10)}, {"cancelled"}
            )
        self.assertEqual(locks[:2], ["global-X", "global-Z"])
        for account in ("global-X", "global-Z"):
            self.assertEqual(self.repo.get_budget(account)["reserved_microusd"], 0)
            self.assertEqual(self.repo.get_budget(account)["spent_microusd"], 0)


if __name__ == "__main__":
    unittest.main()
