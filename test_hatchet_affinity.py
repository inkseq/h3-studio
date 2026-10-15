"""Original replica affinity and prompt callback yielding; no broker/GPU calls."""
from pathlib import Path
from types import ModuleType, SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import update

from studio_platform.control import WorkerControl
from studio_platform.hatchet_dispatch import BrokerConfig, DispatchCancelled, ExactJobRunner, HatchetSlotRunner, SDKPublisher
from studio_platform.repository import jobs, registered_workers
from studio_platform.storage import LocalObjectStore
import test_hatchet_dispatch as fixtures
from test_hatchet_dispatch import CountingMock, MemoryBackend
from test_platform_repository import LedgerCase


class HatchetAffinityTests(LedgerCase):
    make_job = fixtures.HatchetDispatchTests.make_job
    ready = fixtures.HatchetDispatchTests.ready
    message = fixtures.HatchetDispatchTests.message
    runner = fixtures.HatchetDispatchTests.runner

    def setUp(self):
        super().setUp()
        self.control = WorkerControl(self.repo)
        self.directory = Path(self.temp.name)
        self.store = LocalObjectStore(self.directory / "objects")
        self.config = BrokerConfig(self.directory / "token", "http://127.0.0.1:8080",
            "127.0.0.1:7070", tls=False, poll_interval_s=.1)

    def sdk_module(self):
        module = ModuleType("hatchet_sdk")
        module.DesiredWorkerLabel = lambda **fields: SimpleNamespace(**fields)
        module.TTLBasedIdempotencyConfig = lambda **fields: SimpleNamespace(**fields)
        class Collision(Exception):
            pass
        module.IdempotencyCollisionError = Collision
        return module

    def publish(self, job):
        client = Mock()
        client.stubs.task.return_value.run_no_wait.return_value = SimpleNamespace(workflow_run_id="broker-run")
        with patch.dict("sys.modules", {"hatchet_sdk": self.sdk_module()}):
            result = SDKPublisher(client, self.repo).publish(self.message(job), job)
        self.assertEqual(result, "broker-run")
        entries = client.stubs.task.return_value.run_no_wait.call_args.kwargs["desired_worker_labels"]
        self.assertTrue(all(entry.required for entry in entries))
        return {entry.key: entry.value for entry in entries}

    def callback(self, job, backend, worker):
        sdk = SimpleNamespace()
        def task(**options):
            sdk.task_options = options
            def decorate(operation):
                sdk.operation = operation
                return operation
            return decorate
        def register(**options):
            sdk.worker_options = options
            def start():
                sdk.result = sdk.operation(self.message(job), SimpleNamespace(
                    is_cancelled=False, workflow_run_id="broker-run"))
            return SimpleNamespace(start=start)
        sdk.task, sdk.worker = task, register
        runner = HatchetSlotRunner(self.repo, self.store, self.directory / worker, backend=backend,
            control=self.control, broker_config=self.config, collection_lock_dir=self.directory / "collection-lock",
            client_factory=lambda _config: sdk, submission_guard=lambda _job: True)
        # Replace only the callback's module reference. Patching the shared
        # time.sleep function also intercepts legitimate Linux ffmpeg/Popen
        # waits and turns valid CPU media collection into a false failure.
        callback_time = SimpleNamespace(monotonic=time.monotonic,
            sleep=Mock(side_effect=AssertionError("callback did not promptly yield")))
        with patch.dict("sys.modules", {"hatchet_sdk": self.sdk_module()}), \
                patch("studio_platform.hatchet_dispatch.os", SimpleNamespace(name="posix")), \
                patch("studio_platform.hatchet_dispatch.time", callback_time):
            runner.run_forever(worker, "hatchet-test")
        callback_time.sleep.assert_not_called()
        self.assertEqual(sdk.worker_options["labels"]["sixnine_worker"], worker)
        # Workflow registration remains shared; an individual replica cannot
        # overwrite the common workflow's defaults with its own physical ID.
        self.assertNotIn("sixnine_worker", {label.key for label in sdk.task_options["desired_worker_labels"]})
        return sdk.result

    def test_new_job_uses_common_labels_and_any_replica_can_claim(self):
        job = self.make_job()
        self.ready("one")
        self.ready("two")
        self.assertNotIn("sixnine_worker", self.publish(job))
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 0)

    def test_cancel_after_delivery_selection_refuses_sdk_request_and_stale_callback(self):
        job = self.make_job()
        message = self.message(job)
        self.ready()
        self.repo.request_cancel(self.scope, job["id"])
        client = Mock()
        with patch.dict("sys.modules", {"hatchet_sdk": self.sdk_module()}):
            with self.assertRaises(DispatchCancelled):
                SDKPublisher(client, self.repo).publish(message, job)
        client.stubs.task.assert_not_called()
        backend = Mock(wraps=MemoryBackend())
        backend.kind, backend.slot_key = "mock", "cancelled-slot"
        self.assertEqual(self.callback(job, backend, "worker")["state"], "cancelled")
        backend.prepare.assert_not_called()
        backend.submit.assert_not_called()
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 0)

    def test_cleared_lease_recovery_uses_authoritative_attempt_worker(self):
        original_snapshot = self.make_job()
        self.ready("one")
        self.ready("two")
        backend = MemoryBackend(lose_response=True)
        self.assertEqual(self.runner(original_snapshot, backend, worker="one").run_once("one", "hatchet-test")["state"],
            "submission_unknown")
        current = self.repo.get_job(self.scope, original_snapshot["id"])
        self.assertIsNone(current["lease_worker_id"])
        self.assertEqual(self.publish(original_snapshot)["sixnine_worker"], "one")
        self.assertEqual(current["attempt_no"], 1)
        self.assertEqual(len(backend.submissions), 1)

    def test_wrong_replica_yields_then_original_collects_without_second_submission(self):
        job = self.make_job(audio=True)
        self.ready("one")
        self.ready("two")
        original = CountingMock(self.directory / "native-one", enabled=True, lose_response=True)
        self.assertEqual(self.runner(job, original, worker="one").run_once("one", "hatchet-test")["state"],
            "submission_unknown")
        attempt = self.repo.get_job(self.scope, job["id"])["current_attempt_id"]
        wrong = Mock(wraps=MemoryBackend())
        wrong.kind, wrong.slot_key = "mock", "native-two"
        result = self.callback(job, wrong, "two")
        self.assertEqual(result["reason_code"], "hatchet_worker_incompatible")
        wrong.prepare.assert_not_called()
        wrong.submit.assert_not_called()
        wrong.reconcile.assert_not_called()
        wrong.poll.assert_not_called()
        self.assertEqual(self.control.get("two")["state"], "ready")
        self.assertEqual(self.callback(job, original, "one"), {"job_id": job["id"], "state": "succeeded"})
        current = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(current["current_attempt_id"], attempt)
        self.assertEqual(current["attempt_no"], 1)
        self.assertEqual((original.submits, original.fetches), (1, 1))
        self.assertEqual(len(current["result"]["artifact_ids"]), 2)

    def test_busy_replica_yields_new_job_without_stealing_its_current_job(self):
        busy_job = self.make_job("busy")
        self.now += 1
        offered = self.make_job("offered")
        self.ready("one")
        original = MemoryBackend()
        self.assertEqual(self.runner(busy_job, original, worker="one").run_once("one", "hatchet-test")["job_id"],
            busy_job["id"])
        before = self.repo.get_job(self.scope, busy_job["id"])
        wrong = Mock(wraps=MemoryBackend())
        wrong.kind, wrong.slot_key = "mock", "native-one"
        self.assertEqual(self.callback(offered, wrong, "one")["reason_code"], "hatchet_worker_busy")
        wrong.prepare.assert_not_called()
        wrong.submit.assert_not_called()
        wrong.reconcile.assert_not_called()
        self.assertEqual(self.control.get("one")["current_job_id"], busy_job["id"])
        self.assertEqual(self.repo.get_job(self.scope, busy_job["id"])["current_attempt_id"], before["current_attempt_id"])
        self.assertEqual(self.repo.get_job(self.scope, offered["id"])["attempt_no"], 0)
        self.assertEqual(len(original.submissions), 1)

    def test_lost_candidate_or_admission_returns_idle_and_yields_after_one_claim(self):
        job = self.make_job()
        self.ready("one")
        with patch.object(ExactJobRunner, "run_once", return_value={"state": "idle"}) as claim:
            self.assertEqual(self.callback(job, MemoryBackend(), "one")["reason_code"], "hatchet_claim_unavailable")
        claim.assert_called_once_with("one", "hatchet-test")
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 0)

    def test_actual_callback_owner_fairness_yields_then_resumes_same_job_identity(self):
        active = self.make_job("a1-active")
        self.now += 1
        same_owner = self.make_job("a2-queued")
        self.now += 1
        other_owner = self.make_job("b-queued", scope=self.other)
        self.ready("one")
        self.ready("two")
        native_one = MemoryBackend()
        self.runner(active, native_one, worker="one").run_once("one", "hatchet-test")
        # All jobs were already eligible broker deliveries. Only the original
        # scheduler transaction may choose the owner-fair candidate.
        for job in (same_owner, other_owner):
            self.assertNotIn("sixnine_worker", self.publish(job))
        result = self.callback(same_owner, MemoryBackend(), "two")
        self.assertEqual(result["reason_code"], "hatchet_claim_unavailable")
        self.assertEqual(self.repo.get_job(self.scope, same_owner["id"])["attempt_no"], 0)
        self.assertEqual(self.repo.get_job(self.other, other_owner["id"])["attempt_no"], 0)
        self.assertEqual(self.control.get("two")["state"], "ready")
        native_two = CountingMock(self.directory / "native-two", enabled=True)
        self.assertEqual(self.callback(other_owner, native_two, "two"),
            {"job_id": other_owner["id"], "state": "succeeded"})
        self.assertEqual(self.callback(same_owner, native_two, "two"),
            {"job_id": same_owner["id"], "state": "succeeded"})
        resumed = self.repo.get_job(self.scope, same_owner["id"])
        self.assertEqual((resumed["id"], resumed["attempt_no"]), (same_owner["id"], 1))
        self.assertEqual((native_two.submits, native_two.fetches), (2, 2))
        self.assertEqual(len(native_one.submissions), 1)
        self.assertEqual(self.control.get("one")["current_job_id"], active["id"])

    def test_unbound_original_attempt_does_not_fall_back_to_general_replica(self):
        job = self.make_job()
        self.ready("one")
        self.runner(job, MemoryBackend(lose_response=True), worker="one").run_once("one", "hatchet-test")
        with self.repo.transaction() as connection:
            connection.execute(update(registered_workers).where(registered_workers.c.id == "one")
                .values(current_job_id=None))
        client = Mock()
        with patch.dict("sys.modules", {"hatchet_sdk": self.sdk_module()}):
            with self.assertRaisesRegex(ValueError, "hatchet_original_worker_unconfirmed"):
                SDKPublisher(client, self.repo).publish(self.message(job), job)
        client.stubs.task.assert_not_called()
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)

    def test_proven_unsubmitted_preparation_deferral_retains_safe_replica_retry(self):
        job = self.make_job()
        self.ready("one")
        class NotPrepared(MemoryBackend):
            def prepare(self, *_args):
                raise ValueError("temporary native preparation failure")
        before = NotPrepared()
        self.assertEqual(self.runner(job, before, worker="one").run_once("one", "hatchet-test")["state"], "queued")
        self.assertEqual(before.submissions, [])
        self.assertIsNone(self.control.get("one")["current_job_id"])
        self.now += 31
        self.assertNotIn("sixnine_worker", self.publish(job))
        self.ready("two")
        backend = CountingMock(self.directory / "native-two", enabled=True)
        self.assertEqual(self.callback(job, backend, "two"), {"job_id": job["id"], "state": "succeeded"})
        self.assertEqual(backend.submits, 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 2)

    def test_queued_status_cannot_remove_existing_submission_affinity(self):
        job = self.make_job()
        self.ready("one")
        self.ready("two")
        original = MemoryBackend(lose_response=True)
        self.runner(job, original, worker="one").run_once("one", "hatchet-test")
        with self.repo.transaction() as connection:
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="queued"))
        self.assertEqual(self.publish(job)["sixnine_worker"], "one")
        self.assertEqual(self.callback(job, MemoryBackend(), "two")["reason_code"], "hatchet_worker_incompatible")
        self.assertEqual(len(original.submissions), 1)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 1)

    def test_attempt_and_lease_disagreement_refuses_publication(self):
        job = self.make_job()
        self.ready("one")
        self.ready("two")
        self.runner(job, MemoryBackend(lose_response=True), worker="one").run_once("one", "hatchet-test")
        with self.repo.transaction() as connection:
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(lease_worker_id="two"))
        with self.assertRaisesRegex(ValueError, "hatchet_original_worker_unconfirmed"):
            self.publish(job)


if __name__ == "__main__":
    unittest.main()
