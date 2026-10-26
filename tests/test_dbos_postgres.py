"""Real pinned client transactions in a disposable local PostgreSQL database."""

import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from studio_platform.dbos_transport import configure_producer, workflow_id
from studio_platform.repository import Repository, Scope, jobs, budget_reservations
from studio_platform.workflow_schema import INKSEQ_WORKFLOW_SCHEMA


@unittest.skipUnless(
    os.environ.get("PLATFORM_TEST_DATABASE_URL") and importlib.util.find_spec("dbos"),
    "explicit local PostgreSQL and pinned SDK required",
)
class ClientTransactionTests(unittest.TestCase):
    def setUp(self):
        from dbos import DBOS

        parsed = make_url(os.environ["PLATFORM_TEST_DATABASE_URL"])
        if (
            parsed.host not in {"127.0.0.1", "localhost"}
            or parsed.database != "sixnine_test"
        ):
            raise RuntimeError("test_database_must_be_explicit_local_sixnine_test")
        self.database = "dbos_transport_test_" + uuid.uuid4().hex
        self.bootstrap = create_engine(
            parsed, isolation_level="AUTOCOMMIT", hide_parameters=True
        )
        with self.bootstrap.connect() as connection:
            connection.exec_driver_sql('CREATE DATABASE "' + self.database + '"')
        self.addCleanup(self.drop_database)
        self.url = parsed.set(database=self.database).render_as_string(
            hide_password=False
        )
        # Test-only reference creation. Production uses the reviewed explicit
        # Alembic migration and launch run_migrations=False.
        DBOS.migrate(self.url, schema=INKSEQ_WORKFLOW_SCHEMA)
        self.repo = Repository(self.url, clock=lambda: 1000)
        self.addCleanup(self.repo.close)
        self.repo.create_schema()
        self.repo.configure_capacity(max_instances=10, max_physical_gpus=10)
        self.repo.configure_budget(
            "test-budget",
            tenant_id="test",
            owner_id="superdan",
            limit_microusd=10_000_000,
        )
        self.scope = Scope("test", "superdan", "project")
        self.transport = configure_producer(self.repo)
        self.addCleanup(self.transport.close)
        self.transport.client.register_queue(
            "inkseq-generation-v1", polling_interval_sec=10
        )

    def drop_database(self):
        with self.bootstrap.connect() as connection:
            connection.exec_driver_sql(
                'DROP DATABASE "' + self.database + '" WITH (FORCE)'
            )
        self.bootstrap.dispose()

    def job(self, key="job", *, status="queued"):
        plan = self.repo.create_plan(
            self.scope,
            {"recipe_id": "test", "request": {"model": "SIMULATION"}},
            {
                "backend": "mock",
                "enabled": True,
                "pool": "test",
                "configuration_id": "test-config",
                "expected_runtime_s": 1,
            },
            expires_at=2000,
            estimated_cost_microusd=100_000,
        )
        return self.repo.create_job(
            self.scope,
            plan["id"],
            key,
            initial_status=status,
            budget_account_ids=["test-budget"],
        )

    def workflows(self):
        with self.repo.engine.connect() as connection:
            return (
                connection.execute(
                    text(
                        "SELECT workflow_uuid,status,name,queue_name,application_version,application_name FROM inkseq_workflows.workflow_status ORDER BY workflow_uuid"
                    )
                )
                .mappings()
                .all()
            )

    def test_admission_commit_contains_one_original_job_hold_and_sdk_id(self):
        job = self.job()
        rows = self.workflows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["workflow_uuid"],
            workflow_id("test", "superdan", "project", job["id"]),
        )
        self.assertEqual(rows[0]["status"], "ENQUEUED")
        self.assertEqual(rows[0]["name"], "inkseq.generation.v1")
        self.assertEqual(rows[0]["application_version"], "inkseq-workflows-v1")
        self.assertEqual(
            self.repo.get_budget("test-budget")["reserved_microusd"], 100_000
        )

    def test_executor_restart_keeps_same_workflow_job_attempt_and_native_tag(self):
        from dbos import DBOS
        from studio_platform.control import WorkerControl, WorkerSpec
        from studio_platform.dbos_transport import (
            DurableSlots,
            _Slot,
            register_generation_workflow,
        )
        from studio_platform.storage import LocalObjectStore
        from studio_platform.worker import WorkerRunner, Outcome
        from studio_platform.workflow_schema import dbos_config
        from test_postgres_dispatch import MemoryBackend

        class NativeMemory(MemoryBackend):
            finished = False

            def poll(self, tag, task_id):
                return Outcome(
                    "failed" if self.finished else "running",
                    task_id,
                    0 if self.finished else None,
                )

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        current_time = [1000.0]
        self.repo.clock = lambda: current_time[0]
        control = WorkerControl(self.repo)
        spec = WorkerSpec(
            "worker",
            "test",
            "mock",
            "test-instance",
            ("gpu",),
            ("test",),
            "SIMULATION",
            "test-config",
            "mock",
        )
        control.register(spec)
        control.mark_ready(spec.worker_id, upstream_idle_confirmed=True)
        backend = NativeMemory(lose_response=True)
        settings = Mock()
        slots = DurableSlots(self.repo, settings, folder.name)
        runner = WorkerRunner(
            self.repo,
            LocalObjectStore(Path(folder.name) / "objects"),
            folder.name,
            control=control,
            backend=backend,
            submission_guard=lambda _: True,
        )
        slots._slots["native"] = _Slot(spec, runner, Mock(), threading.Lock())

        def launch():
            DBOS(config=dbos_config(self.url, executor_id="inkseq-workflows-staging"))
            register_generation_workflow(DBOS, slots)
            DBOS.launch()

        def wait_until(check):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if check():
                    return
                time.sleep(0.05)
            self.fail("bounded_original_workflow_observation_timed_out")

        launch()
        try:
            job = self.job()
            wait_until(
                lambda: (
                    self.repo.get_job(self.scope, job["id"])["status"]
                    == "submission_unknown"
                )
            )
            original = self.repo.get_job(self.scope, job["id"])
            first_sdk = self.workflows()[0]["workflow_uuid"]
        finally:
            DBOS.destroy(destroy_registry=True)
        # A native submit may have succeeded before its response was lost. The
        # restarted SDK replays the same durable wait and original domain turn.
        backend.finished = True
        current_time[0] += 31
        launch()
        try:
            wait_until(
                lambda: self.repo.get_job(self.scope, job["id"])["status"] == "failed"
            )
            wait_until(lambda: self.workflows()[0]["status"] == "SUCCESS")
        finally:
            DBOS.destroy(destroy_registry=True)
        final = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(final["current_attempt_id"], original["current_attempt_id"])
        self.assertEqual(final["attempt_no"], 1)
        self.assertEqual(backend.submissions, [original["current_attempt_id"]])
        self.assertEqual(self.workflows()[0]["workflow_uuid"], first_sdk)
        self.assertEqual(len(self.workflows()), 1)
        with self.repo.engine.connect() as connection:
            self.assertEqual(len(connection.execute(jobs.select()).all()), 1)
            self.assertEqual(
                len(connection.execute(budget_reservations.select()).all()), 1
            )
        # Replaying original admission returns the same canonical job before it
        # emits anything. No second SDK run or reservation is created.
        same = self.repo.create_job(
            self.scope, job["plan_id"], "job", budget_account_ids=["test-budget"]
        )
        self.assertEqual(same["id"], job["id"])
        self.assertEqual(len(self.workflows()), 1)

    def test_drained_executor_recovers_same_sleeping_film_without_workflow_wait(self):
        from dbos import DBOS
        from types import SimpleNamespace
        from unittest.mock import patch
        from studio_platform.dbos_transport import DurableExecutor
        from studio_platform.domain_turns import DomainTurns
        from studio_platform.film_runs import VERSION as FILM_VERSION
        from studio_platform.film_workflows import register_film_workflows
        from studio_platform.workflow_schema import dbos_config

        args = ("test", "superdan", "project", "drain-film")
        finish, called, sleeping = threading.Event(), threading.Event(), threading.Event()
        original_recv = DBOS.recv_async

        async def observe_recv(*args, **kwargs):
            sleeping.set()
            return await original_recv(*args, **kwargs)

        receiver = patch.object(DBOS, "recv_async", side_effect=observe_recv)
        receiver.start()
        self.addCleanup(receiver.stop)

        def turn(*_):
            called.set()
            return {"terminal": finish.is_set(), "phase": "completed" if finish.is_set() else "authorization_required"}

        service = SimpleNamespace(tenant="test", turn=turn)
        self.transport.client.register_queue(FILM_VERSION, polling_interval_sec=1)

        def launch():
            turns = DomainTurns()
            DBOS(config=dbos_config(self.url, executor_id="inkseq-workflows-staging"))
            register_film_workflows(
                DBOS, service, lambda *_: SimpleNamespace(owner="superdan"),
                domain_turns=turns,
            )
            DBOS.launch()
            executor = DurableExecutor.__new__(DurableExecutor)
            executor.slots = Mock()
            executor.domain_turns = turns
            executor._DBOS = DBOS
            return executor

        executor = launch()
        try:
            with self.repo.transaction() as connection:
                self.transport.enqueue_film(connection, *args)
            self.assertTrue(called.wait(10))
            self.assertTrue(sleeping.wait(10))
            ident = self.workflows()[0]["workflow_uuid"]
            started = time.monotonic()
        finally:
            executor.close()
        self.assertLess(time.monotonic() - started, 25)
        self.assertEqual(self.workflows()[0]["status"], "PENDING")
        self.assertEqual(self.workflows()[0]["workflow_uuid"], ident)
        finish.set()
        executor = launch()
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and self.workflows()[0]["status"] != "SUCCESS":
                time.sleep(.05)
            self.assertEqual(self.workflows()[0]["status"], "SUCCESS")
            self.assertEqual(self.workflows()[0]["workflow_uuid"], ident)
            self.assertEqual(len(self.workflows()), 1)
        finally:
            executor.close()

    def test_unknown_sdk_send_response_rolls_back_sdk_and_money_together(self):
        client = self.transport.client
        proxy = Mock(wraps=client)

        def lost_response(*args, **kwargs):
            client.send_in_transaction(*args, **kwargs)
            raise RuntimeError("test_response_lost_after_actual_sdk_insert")

        proxy.send_in_transaction.side_effect = lost_response
        self.transport.client = proxy
        try:
            with self.assertRaises(RuntimeError):
                self.job()
        finally:
            self.transport.client = client
        self.assertEqual(self.workflows(), [])
        self.assertEqual(self.repo.get_budget("test-budget")["reserved_microusd"], 0)
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(jobs.select()).all(), [])
            self.assertEqual(connection.execute(budget_reservations.select()).all(), [])

    def test_film_enqueue_uses_original_transaction_and_resume_identity(self):
        from studio_platform.film_runs import VERSION as FILM_VERSION

        client = self.transport.client
        client.register_queue(FILM_VERSION, polling_interval_sec=10)
        args = ("test", "superdan", "project", "original-film")
        with self.repo.transaction() as connection:
            self.transport.enqueue_film(connection, *args)
            self.assertEqual(self.transport.film_status(connection, *args), 'ENQUEUED')
        original = self.workflows()[0]["workflow_uuid"]
        with self.repo.transaction() as connection:
            self.transport.enqueue_film(connection, *args)
        self.assertEqual(len(self.workflows()), 1)
        self.assertEqual(self.workflows()[0]["name"], FILM_VERSION)
        with self.repo.transaction() as connection:
            connection.execute(
                text(
                    "UPDATE inkseq_workflows.workflow_status SET status='ERROR' WHERE workflow_uuid=:id"
                ),
                {"id": original},
            )
            self.assertEqual(self.transport.film_status(connection, *args), 'ERROR')
            self.assertIsNone(
                self.transport.film_status(
                    connection, 'test', 'foreign', 'project', 'original-film'
                )
            )
        with self.assertRaises(RuntimeError):
            with self.repo.transaction() as connection:
                self.transport.enqueue_film(connection, *args)
                raise RuntimeError("test_confirmed_original_transaction_rollback")
        self.assertEqual(len(self.workflows()), 1)
        with self.repo.transaction() as connection:
            self.transport.enqueue_film(connection, *args)
            self.assertEqual(self.transport.film_status(connection, *args), 'ENQUEUED')
        rows = self.workflows()
        self.assertEqual(len(rows), 2)
        current = next(row for row in rows if row["status"] == "ENQUEUED")
        self.assertTrue(current["workflow_uuid"].startswith(original + "/"))
        with self.repo.transaction() as connection:
            self.transport.enqueue_film(connection, *args)
        self.assertEqual(len(self.workflows()), 2)
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(jobs.select()).all(), [])
            self.assertEqual(connection.execute(budget_reservations.select()).all(), [])

    def test_terminal_sdk_error_needs_original_admission_not_a_timer_epoch(self):
        job = self.job()
        first = self.workflows()[0]["workflow_uuid"]
        with self.repo.transaction() as connection:
            connection.execute(
                text(
                    "UPDATE inkseq_workflows.workflow_status SET status='ERROR' WHERE workflow_uuid=:id"
                ),
                {"id": first},
            )
            self.transport.job_event(
                connection,
                job["id"],
                "timer-observation",
                event_type="job.lease_expired",
            )
        self.assertEqual(len(self.workflows()), 1)
        with self.repo.transaction() as connection:
            self.transport.job_event(
                connection,
                job["id"],
                "original-explicit-requeue",
                event_type="job.queued",
            )
        rows = self.workflows()
        self.assertEqual(len(rows), 2)
        active = [row for row in rows if row["status"] == "ENQUEUED"]
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0]["workflow_uuid"].startswith(first + "/"))
        with self.repo.transaction() as connection:
            self.transport.job_event(
                connection,
                job["id"],
                "same-active-lease-event",
                event_type="job.lease_expired",
            )
        self.assertEqual(len(self.workflows()), 2)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 0)
        self.assertEqual(
            self.repo.get_budget("test-budget")["reserved_microusd"], 100_000
        )


if __name__ == "__main__":
    unittest.main()
