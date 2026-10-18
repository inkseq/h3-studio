"""Direct PostgreSQL wake consumers use only original fenced ledger state."""

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib.util
import threading
import unittest
from unittest.mock import Mock, patch
from sqlalchemy import select, update
from studio_platform.control import WorkerControl, WorkerSpec, worker_spec_payload
from studio_platform.postgres_dispatch import PostgresSlotRunner, ROUTE
from studio_platform.repository import Conflict, dispatch_receipts, outbox, jobs
from studio_platform.queue import TaskQueue
from studio_platform.storage import LocalObjectStore
from studio_platform.worker import MockBackend, Outcome, SubmissionUncertain
from test_platform_repository import LedgerCase


class MemoryBackend:
    enabled, kind, slot_key = True, "mock", "test-memory-slot"

    def __init__(self, *, lose_response=False):
        self.submissions, self.tasks = [], {}
        self.lose_response = lose_response

    def prepare(self, job, tag, store, heartbeat):
        return {}

    def submit(self, prepared, tag):
        self.submissions.append(tag)
        self.tasks[tag] = "task-" + tag
        if self.lose_response:
            raise SubmissionUncertain("lost-response")
        return self.tasks[tag]

    def poll(self, tag, task_id):
        return Outcome("running", task_id)

    def reconcile(self, tag, task_id=None):
        return Outcome("running", self.tasks.get(tag))


class CountingMock(MockBackend):
    def __init__(self, *a, lose_response=False, **kw):
        super().__init__(*a, **kw)
        self.submits = self.fetches = 0
        self.lose_response = lose_response

    def submit(self, prepared, tag):
        self.submits += 1
        task_id = super().submit(prepared, tag)
        if self.lose_response:
            raise SubmissionUncertain("lost-response")
        return task_id

    def fetch(self, *a, **kw):
        self.fetches += 1
        return super().fetch(*a, **kw)


class PostgresDispatchTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.control = WorkerControl(self.repo)
        self.directory = Path(self.temp.name)
        self.store = LocalObjectStore(self.directory / "objects")

    def make_job(
        self,
        key="job",
        *,
        dispatch=ROUTE,
        status="queued",
        audio=False,
        scope=None,
        source_bytes=0,
    ):
        scope = scope or self.scope
        request = {
            "recipe_id": "test-recipe",
            "request": {
                "model": "SIMULATION",
                "prompt": "test only",
                "duration": 4,
                "resolution": "custom",
                "width": 256,
                "height": 256,
                "generate_audio": audio,
                "export_crf": 18,
            },
            "output_spec": {"width": 256, "height": 256},
            "assets": {},
        }
        if source_bytes:
            request["hidden_sources"] = "x" * source_bytes
        execution = {
            "pool": "hatchet-test",
            "backend": "mock",
            "enabled": True,
            "configuration_id": "test-config",
            "expected_runtime_s": 1,
        }
        if dispatch is not None:
            execution["dispatch_backend"] = dispatch
        plan = self.repo.create_plan(
            scope,
            request,
            execution,
            expires_at=self.now + 1000,
            estimated_cost_microusd=100_000 if scope == self.scope else 0,
        )
        return self.repo.create_job(
            scope,
            plan["id"],
            key,
            initial_status=status,
            budget_account_ids=["owner-budget"] if scope == self.scope else [],
        )

    def ready(self, worker="worker", dispatch=ROUTE):
        spec = WorkerSpec(
            worker,
            "hatchet-test",
            "mock",
            "instance-" + worker,
            ("gpu-0",),
            ("test-recipe",),
            "SIMULATION",
            "test-config",
            "mock",
            dispatch_backend=dispatch,
        )
        self.control.register(spec)
        self.control.mark_ready(worker, upstream_idle_confirmed=True)
        return spec

    def runner(self, backend, *legacy_args, worker="worker", **kwargs):
        if legacy_args:
            backend = legacy_args[0]
        return PostgresSlotRunner(
            self.repo,
            self.store,
            self.directory / worker,
            backend=backend,
            control=self.control,
            collection_lock_dir=self.directory / "collection-lock",
            submission_guard=lambda _: True,
            **kwargs,
        )

    def test_original_route_and_budget_remain_immutable_without_delivery_receipts(self):
        job = self.make_job()
        self.ready()
        backend = MemoryBackend()
        before = self.repo.get_job(self.scope, job["id"])
        runner = self.runner(backend)
        runner.run_once("worker", "hatchet-test")
        after = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(after["execution_plan"], before["execution_plan"])
        self.assertEqual(after["request_hash"], before["request_hash"])
        self.assertEqual(after["attempt_no"], 1)
        with self.repo.engine.connect() as c:
            self.assertEqual(c.execute(select(dispatch_receipts)).all(), [])
            self.assertEqual(
                c.execute(
                    select(outbox).where(
                        outbox.c.event_type == "job.dispatch_requested"
                    )
                ).all(),
                [],
            )

    def test_duplicate_wakes_and_restart_reconcile_same_attempt_without_post(self):
        job = self.make_job()
        self.ready()
        backend = MemoryBackend(lose_response=True)
        self.runner(backend).run_once("worker", "hatchet-test")
        first = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(first["status"], "submission_unknown")
        for _ in range(3):
            self.runner(backend).run_once("worker", "hatchet-test")
        self.now += 31
        self.runner(backend).run_once("worker", "hatchet-test")
        result = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(result["current_attempt_id"], first["current_attempt_id"])
        self.assertEqual(result["attempt_no"], 1)
        self.assertEqual(len(backend.submissions), 1)

    def test_delayed_retry_timer_and_registration_bound_without_notification(self):
        job = self.make_job()
        self.ready()
        with self.repo.transaction() as c:
            c.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(not_before=self.now + 7)
            )
        runner = self.runner(MemoryBackend())
        self.assertEqual(runner.run_once("worker", "hatchet-test")["state"], "idle")
        self.assertEqual(runner.wait_seconds("worker", "hatchet-test"), 7)
        self.now += 7
        self.assertEqual(runner.run_once("worker", "hatchet-test")["job_id"], job["id"])
        self.assertLessEqual(runner.wait_seconds("worker", "hatchet-test"), 30)

    def test_subscribe_precedes_scan_and_reconnect_rescans_durable_rows(self):
        self.ready()
        order = []
        outer = self

        class Wake:
            def __init__(self, repo, stop):
                self.stop = stop
                self.waits = 0

            def subscribe(self):
                order.append("subscribe")

            def wait(self, seconds):
                self.waits += 1
                if self.waits == 1:
                    outer.make_job()
                    order.append("reconnected")
                else:
                    self.stop.set()

            def close(self):
                order.append("close")

            def interrupt(self):
                self.stop.set()

        backend = MemoryBackend()
        runner = self.runner(backend, wakeup_factory=Wake)
        original = runner.run_once

        def run(*args):
            order.append("scan")
            return original(*args)

        with patch.object(runner, "run_once", side_effect=run):
            runner.run_forever("worker", "hatchet-test")
        self.assertEqual(order, ["subscribe", "scan", "reconnected", "scan", "close"])
        self.assertEqual(len(backend.submissions), 1)

    def test_original_worker_affinity_blocks_other_slot_reconciliation(self):
        job = self.make_job()
        self.ready("one")
        self.ready("two")
        backend = MemoryBackend()
        self.runner(backend, worker="one").run_once("one", "hatchet-test")
        self.now += 31
        self.assertEqual(
            self.runner(backend, worker="two").run_once("two", "hatchet-test")["state"],
            "idle",
        )
        self.assertEqual(len(backend.submissions), 1)

    def test_collection_uses_shared_serial_lock(self):
        runner = self.runner(MemoryBackend())
        from contextlib import contextmanager

        calls = []

        @contextmanager
        def lock(path, heartbeat):
            calls.append(path)
            yield

        with (
            patch("studio_platform.postgres_dispatch.collection_slot", lock),
            patch(
                "studio_platform.worker.WorkerRunner._collect", return_value="collected"
            ),
        ):
            self.assertEqual(
                runner._collect({}, None, "tag", "task", lambda: None), "collected"
            )
        self.assertEqual(calls, [self.directory / "collection-lock"])

    def test_legacy_hash_and_claims_remain_compatible_and_routes_do_not_cross(self):
        spec = self.ready("legacy", "legacy")
        self.assertNotIn("dispatch_backend", worker_spec_payload(spec))
        self.assertEqual(
            worker_spec_payload(replace(spec, dispatch_backend=ROUTE))[
                "dispatch_backend"
            ],
            ROUTE,
        )
        legacy = self.make_job("legacy", dispatch=None)
        modern = self.make_job("modern")
        self.ready("modern")
        self.assertFalse(self.control.matches(self.control.get("legacy"), modern))
        self.assertFalse(self.control.matches(self.control.get("modern"), legacy))
        self.assertEqual(
            self.control.claim("legacy", "hatchet-test").job["id"], legacy["id"]
        )
        self.assertEqual(
            self.control.claim("modern", "hatchet-test").job["id"], modern["id"]
        )

    def test_queue_route_separation_applies_without_control(self):
        modern = self.make_job()
        queue = TaskQueue(self.repo)
        self.assertIsNone(queue.claim("legacy", "hatchet-test"))
        self.assertEqual(
            queue.claim("modern", "hatchet-test", dispatch_backend=ROUTE).job["id"],
            modern["id"],
        )

    def test_invalid_dispatch_route_is_rejected_before_acceptance(self):
        with self.assertRaisesRegex(ValueError, "invalid_dispatch_backend"):
            self.make_job(dispatch="other")

    def test_duplicate_dispatch_to_competing_workers_submits_upstream_once(self):
        job = self.make_job()
        self.ready("one")
        self.ready("two")
        backend = MemoryBackend()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda worker: self.runner(job, backend, worker=worker).run_once(
                        worker, "hatchet-test"
                    ),
                    ("one", "two"),
                )
            )
        current = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(current["attempt_no"], 1)
        self.assertEqual(len(backend.submissions), 1)
        self.assertEqual(
            sum(
                self.control.get(worker)["current_job_id"] == job["id"]
                for worker in ("one", "two")
            ),
            1,
        )

    def test_lost_remote_submission_response_reconciles_original_attempt_without_second_submit(
        self,
    ):
        job = self.make_job()
        self.ready()
        backend = MemoryBackend(lose_response=True)
        runner = self.runner(job, backend)
        self.assertEqual(
            runner.run_once("worker", "hatchet-test")["state"], "submission_unknown"
        )
        attempt = self.repo.get_job(self.scope, job["id"])["current_attempt_id"]
        self.assertEqual(self.control.get("worker")["state"], "unknown")
        self.assertEqual(runner.run_once("worker", "hatchet-test")["state"], "running")
        self.assertEqual(
            self.repo.get_job(self.scope, job["id"])["current_attempt_id"], attempt
        )
        self.assertEqual(len(backend.submissions), 1)

    def test_ledger_lease_expiry_fences_old_executor_and_preserves_upstream_attempt(
        self,
    ):
        job = self.make_job()
        self.ready()
        claim = self.control.claim("worker", "hatchet-test", lease_seconds=60)
        queue = TaskQueue(self.repo)
        queue.begin_submission(claim.lease)
        self.now += 61
        queue.recover_expired()
        current = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(current["status"], "submission_unknown")
        self.assertEqual(current["current_attempt_id"], claim.lease.attempt_id)
        self.assertEqual(current["attempt_no"], 1)
        with self.assertRaises(Conflict):
            queue.record_submitted(claim.lease, "task-original")
        self.assertIsNone(
            self.control.claim("worker", "hatchet-test", purpose="generate")
        )

    def test_upload_completed_database_commit_loss_reuses_artifact_receipt_and_original_generation(
        self,
    ):
        job = self.make_job(audio=True)
        self.ready()
        backend = CountingMock(self.directory / "mock", enabled=True)
        runner = self.runner(job, backend)
        actual_complete = runner.queue.complete
        with patch.object(
            runner.queue,
            "complete",
            side_effect=RuntimeError("simulated commit interruption"),
        ):
            self.assertEqual(
                runner.run_once("worker", "hatchet-test")["state"], "collecting"
            )
        original = self.repo.get_job(self.scope, job["id"])["current_attempt_id"]
        self.now += 31
        result = runner.run_once("worker", "hatchet-test")
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(backend.submits, 1)
        self.assertEqual(backend.fetches, 1)
        self.assertEqual(
            self.repo.get_job(self.scope, job["id"])["current_attempt_id"], original
        )
        artifacts = self.repo.get_job(self.scope, job["id"])["result"]["artifact_ids"]
        self.assertEqual(len(artifacts), 2)

    def test_direct_claim_preserves_owner_fairness(self):
        active = self.make_job("active")
        self.now += 1
        same = self.make_job("same")
        self.now += 1
        other = self.make_job("other", scope=self.other)
        self.ready("one")
        self.ready("two")
        self.assertEqual(
            self.runner(MemoryBackend(), worker="one").run_once("one", "hatchet-test")[
                "job_id"
            ],
            active["id"],
        )
        self.assertEqual(
            self.runner(MemoryBackend(), worker="two").run_once("two", "hatchet-test")[
                "job_id"
            ],
            other["id"],
        )
        self.assertEqual(self.repo.get_job(self.scope, same["id"])["attempt_no"], 0)

    def test_real_postgres_reconnect_scans_job_committed_while_disconnected(self):
        if self.repo.engine.dialect.name != "postgresql":
            self.skipTest("requires explicit isolated local PostgreSQL")
        from studio_platform.demand_wakeup import DemandWakeup

        self.ready()
        wake = DemandWakeup(self.repo, threading.Event())
        try:
            self.assertTrue(wake.subscribe())
            wake._disconnect()
            job = self.make_job()
            self.assertEqual(wake.wait(0.01), "reconnected")
            backend = MemoryBackend()
            result = self.runner(backend).run_once("worker", "hatchet-test")
            self.assertEqual(result["job_id"], job["id"])
            self.assertEqual(len(backend.submissions), 1)
        finally:
            wake.close()

    def test_signal_restart_recovers_first_attempt_then_next_queued_job(self):
        import signal

        first = self.make_job("first")
        self.now += 1
        second = self.make_job("second")
        self.ready()
        backend = CountingMock(self.directory / "signal-backend", enabled=True)
        original_poll = backend.poll
        signalled = []

        def poll(tag, task_id):
            if not signalled:
                signalled.append(True)
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                return Outcome("running", task_id)
            return original_poll(tag, task_id)

        backend.poll = poll
        runner = self.runner(backend)
        runner.run_forever("worker", "hatchet-test")
        before = self.repo.get_job(self.scope, first["id"])
        self.assertEqual(before["status"], "running")
        self.assertEqual(self.control.get("worker")["drain_requested"], 0)
        self.assertEqual(self.repo.get_job(self.scope, second["id"])["attempt_no"], 0)
        restarted = self.runner(backend)
        for _ in range(6):
            self.now += 6
            restarted.run_once("worker", "hatchet-test")
        after = self.repo.get_job(self.scope, first["id"])
        next_job = self.repo.get_job(self.scope, second["id"])
        self.assertEqual(
            (after["status"], next_job["status"]), ("succeeded", "succeeded")
        )
        self.assertEqual(after["current_attempt_id"], before["current_attempt_id"])
        self.assertEqual(
            (after["attempt_no"], next_job["attempt_no"], backend.submits), (1, 1, 2)
        )
        self.assertEqual(after["execution_plan"], first["execution_plan"])

    def test_signal_does_not_clear_concurrent_owner_drain_or_directory_flag(self):
        import signal

        for source in ("owner", "flag"):
            worker = "signal-" + source
            self.ready(worker)
            flag = self.directory / (source + ".flag")
            outer = self

            class Wake:
                def __init__(self, *args):
                    pass

                def subscribe(self):
                    pass

                def wait(self, seconds):
                    if source == "owner":
                        outer.control.drain(worker)
                    else:
                        flag.touch()
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

                def interrupt(self):
                    pass

                def close(self):
                    pass

            self.runner(
                MemoryBackend(),
                worker=worker,
                wakeup_factory=Wake,
                stop_requested=flag.exists,
            ).run_forever(worker, "hatchet-test")
            self.assertEqual(self.control.get(worker)["drain_requested"], 1)
