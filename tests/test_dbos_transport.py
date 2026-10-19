"""Transport wakeups cannot authorize another job, submission or budget."""

from pathlib import Path
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock

from studio_platform.dbos_transport import DurableSlots, _Slot, workflow_id
from studio_platform.control import WorkerControl
from studio_platform.storage import LocalObjectStore
from test_platform_repository import LedgerCase
import test_postgres_dispatch as dispatch_cases
from test_postgres_dispatch import MemoryBackend


class DurableDomainTests(LedgerCase):
    make_job = dispatch_cases.PostgresDispatchTests.make_job
    ready = dispatch_cases.PostgresDispatchTests.ready
    runner = dispatch_cases.PostgresDispatchTests.runner

    def setUp(self):
        super().setUp()
        self.control = WorkerControl(self.repo)
        self.directory = Path(self.temp.name)
        self.store = LocalObjectStore(self.directory / "objects")

    def slots(self, spec, backend):
        runner = self.runner(backend)
        context = Mock()
        slots = DurableSlots(self.repo, Mock(), self.directory)
        slots._slots["original-node"] = _Slot(spec, runner, context, threading.Lock())
        return slots

    def test_selected_delivery_yields_to_original_fairness_order(self):
        first = self.make_job("first")
        self.now += 1
        second = self.make_job("second")
        backend = MemoryBackend()
        slots = self.slots(self.ready(), backend)
        delayed = slots.turn(
            self.scope.tenant_id,
            self.scope.owner_id,
            self.scope.project_id,
            second["id"],
        )
        self.assertFalse(delayed["done"])
        self.assertEqual(backend.submissions, [])
        slots.turn(
            self.scope.tenant_id,
            self.scope.owner_id,
            self.scope.project_id,
            first["id"],
        )
        self.assertEqual(len(backend.submissions), 1)
        self.assertEqual(self.repo.get_job(self.scope, second["id"])["attempt_no"], 0)

    def test_lost_step_ack_reenters_same_native_tag_without_another_submit(self):
        job = self.make_job()
        backend = MemoryBackend(lose_response=True)
        slots = self.slots(self.ready(), backend)
        slots.turn(
            self.scope.tenant_id, self.scope.owner_id, self.scope.project_id, job["id"]
        )
        original = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(original["status"], "submission_unknown")
        self.now += 31
        # Equivalent to a process dying before DBOS persisted its step result.
        slots.turn(
            self.scope.tenant_id, self.scope.owner_id, self.scope.project_id, job["id"]
        )
        recovered = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(
            recovered["current_attempt_id"], original["current_attempt_id"]
        )
        self.assertEqual(recovered["attempt_no"], 1)
        self.assertEqual(backend.submissions, [original["current_attempt_id"]])
        self.assertEqual(recovered["status"], "running")

    def test_cancelled_job_finishes_transport_without_claiming_or_new_hold(self):
        job = self.make_job()
        backend = MemoryBackend()
        slots = self.slots(self.ready(), backend)
        self.repo.request_cancel(self.scope, job["id"])
        value = slots.turn(
            self.scope.tenant_id, self.scope.owner_id, self.scope.project_id, job["id"]
        )
        self.assertTrue(value["done"])
        self.assertEqual(value["state"], "cancelled")
        self.assertFalse(backend.submissions)
        self.assertEqual(self.repo.get_job(self.scope, job["id"])["attempt_no"], 0)

    def test_foreign_identity_cannot_open_or_claim_original_job(self):
        job = self.make_job()
        backend = MemoryBackend()
        slots = self.slots(self.ready(), backend)
        from studio_platform.repository import NotFound

        with self.assertRaises(NotFound):
            slots.turn(
                self.scope.tenant_id,
                self.other.owner_id,
                self.scope.project_id,
                job["id"],
            )
        self.assertFalse(backend.submissions)

    def test_locked_domain_turn_yields_without_lease_or_serializing_other_slots(self):
        job = self.make_job()
        backend = MemoryBackend()
        slots = self.slots(self.ready(), backend)
        with slots._slots["original-node"].lock:
            value = slots.turn(
                self.scope.tenant_id,
                self.scope.owner_id,
                self.scope.project_id,
                job["id"],
            )
        self.assertFalse(value["done"])
        current = self.repo.get_job(self.scope, job["id"])
        self.assertIsNone(current["lease_worker_id"])
        self.assertEqual(current["attempt_no"], 0)

    def test_producer_failure_rolls_back_original_job_and_reservation(self):
        planned = self.make_job(status="planned")
        before = self.repo.get_job(self.scope, planned["id"])
        transport = Mock()
        transport.job_event.side_effect = RuntimeError(
            "test_transport_database_unavailable"
        )
        self.repo.generation_transport = transport
        with self.assertRaises(RuntimeError):
            self.repo.enqueue(
                self.scope, planned["id"], budget_account_ids=["owner-budget"]
            )
        self.repo.generation_transport = None
        after = self.repo.get_job(self.scope, planned["id"])
        self.assertEqual(after, before)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"], 0)

    def test_workflow_identity_is_stable_and_owner_scoped(self):
        first = workflow_id("t", "superdan", "p", "job")
        self.assertEqual(first, workflow_id("t", "superdan", "p", "job"))
        self.assertNotEqual(first, workflow_id("t", "supervan", "p", "job"))
        self.assertNotIn("superdan", first)

    def test_local_render_capabilities_do_not_keep_provider_polling_active(self):
        from studio_platform.settings import Settings

        settings = Settings(self.directory, auth_mode="local-test", render_enabled=True)
        with DurableSlots(self.repo, settings, self.directory / "executor") as slots:
            slots.add_render_slots()
            self.assertEqual(len(slots._slots), 3)
            self.assertEqual(slots.children, {})
            for slot in slots._slots.values():
                self.assertEqual(slot.spec.backend, "cpu-render")
                worker = self.control.get(slot.spec.worker_id)
                self.assertEqual(worker["state"], "ready")
                self.assertEqual(worker["expires_at"], self.now + 3600)

    @unittest.skipUnless(
        os.name == "posix", "Inherited POSIX shared CPU operation fence"
    )
    def test_cpu_fence_survives_parent_close_across_configurations(self):
        slots = DurableSlots(self.repo, Mock(), self.directory)
        with slots._cpu_fence() as (acquired, descriptor):
            self.assertTrue(acquired)
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys;sys.stdin.read()"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(descriptor,),
            )
        try:
            other = DurableSlots(self.repo, Mock(), self.directory)
            with other._cpu_fence() as (acquired, descriptor):
                self.assertFalse(acquired)
                self.assertIsNone(descriptor)
        finally:
            child.communicate(timeout=5)
        with slots._cpu_fence() as (acquired, descriptor):
            self.assertTrue(acquired)


if __name__ == "__main__":
    unittest.main()
