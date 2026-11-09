"""#450: new admission waits a short, bounded time behind account maintenance."""

import os
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from studio_platform import identity
from studio_platform.api_errors import register_handlers
from studio_platform.identity import AccountGateBusy, lock_account_gate
from studio_platform.repository import jobs
from test_platform_repository import LedgerCase


class AccountGateBusyHttpTests(unittest.TestCase):
    def test_busy_gate_is_a_retryable_503_without_internals(self):
        app = FastAPI()
        register_handlers(app, repository=None, settings=None)

        @app.post("/admit")
        def admit():
            raise AccountGateBusy()

        reply = TestClient(app).post("/admit")
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(reply.headers["retry-after"], "5")
        body = reply.json()
        self.assertEqual(body["code"], "maintenance_try_again")
        self.assertIs(body["retryable"], True)
        self.assertEqual(set(body["messages"]), {"en", "zh"})
        self.assertNotIn("lock", reply.text.lower())


@unittest.skipUnless(
    os.environ.get("PLATFORM_TEST_DATABASE_URL"),
    "Explicit isolated local PG URL required",
)
class AdmissionGateWaitPostgresTests(LedgerCase):
    def hold_exclusive(self):
        """Hold the gate exclusively (account maintenance) until released."""
        held, release = threading.Event(), threading.Event()

        def run():
            with self.repo.engine.begin() as connection:
                lock_account_gate(connection, exclusive=True)
                held.set()
                release.wait(30)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(held.wait(10))
        self.addCleanup(thread.join, 30)
        self.addCleanup(release.set)
        return release, thread

    def job_count(self):
        with self.repo.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(jobs)).scalar()

    def test_admission_waits_only_the_bound_then_retry_keeps_idempotency(self):
        plan = self.plan()
        release, thread = self.hold_exclusive()
        with patch.object(identity, "ADMISSION_GATE_WAIT_SECONDS", 0.3):
            started = time.monotonic()
            with self.assertRaises(AccountGateBusy):
                self.repo.create_job(
                    self.scope,
                    plan["id"],
                    "original-key",
                    budget_account_ids=("owner-budget",),
                )
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3)
        self.assertGreaterEqual(elapsed, 0.25)
        # Nothing survived the failed transaction.
        self.assertEqual(self.job_count(), 0)
        release.set()
        thread.join(30)
        first = self.repo.create_job(
            self.scope, plan["id"], "original-key", budget_account_ids=("owner-budget",)
        )
        again = self.repo.create_job(
            self.scope, plan["id"], "original-key", budget_account_ids=("owner-budget",)
        )
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(self.job_count(), 1)

    def test_enqueue_is_bounded_too(self):
        planned = self.job(status="planned")
        release, _ = self.hold_exclusive()
        with patch.object(identity, "ADMISSION_GATE_WAIT_SECONDS", 0.2):
            with self.assertRaises(AccountGateBusy):
                self.repo.enqueue(
                    self.scope, planned["id"], budget_account_ids=("owner-budget",)
                )
        release.set()
        with self.repo.engine.connect() as connection:
            status = connection.execute(
                select(jobs.c.status).where(jobs.c.id == planned["id"])
            ).scalar_one()
        self.assertEqual(status, "planned")

    def test_bound_applies_only_to_the_gate_wait(self):
        with self.repo.engine.begin() as connection:
            before = connection.execute(text("SHOW lock_timeout")).scalar_one()
            lock_account_gate(connection, wait_seconds=1)
            self.assertEqual(
                connection.execute(text("SHOW lock_timeout")).scalar_one(), before
            )
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("SHOW lock_timeout")).scalar_one(), before
            )

    def test_shared_holders_do_not_block_bounded_admission(self):
        with self.repo.engine.begin() as other:
            lock_account_gate(other)  # Another admission or worker claim.
            with patch.object(identity, "ADMISSION_GATE_WAIT_SECONDS", 0.2):
                created = self.job()
        self.assertTrue(created["created"])

    def test_many_bounded_waiters_all_return_and_free_connections(self):
        plans = [self.plan() for _ in range(6)]
        release, _ = self.hold_exclusive()
        outcomes = []

        def admit(index):
            try:
                self.repo.create_job(self.scope, plans[index]["id"], f"k{index}")
                outcomes.append("created")
            except AccountGateBusy:
                outcomes.append("busy")

        with patch.object(identity, "ADMISSION_GATE_WAIT_SECONDS", 0.3):
            self.parallel(admit, count=6)
        self.assertEqual(outcomes, ["busy"] * 6)
        release.set()
        # The pool is usable at once: every waiter returned its connection.
        self.assertTrue(self.job()["created"])


if __name__ == "__main__":
    unittest.main()
