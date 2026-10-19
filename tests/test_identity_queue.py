"""Suspension fences original admission/claim, while paid work can still recover."""

from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
import uuid

from sqlalchemy import insert, select, update

from studio_platform.control import WorkerControl
from studio_platform.identity import lock_account_gate
from studio_platform.identity_schema import metadata, users
from studio_platform.queue import TaskQueue
from studio_platform.repository import Conflict, attempts, jobs
import test_platform_control as control_fixture
from test_platform_repository import LedgerCase


class IdentityQueueTests(LedgerCase):
    def setUp(self):
        super().setUp()
        with self.repo.transaction() as connection:
            metadata.create_all(connection)
            for owner in (self.scope.owner_id, self.other.owner_id):
                connection.execute(
                    insert(users).values(
                        id=str(uuid.uuid4()),
                        tenant=self.scope.tenant_id,
                        owner=owner,
                        role="user",
                        status="active",
                        display_name=owner,
                        email_verified=0,
                        created_at=self.now,
                        updated_at=self.now,
                    )
                )
        self.queue = TaskQueue(self.repo)

    def suspend(self):
        with self.repo.transaction() as connection:
            lock_account_gate(connection, exclusive=True)
            connection.execute(
                update(users)
                .where(
                    users.c.tenant == self.scope.tenant_id,
                    users.c.owner == self.scope.owner_id,
                )
                .values(status="suspended")
            )

    def test_suspended_admission_creates_no_job_or_budget_hold(self):
        plan = self.plan()
        before = self.repo.get_budget("owner-budget")
        self.suspend()
        with self.assertRaisesRegex(Conflict, "account_suspended"):
            self.repo.create_job(
                self.scope, plan["id"], "suspended", budget_account_ids=["owner-budget"]
            )
        self.assertEqual(self.repo.get_budget("owner-budget"), before)
        with self.repo.engine.connect() as connection:
            self.assertIsNone(connection.execute(select(jobs.c.id)).first())

    def test_suspended_owner_is_skipped_without_blocking_another_owner(self):
        original = self.job()
        other = self.job(scope=self.other, cost=0)
        self.suspend()
        claim = self.queue.claim("test-worker", "test-pool")
        self.assertEqual(claim.job["id"], other["id"])
        self.assertEqual(self.repo.get_job(self.scope, original["id"])["attempt_no"], 0)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def test_suspension_between_claim_and_submit_keeps_original_unsubmitted_attempt(
        self,
    ):
        original = self.job()
        claim = self.queue.claim("test-worker", "test-pool")
        self.suspend()
        with self.assertRaisesRegex(Conflict, "account_suspended"):
            self.queue.begin_submission(claim.lease)
        with self.repo.engine.connect() as connection:
            attempt = (
                connection.execute(
                    select(attempts).where(attempts.c.id == claim.lease.attempt_id)
                )
                .mappings()
                .one()
            )
        self.assertIsNone(attempt["submission_started_at"])
        self.assertIsNone(attempt["upstream_task_id"])
        self.assertEqual(self.repo.get_job(self.scope, original["id"])["attempt_no"], 1)
        self.assertEqual(
            self.repo.get_budget("owner-budget")["reserved_microusd"], 100_000
        )

    def test_already_submitted_work_can_collect_after_suspension(self):
        original = self.job()
        claim = self.queue.claim("test-worker", "test-pool")
        self.queue.begin_submission(claim.lease)
        self.queue.record_submitted(claim.lease, "original-upstream-id")
        self.suspend()
        self.queue.begin_collection(claim.lease)
        current = self.repo.get_job(self.scope, original["id"])
        self.assertEqual((current["status"], current["attempt_no"]), ("collecting", 1))
        with self.repo.engine.connect() as connection:
            upstream = connection.execute(
                select(attempts.c.upstream_task_id).where(
                    attempts.c.id == claim.lease.attempt_id
                )
            ).scalar_one()
        self.assertEqual(upstream, "original-upstream-id")

    def test_slot_advice_and_actual_claim_both_reject_suspended_account(self):
        self.repo.configure_pool("control-test", max_instances=1, max_physical_gpus=1)
        self.control = WorkerControl(self.repo)
        spec = control_fixture.ControlTests.spec(self)
        control_fixture.ControlTests.ready(self, spec)
        job = control_fixture.ControlTests.controlled_job(self)
        self.suspend()
        worker = self.control.get(spec.worker_id)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                self.control.claim_reason(connection, worker, job), "account_suspended"
            )
        self.assertIsNone(self.control.claim(spec.worker_id, spec.pool))
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 0)

    def test_postgres_suspension_commit_precedes_waiting_claim_without_attempt(self):
        if self.repo.engine.dialect.name != "postgresql":
            self.skipTest("PostgreSQL advisory lock behavior")
        original = self.job()
        started, finished = threading.Event(), threading.Event()

        def claim():
            started.set()
            try:
                return self.queue.claim("test-worker", "test-pool")
            finally:
                finished.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.repo.transaction() as connection:
                lock_account_gate(connection, exclusive=True)
                connection.execute(
                    update(users)
                    .where(
                        users.c.tenant == self.scope.tenant_id,
                        users.c.owner == self.scope.owner_id,
                    )
                    .values(status="suspended")
                )
                future = executor.submit(claim)
                self.assertTrue(started.wait(3))
                self.assertFalse(finished.wait(0.1))
            self.assertIsNone(future.result(timeout=10))
        self.assertEqual(self.repo.get_job(self.scope, original["id"])["attempt_no"], 0)


if __name__ == "__main__":
    unittest.main()
