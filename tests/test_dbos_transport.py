"""Transport wakeups cannot authorize another job, submission or budget."""

from pathlib import Path
from contextlib import contextmanager
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from studio_platform.dbos_transport import DurableSlots, _Slot, workflow_id
from studio_platform.control import WorkerControl
from studio_platform.fleet import SlotConfig
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

    @contextmanager
    def ensured_slots(self, spec, backend):
        @contextmanager
        def context(*_, **__):
            yield self.runner(backend)

        with DurableSlots(
            self.repo, Mock(), self.directory / "executor", runner_context=context
        ) as slots:
            slots.ensure("original-node", SlotConfig(spec, enabled=True))
            yield slots

    def test_fresh_native_ensure_keeps_idle_registration_live_past_first_lease(self):
        spec = self.ready()
        before = self.control.get(spec.worker_id)
        with self.ensured_slots(spec, MemoryBackend()) as slots:
            for _ in range(4):
                self.now += 45
                slots.ensure("original-node", SlotConfig(spec, enabled=True))
                live = self.control.get(spec.worker_id)
                self.assertEqual(live["state"], "ready")
                self.assertEqual(live["fence"], before["fence"])
                self.assertIsNone(live["current_job_id"])
                self.assertEqual(live["expires_at"], self.now + 120)
        self.assertGreater(self.now, before["expires_at"])

    def test_ensure_alone_cannot_revive_expired_or_unknown_registration(self):
        spec = self.ready()
        with self.ensured_slots(spec, MemoryBackend()) as slots:
            self.now += 121
            expired = self.control.get(spec.worker_id)
            slots.ensure("original-node", SlotConfig(spec, enabled=True))
            self.assertEqual(self.control.get(spec.worker_id), expired)
            self.control.recover_expired()
            unknown = self.control.get(spec.worker_id)
            self.assertEqual(unknown["state"], "unknown")
            slots.ensure("original-node", SlotConfig(spec, enabled=True))
            self.assertEqual(self.control.get(spec.worker_id), unknown)

    def test_ensure_heartbeat_never_resets_running_job_or_drain(self):
        job = self.make_job()
        spec = self.ready()
        with self.ensured_slots(spec, MemoryBackend()) as slots:
            slots.turn(
                self.scope.tenant_id,
                self.scope.owner_id,
                self.scope.project_id,
                job["id"],
            )
            for draining in (False, True):
                if draining:
                    self.control.drain(spec.worker_id)
                before = self.control.get(spec.worker_id)
                self.assertEqual(before["current_job_id"], job["id"])
                self.now += 10
                slots.ensure("original-node", SlotConfig(spec, enabled=True))
                after = self.control.get(spec.worker_id)
                for field in ("state", "current_job_id", "fence", "drain_requested"):
                    self.assertEqual(after[field], before[field])

    def test_local_render_capabilities_do_not_keep_provider_polling_active(self):
        from studio_platform.settings import Settings

        settings = Settings(self.directory, auth_mode="local-test", render_enabled=True)
        with DurableSlots(self.repo, settings, self.directory / "executor") as slots:
            slots.add_render_slots()
            self.assertEqual(
                set(slots._slots),
                {
                    "dbos-cpu-render-v1",
                    "dbos-cpu-render-v2",
                    "dbos-cpu-render-v3",
                    "dbos-cpu-render-v5",
                },
            )
            self.assertEqual(slots.children, {})
            for slot in slots._slots.values():
                self.assertEqual(slot.spec.backend, "cpu-render")
                worker = self.control.get(slot.spec.worker_id)
                self.assertEqual(worker["state"], "ready")
                self.assertEqual(worker["expires_at"], self.now + 3600)

    def test_enhance_slot_registers_ready_without_provider_polling(self):
        from studio_platform.settings import Settings

        settings = Settings(
            self.directory, auth_mode="local-test", enhance_enabled=True
        )
        with DurableSlots(self.repo, settings, self.directory / "executor") as slots:
            slots.add_render_slots()
            slots.add_enhance_slot()
            self.assertEqual(set(slots._slots), {"dbos-runpod-enhance-v1"})
            self.assertEqual(slots.children, {})
            worker = self.control.get("dbos-runpod-enhance-v1")
            self.assertEqual(worker["state"], "ready")
            self.assertEqual(worker["expires_at"], self.now + 3600)

    def test_enhance_slot_failure_leaves_the_executor_running(self):
        from studio_platform.settings import Settings

        settings = Settings(
            self.directory, auth_mode="local-test", enhance_enabled=True
        )
        with DurableSlots(self.repo, settings, self.directory / "executor") as slots:
            with patch.object(slots, "ensure", side_effect=ValueError("boom")):
                slots.add_enhance_slot()
            self.assertEqual(slots._slots, {})
            slots.add_render_slots()

    def test_startup_v5_claim_is_exact_and_all_versions_share_the_cpu_fence(self):
        from studio_platform.render_plans import CONFIGURATIONS, MODEL, POOL, RECIPE
        from studio_platform.settings import Settings

        settings = Settings(self.directory, auth_mode="local-test", render_enabled=True)
        with DurableSlots(self.repo, settings, self.directory / "executor") as slots:
            slots.add_render_slots()
            jobs = {}
            for version in (1, 2, 3, 4, 5):
                plan = self.repo.create_plan(
                    self.scope,
                    {
                        "recipe_id": RECIPE,
                        "request": {
                            "model": MODEL,
                            "render": {
                                "version": version,
                                **({"stage": "chapter"} if version == 5 else {}),
                            },
                        },
                    },
                    {
                        "pool": POOL,
                        "backend": "cpu-render",
                        "enabled": True,
                        "configuration_id": CONFIGURATIONS[version],
                    },
                    expires_at=self.now + 1000,
                    estimated_cost_microusd=0,
                )
                jobs[version] = self.repo.create_job(
                    self.scope, plan["id"], "render-version-" + str(version)
                )
            # Holding the shared operation mutex prevents even the new slot
            # from leasing; distinct configuration rows add no CPU capacity.
            slots._cpu_operation.acquire()
            try:
                slots.turn(
                    self.scope.tenant_id,
                    self.scope.owner_id,
                    self.scope.project_id,
                    jobs[5]["id"],
                )
            finally:
                slots._cpu_operation.release()
            self.assertEqual(
                self.repo.get_job(self.scope, jobs[5]["id"])["attempt_no"], 0
            )

            @contextmanager
            def occupied_fence():
                yield False, None

            with patch.object(slots, "_cpu_fence", side_effect=occupied_fence) as fence:
                slots.turn(
                    self.scope.tenant_id,
                    self.scope.owner_id,
                    self.scope.project_id,
                    jobs[5]["id"],
                )
                self.assertEqual(fence.call_count, 4)
            self.assertEqual(
                self.repo.get_job(self.scope, jobs[5]["id"])["attempt_no"], 0
            )
            v5 = self.control.get("dbos-cpu-render-v5")
            for version, job in jobs.items():
                self.assertEqual(self.control.matches(v5, job), version == 5)
            claimed = self.control.claim("dbos-cpu-render-v5", POOL)
            self.assertEqual(claimed.job["id"], jobs[5]["id"])
            for version in (1, 2, 3, 4):
                self.assertEqual(
                    self.repo.get_job(self.scope, jobs[version]["id"])["attempt_no"], 0
                )
            self.assertTrue(
                self.control.matches(self.control.get("dbos-cpu-render-v3"), jobs[4])
            )
            self.assertEqual(
                self.control.capacity(), {"instances": 0, "physical_gpus": 0}
            )

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
