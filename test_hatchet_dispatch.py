"""Isolated ledgers/CPU media only; no broker/provider/GPU network operations."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib.util
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import event, select, update

from studio_platform.control import WorkerControl, WorkerSpec, worker_spec_payload
from studio_platform.hatchet_dispatch import (BrokerConfig, DispatchInput, EVENT,
    ExactJobRunner, OutboxDispatcher, ROUTE, SDKPublisher, binding, workflow_name)
from studio_platform.queue import TaskQueue
from studio_platform.repository import Conflict, dispatch_receipts, outbox, request_hash
from studio_platform.storage import LocalObjectStore
from studio_platform.worker import MockBackend, Outcome, SubmissionUncertain
from test_platform_repository import LedgerCase


class Publisher:
    def __init__(self, *, lose_first=False, gate=None):
        self.calls = []
        self.runs = {}
        self.states = {}
        self.lose_first, self.gate = lose_first, gate
        self.lock = threading.Lock()

    def publish(self, message, job):
        with self.lock:
            self.calls.append(message)
            self.runs.setdefault(message.event_id, "run-"+message.event_id)
            run_id = self.runs[message.event_id]
            self.states.setdefault(run_id, "QUEUED")
            unknown = self.lose_first and len(self.calls) == 1
        if self.gate:
            self.gate[0].set()
            self.gate[1].wait(5)
        if unknown:
            raise TimeoutError("secret broker detail must not escape")
        return run_id

    def status(self, run_id):
        return self.states[run_id]


class MemoryBackend:
    enabled, kind, slot_key = True, "mock", "test-memory-slot"
    def __init__(self, *, lose_response=False):
        self.submissions, self.tasks = [], {}
        self.lose_response = lose_response

    def prepare(self, job, tag, store, heartbeat):
        return {}

    def submit(self, prepared, tag):
        self.submissions.append(tag)
        self.tasks[tag] = "task-"+tag
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


class HatchetDispatchTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.control = WorkerControl(self.repo)
        self.directory = Path(self.temp.name)
        self.store = LocalObjectStore(self.directory/"objects")

    def make_job(self, key="job", *, dispatch=ROUTE, status="queued", audio=False, scope=None, source_bytes=0):
        scope = scope or self.scope
        request = {"recipe_id": "test-recipe", "request": {"model": "SIMULATION", "prompt": "test only",
            "duration": 4, "resolution": "custom", "width": 256, "height": 256,
            "generate_audio": audio, "export_crf": 18},
            "output_spec": {"width": 256, "height": 256}, "assets": {}}
        if source_bytes:
            request["hidden_sources"] = "x" * source_bytes
        execution = {"pool": "hatchet-test", "backend": "mock", "enabled": True,
            "configuration_id": "test-config", "expected_runtime_s": 1}
        if dispatch is not None:
            execution["dispatch_backend"] = dispatch
        plan = self.repo.create_plan(scope, request, execution,
            expires_at=self.now+1000, estimated_cost_microusd=100_000 if scope == self.scope else 0)
        return self.repo.create_job(scope, plan["id"], key, initial_status=status,
            budget_account_ids=["owner-budget"] if scope == self.scope else [])

    def ready(self, worker="worker", dispatch=ROUTE):
        spec = WorkerSpec(worker, "hatchet-test", "mock", "instance-"+worker,
            ("gpu-0",), ("test-recipe",), "SIMULATION", "test-config", "mock", dispatch_backend=dispatch)
        self.control.register(spec)
        self.control.mark_ready(worker, upstream_idle_confirmed=True)
        return spec

    def message(self, job):
        with self.repo.engine.connect() as connection:
            event = connection.execute(select(outbox).where(outbox.c.aggregate_id == job["id"],
                outbox.c.event_type == EVENT).order_by(outbox.c.created_at)).mappings().first()
        return DispatchInput(event_id=event["id"], **event["payload"])

    def runner(self, job, backend, *, worker="worker", message=None):
        return ExactJobRunner(self.repo, self.store, self.directory/worker,
            message=message or self.message(job), backend=backend, control=self.control,
            collection_lock_dir=self.directory/"collection-lock", submission_guard=lambda _: True)

    def dispatcher(self, publisher, identifier="test-dispatcher"):
        return OutboxDispatcher(self.repo, publisher, publisher_id=identifier)

    def test_legacy_hash_and_claims_remain_compatible_and_routes_do_not_cross(self):
        spec = self.ready("legacy", "legacy")
        self.assertNotIn("dispatch_backend", worker_spec_payload(spec))
        self.assertEqual(worker_spec_payload(replace(spec, dispatch_backend=ROUTE))["dispatch_backend"], ROUTE)
        legacy = self.make_job("legacy", dispatch=None)
        modern = self.make_job("modern")
        self.ready("modern")
        self.assertFalse(self.control.matches(self.control.get("legacy"), modern))
        self.assertFalse(self.control.matches(self.control.get("modern"), legacy))
        self.assertEqual(self.control.claim("legacy", "hatchet-test").job["id"], legacy["id"])
        self.assertEqual(self.control.claim("modern", "hatchet-test").job["id"], modern["id"])

    def test_queue_route_separation_applies_without_control(self):
        modern = self.make_job()
        queue = TaskQueue(self.repo)
        self.assertIsNone(queue.claim("legacy", "hatchet-test"))
        self.assertEqual(queue.claim("modern", "hatchet-test", dispatch_backend=ROUTE).job["id"], modern["id"])

    def test_invalid_dispatch_route_is_rejected_before_acceptance(self):
        with self.assertRaisesRegex(ValueError, "invalid_dispatch_backend"):
            self.make_job(dispatch="other")

    def test_only_runnable_acceptance_emits_identity_only_wakeup(self):
        job = self.make_job()
        planned = self.make_job("planned", status="planned")
        legacy = self.make_job("legacy", dispatch=None)
        with self.repo.engine.connect() as connection:
            events = list(connection.execute(select(outbox).where(outbox.c.event_type == EVENT)).mappings())
        self.assertEqual([event["aggregate_id"] for event in events], [job["id"]])
        self.assertEqual(set(events[0]["payload"]), {"version", "job_id", "request_hash", "plan_hash"})
        self.repo.enqueue(self.scope, planned["id"], budget_account_ids=["owner-budget"])
        self.assertEqual(self.message(planned).job_id, planned["id"])

    def test_outbox_delivery_ack_does_not_consume_general_business_events(self):
        job = self.make_job()
        result = self.dispatcher(Publisher()).publish_once()
        self.assertEqual(result["state"], "published")
        events = self.repo.pending_events()
        self.assertIn("job.created", {event["event_type"] for event in events})
        self.assertNotIn(EVENT, {event["event_type"] for event in events})
        with self.repo.engine.connect() as connection:
            receipt = connection.execute(select(dispatch_receipts)).mappings().one()
        self.assertEqual(receipt["job_id"], job["id"])
        self.assertEqual(receipt["external_run_id"], result["external_run_id"])

    def test_concurrent_publishers_are_leased_without_blocking_network_inside_transaction(self):
        self.make_job()
        entered, release = threading.Event(), threading.Event()
        publisher = Publisher(gate=(entered, release))
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.dispatcher(publisher, "first").publish_once)
            self.assertTrue(entered.wait(3))
            second = executor.submit(self.dispatcher(publisher, "second").publish_once)
            self.assertEqual(second.result(timeout=3)["state"], "idle")
            release.set()
            self.assertEqual(first.result(timeout=3)["state"], "published")
        self.assertEqual(len(publisher.calls), 1)

    def test_unknown_publish_reuses_event_id_and_does_not_create_new_job(self):
        job = self.make_job()
        publisher = Publisher(lose_first=True)
        dispatcher = self.dispatcher(publisher)
        unknown = dispatcher.publish_once()
        self.assertEqual(unknown["state"], "unknown")
        self.assertEqual(dispatcher.publish_once()["state"], "idle")
        self.now += 31
        accepted = dispatcher.publish_once()
        self.assertEqual(accepted["state"], "published")
        self.assertEqual(unknown["event_id"], accepted["event_id"])
        self.assertEqual(len(publisher.runs), 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 0)

    def test_exact_delivery_yields_to_older_job_without_stealing_or_creating_attempt(self):
        older = self.make_job("older")
        self.now += 1
        intended = self.make_job("intended")
        self.ready()
        backend = MemoryBackend()
        result = self.runner(intended, backend).run_once("worker", "hatchet-test")
        self.assertEqual(result["state"], "idle")
        self.assertEqual(self.repo.get_job(self.scope, older["id"])["status"], "queued")
        self.assertEqual(self.repo.get_job(self.scope, intended["id"])["attempt_no"], 0)
        self.assertEqual(backend.submissions, [])
        self.assertEqual(self.runner(older, backend).run_once("worker", "hatchet-test")["job_id"], older["id"])

    def test_already_published_jobs_obey_original_owner_fairness_at_claim(self):
        active = self.make_job("active")
        self.now += 1
        same_owner = self.make_job("same-owner")
        self.now += 1
        other_owner = self.make_job("other-owner", scope=self.other)
        publisher = Publisher()
        dispatcher = self.dispatcher(publisher)
        for _ in range(3):
            self.assertEqual(dispatcher.publish_once()["state"], "published")
        self.ready("one")
        self.ready("two")
        backend = MemoryBackend()
        self.runner(active, backend, worker="one").run_once("one", "hatchet-test")
        self.assertEqual(self.runner(same_owner, backend, worker="two").run_once("two", "hatchet-test")["state"], "idle")
        self.assertEqual(self.control.get("two")["state"], "ready")
        self.assertEqual(self.repo.get_job(self.scope, same_owner["id"])["attempt_no"], 0)
        result = self.runner(other_owner, backend, worker="two").run_once("two", "hatchet-test")
        self.assertEqual(result["job_id"], other_owner["id"])
        self.assertEqual(len(backend.submissions), 2)

    def test_aged_exact_job_keeps_original_fifo_priority_over_new_owner(self):
        active = self.make_job("active")
        self.ready("one")
        backend = MemoryBackend()
        self.runner(active, backend, worker="one").run_once("one", "hatchet-test")
        older = self.make_job("aged")
        self.now += 901
        newer = self.make_job("new-owner", scope=self.other)
        self.ready("two")
        result = self.runner(older, backend, worker="two").run_once("two", "hatchet-test")
        self.assertEqual(result["job_id"], older["id"])
        self.assertEqual(self.repo.get_job(self.other, newer["id"])["attempt_no"], 0)

    def test_running_broker_delivery_is_not_reenqueued_on_every_gpu_poll(self):
        job = self.make_job()
        publisher = Publisher()
        dispatcher = self.dispatcher(publisher)
        dispatched = dispatcher.publish_once()
        publisher.states[dispatched["external_run_id"]] = "RUNNING"
        self.ready()
        runner = self.runner(job, MemoryBackend())
        for _ in range(3):
            runner.run_once("worker", "hatchet-test")
            self.now += 6
        with self.repo.engine.connect() as connection:
            events = list(connection.execute(select(outbox.c.id).where(outbox.c.event_type == EVENT)).scalars())
        self.assertEqual(len(events), 1)
        self.assertEqual(dispatcher.recover_wakeups()["wakeups"], 0)

    def test_duplicate_dispatch_to_competing_workers_submits_upstream_once(self):
        job = self.make_job()
        self.ready("one")
        self.ready("two")
        backend = MemoryBackend()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda worker: self.runner(job, backend, worker=worker)
                .run_once(worker, "hatchet-test"), ("one", "two")))
        current = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(current["attempt_no"], 1)
        self.assertEqual(len(backend.submissions), 1)
        self.assertEqual(sum(self.control.get(worker)["current_job_id"] == job["id"] for worker in ("one", "two")), 1)

    def test_lost_remote_submission_response_reconciles_original_attempt_without_second_submit(self):
        job = self.make_job()
        self.ready()
        backend = MemoryBackend(lose_response=True)
        runner = self.runner(job, backend)
        self.assertEqual(runner.run_once("worker", "hatchet-test")["state"], "submission_unknown")
        attempt = self.repo.get_job(self.scope, job["id"])["current_attempt_id"]
        self.assertEqual(self.control.get("worker")["state"], "unknown")
        self.assertEqual(runner.run_once("worker", "hatchet-test")["state"], "running")
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["current_attempt_id"], attempt)
        self.assertEqual(len(backend.submissions), 1)

    def test_ledger_lease_expiry_fences_old_executor_and_preserves_upstream_attempt(self):
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
        self.assertIsNone(self.control.claim("worker", "hatchet-test", purpose="generate"))

    def test_mixed_lease_expiry_wakeup_reads_only_hatchet_plan_and_preserves_original_attempt(self):
        legacy = self.make_job("large-legacy", dispatch=None, source_bytes=256*1024)
        modern = self.make_job("large-modern", source_bytes=256*1024)
        self.ready("legacy", "legacy")
        self.ready("modern")
        legacy_claim = self.control.claim("legacy", "hatchet-test", lease_seconds=60)
        modern_claim = self.control.claim("modern", "hatchet-test", lease_seconds=60)
        queue = TaskQueue(self.repo)
        queue.begin_submission(modern_claim.lease)
        initial = self.message(modern)
        # Positive broker terminal evidence has already closed this delivery;
        # the expired original inference may now receive a recovery wakeup.
        with self.repo.transaction() as connection:
            connection.execute(update(outbox).where(outbox.c.id == initial.event_id).values(delivered_at=self.now))
            connection.execute(update(dispatch_receipts).where(dispatch_receipts.c.event_id == initial.event_id)
                .values(state="finished"))
        self.now += 61
        selected_columns = []
        def capture(_connection, _cursor, _statement, _parameters, context, _executemany):
            sql = context.compiled.statement if context.compiled is not None else None
            if sql is not None and getattr(sql, "is_select", False):
                selected_columns.extend(sql.selected_columns.keys())
        event.listen(self.repo.engine, "before_cursor_execute", capture)
        try:
            recovered = queue.recover_expired(summary=True)
        finally:
            event.remove(self.repo.engine, "before_cursor_execute", capture)
        states = {row["id"]: row["status"] for row in recovered}
        self.assertEqual(states, {legacy["id"]: "queued", modern["id"]: "submission_unknown"})
        self.assertNotIn("request", selected_columns)
        self.assertNotIn("result", selected_columns)
        self.assertEqual(selected_columns.count("execution_plan"), 1)
        with self.repo.engine.connect() as connection:
            wakeups = list(connection.execute(select(outbox).where(outbox.c.event_type == EVENT,
                outbox.c.delivered_at.is_(None))).mappings())
        self.assertEqual(len(wakeups), 1)
        self.assertEqual(wakeups[0]["aggregate_id"], modern["id"])
        self.assertEqual(wakeups[0]["payload"], {"version": 1, "job_id": modern["id"],
            "request_hash": modern["request_hash"], "plan_hash": request_hash(modern["execution_plan"])})
        self.assertEqual(self.repo.get_job(self.scope, modern["id"])["current_attempt_id"], modern_claim.lease.attempt_id)
        self.assertEqual(self.repo.get_job(self.scope, legacy["id"])["current_attempt_id"], legacy_claim.lease.attempt_id)
        self.assertEqual(self.repo.get_job(self.scope, modern["id"])["attempt_no"], 1)

    def test_broker_failure_reawakens_original_job_but_unknown_broker_status_does_not(self):
        job = self.make_job()
        self.ready()
        publisher = Publisher()
        dispatcher = self.dispatcher(publisher)
        first = dispatcher.publish_once()
        self.assertEqual(dispatcher.recover_wakeups()["wakeups"], 0)
        publisher.states[first["external_run_id"]] = "FAILED"
        self.assertEqual(dispatcher.recover_wakeups()["wakeups"], 1)
        second = dispatcher.publish_once()
        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertEqual(second["job_id"], job["id"])
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 0)

    def test_recovery_cursor_does_not_starve_jobs_beyond_running_prefix(self):
        publisher = Publisher()
        dispatcher = self.dispatcher(publisher)
        for index in range(5):
            self.now += 1
            job = self.make_job("paged-"+str(index))
            delivery = dispatcher.publish_once()
            if index == 4:
                self.ready("last")
                publisher.states[delivery["external_run_id"]] = "FAILED"
            else:
                worker = "prefix-"+str(index)
                self.ready(worker)
                self.runner(job, MemoryBackend(), worker=worker).run_once(worker, "hatchet-test")
        self.now += 6  # Original running jobs have finished their poll backoff.
        self.assertEqual(dispatcher.recover_wakeups(limit=2)["wakeups"], 0)
        self.assertEqual(dispatcher.recover_wakeups(limit=2)["wakeups"], 0)
        self.assertEqual(dispatcher.recover_wakeups(limit=2)["wakeups"], 1)

    def test_100_no_capacity_ticks_keep_one_run_then_ready_wakes_once(self):
        job = self.make_job()
        original_budget = self.repo.get_budget("owner-budget")
        publisher = Publisher()
        dispatcher = self.dispatcher(publisher)
        initial = dispatcher.publish_once()
        publisher.states[initial["external_run_id"]] = "COMPLETED"
        for _ in range(100):
            self.now += 30
            self.assertEqual(dispatcher.recover_wakeups()["wakeups"], 0)
            self.assertEqual(dispatcher.publish_once()["state"], "idle")
        self.assertEqual(len(publisher.runs), 1)
        self.assertEqual(len(publisher.calls), 1)
        self.ready()
        self.assertEqual(dispatcher.recover_wakeups()["wakeups"], 1)
        self.assertEqual(dispatcher.recover_wakeups()["wakeups"], 0)
        resumed = dispatcher.publish_once()
        self.assertNotEqual(resumed["event_id"], initial["event_id"])
        self.assertEqual(len(publisher.runs), 2)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 0)
        self.assertEqual(self.repo.get_budget("owner-budget"), original_budget)

    def test_cancel_pending_never_publishes_or_emits_another_wakeup(self):
        job = self.make_job()
        self.ready()
        claim = self.control.claim("worker", "hatchet-test")
        queue = TaskQueue(self.repo)
        queue.begin_submission(claim.lease)
        queue.mark_submission_unknown(claim.lease)
        original = self.repo.request_cancel(self.scope, job["id"])
        self.assertEqual(original["status"], "cancel_requested")
        budget = self.repo.get_budget("owner-budget")
        publisher = Publisher()
        dispatcher = self.dispatcher(publisher)
        self.assertEqual(dispatcher.publish_once()["state"], "obsolete")
        for _ in range(3):
            with self.repo.transaction() as connection:
                self.assertIsNone(self.repo._dispatch_wakeup(connection, job["id"]))
            self.assertEqual(dispatcher.recover_wakeups()["wakeups"], 0)
        self.assertEqual(publisher.calls, [])
        current = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(current["status"], "cancel_requested")
        self.assertEqual(current["current_attempt_id"], original["current_attempt_id"])
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)

    def test_cancel_pending_retains_unknown_original_publication_without_repeating_it(self):
        job = self.make_job()
        publisher = Publisher(lose_first=True)
        dispatcher = self.dispatcher(publisher)
        original = dispatcher.publish_once()
        self.assertEqual(original["state"], "unknown")
        self.ready()
        claim = self.control.claim("worker", "hatchet-test")
        queue = TaskQueue(self.repo)
        queue.begin_submission(claim.lease)
        queue.mark_submission_unknown(claim.lease)
        self.repo.request_cancel(self.scope, job["id"])
        self.now += 31
        self.assertEqual(dispatcher.publish_once()["state"], "obsolete")
        with self.repo.engine.connect() as connection:
            receipt = connection.execute(select(dispatch_receipts).where(
                dispatch_receipts.c.event_id == original["event_id"])).mappings().one()
        self.assertEqual(receipt["state"], "unknown")
        self.assertEqual(receipt["publish_attempts"], 1)
        self.assertEqual(receipt["error_code"], "dispatch_cancel_pending")
        self.assertEqual(len(publisher.calls), 1)

    def test_terminal_lower_priority_delivery_waits_for_actual_queue_order(self):
        older = self.make_job("older")
        self.now += 1
        newer = self.make_job("newer")
        self.ready()
        publisher = Publisher()
        dispatcher = self.dispatcher(publisher)
        deliveries = [dispatcher.publish_once(), dispatcher.publish_once()]
        new_delivery = next(row for row in deliveries if row["job_id"] == newer["id"])
        publisher.states[new_delivery["external_run_id"]] = "COMPLETED"
        for _ in range(10):
            self.assertEqual(dispatcher.recover_wakeups()["wakeups"], 0)
        self.assertEqual(len(publisher.runs), 2)
        self.repo.request_cancel(self.scope, older["id"])
        self.assertEqual(dispatcher.recover_wakeups()["wakeups"], 1)
        self.assertEqual(dispatcher.publish_once()["job_id"], newer["id"])
        self.assertEqual(self.repo.get_job(self.scope, newer["id"])["attempt_no"], 0)

    def test_concurrent_recovery_of_terminal_run_publishes_only_one_successor(self):
        self.make_job()
        self.ready()
        publisher = Publisher()
        initial = self.dispatcher(publisher).publish_once()
        barrier = threading.Barrier(2)
        def terminal(_run):
            barrier.wait(timeout=5)
            return "COMPLETED"
        publisher.status = terminal
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda name: self.dispatcher(publisher, name).recover_wakeups(), ("one", "two")))
        self.assertEqual(sum(result["wakeups"] for result in results), 1)
        self.assertEqual(self.dispatcher(publisher).publish_once()["state"], "published")
        self.assertEqual(len(publisher.runs), 2)
        self.assertEqual(len({call.event_id for call in publisher.calls}), 2)

    def test_cancel_during_broker_status_read_fences_successor_under_job_lock(self):
        job = self.make_job()
        self.ready()
        publisher = Publisher()
        dispatcher = self.dispatcher(publisher)
        dispatcher.publish_once()
        entered, resume = threading.Event(), threading.Event()
        def terminal(_run):
            entered.set()
            self.assertTrue(resume.wait(5))
            return "COMPLETED"
        publisher.status = terminal
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(dispatcher.recover_wakeups)
            self.assertTrue(entered.wait(5))
            self.repo.request_cancel(self.scope, job["id"])
            resume.set()
            self.assertEqual(pending.result(timeout=5)["wakeups"], 0)
        self.assertEqual(len(publisher.calls), 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["status"], "cancelled")

    def test_upload_completed_database_commit_loss_reuses_artifact_receipt_and_original_generation(self):
        job = self.make_job(audio=True)
        self.ready()
        backend = CountingMock(self.directory/"mock", enabled=True)
        runner = self.runner(job, backend)
        actual_complete = runner.queue.complete
        with patch.object(runner.queue, "complete", side_effect=RuntimeError("simulated commit interruption")):
            self.assertEqual(runner.run_once("worker", "hatchet-test")["state"], "collecting")
        original = self.repo.get_job(self.scope, job["id"])["current_attempt_id"]
        self.now += 31
        result = runner.run_once("worker", "hatchet-test")
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(backend.submits, 1)
        self.assertEqual(backend.fetches, 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["current_attempt_id"], original)
        artifacts = self.repo.get_job(self.scope, job["id"])["result"]["artifact_ids"]
        self.assertEqual(len(artifacts), 2)

    def test_identity_tampering_cannot_claim_another_snapshot(self):
        job = self.make_job()
        self.ready()
        message = self.message(job).model_copy(update={"request_hash": "0"*64})
        backend = MemoryBackend()
        self.assertEqual(self.runner(job, backend, message=message).run_once("worker", "hatchet-test")["state"], "idle")
        self.assertEqual(backend.submissions, [])

    def test_remote_cleartext_broker_and_short_timeouts_are_rejected(self):
        for changes in ({"server_url": "http://remote.invalid"}, {"host_port": "remote.invalid:7070", "tls": False},
                        {"execution_timeout_s": 60}, {"token_file": Path("relative")}):
            with self.assertRaises(ValueError):
                BrokerConfig(token_file=changes.get("token_file", self.directory/"token"),
                    server_url=changes.get("server_url", "http://127.0.0.1:8080"),
                    host_port=changes.get("host_port", "127.0.0.1:7070"),
                    tls=changes.get("tls", False), execution_timeout_s=changes.get("execution_timeout_s", 7200))

    @unittest.skipUnless(importlib.util.find_spec("hatchet_sdk"), "isolated dispatch SDK optional in business tests")
    def test_sdk_idempotency_collision_returns_original_accepted_run(self):
        from hatchet_sdk import IdempotencyCollisionError
        job = self.make_job()
        client = Mock()
        client.stubs.task.return_value.run_no_wait.side_effect = IdempotencyCollisionError("original-run")
        self.assertEqual(SDKPublisher(client, self.repo).publish(self.message(job), job), "original-run")
        submitted = client.stubs.task.return_value.run_no_wait.call_args
        self.assertTrue(all(label.required for label in submitted.kwargs["desired_worker_labels"]))
        self.assertEqual(client.stubs.task.call_args.kwargs["name"], workflow_name(job))
