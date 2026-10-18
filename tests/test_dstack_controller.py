"""Original-ledger lifecycle and admission; no provider, media or GPU calls."""

from dataclasses import replace
from unittest.mock import patch
from types import SimpleNamespace
from sqlalchemy import select, update
from studio_platform.control import WorkerSpec
from studio_platform.dstack_controller import DstackController
from studio_platform.dstack_controller import SlotProcesses
from studio_platform.fleet import SlotConfig
from studio_platform.operator_capacity import operator_nodes
from studio_platform.repository import (
    attempts,
    budget_reservations,
    instance_intents,
    jobs,
    registered_workers,
)
from studio_platform.queue import TaskQueue
from studio_platform.operator_capacity import operator_commands
from studio_platform.worker_admission import worker_window_reason
from test_platform_repository import LedgerCase
import test_dstack_operator as fixture
import unittest
import tempfile
from pathlib import Path
import subprocess


class SlotProcessesTests(unittest.TestCase):
    def test_only_one_supervisor_owns_workers_and_release_allows_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "new-work-dir"
            broker = Path(directory) / "broker.json"
            first = SlotProcesses(work).__enter__()
            try:
                with self.assertRaisesRegex(ValueError, "already_active"):
                    SlotProcesses(work).__enter__()
            finally:
                first.close()
            with SlotProcesses(work):
                pass

    def test_supervisor_reaps_its_cpu_children_without_provider_calls(self):
        class Child:
            def __init__(self):
                self.alive = True
                self.signals = []

            def poll(self):
                return None if self.alive else 0

            def terminate(self):
                self.signals.append("terminate")

            def kill(self):
                self.signals.append("kill")
                self.alive = False

            def wait(self, timeout=None):
                if self.alive:
                    raise subprocess.TimeoutExpired("owned-cpu-child", timeout)
                self.signals.append("reap")
                return 0

        with tempfile.TemporaryDirectory() as directory:
            child = Child()
            with SlotProcesses(Path(directory)) as processes:
                processes.children["owned-slot"] = child
            self.assertEqual(child.signals, ["terminate", "kill", "reap"])
            self.assertEqual(processes.children, {})
            self.assertIsNone(processes._supervisor_lock)

    def test_worker_spawn_requires_supervisor_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            processes = SlotProcesses(Path(directory))
            with self.assertRaisesRegex(ValueError, "lock_required"):
                processes.ensure("slot", None)


class DstackControllerTests(fixture.DstackOperatorTests):
    preview = fixture.DstackOperatorTests.preview
    start = fixture.DstackOperatorTests.start
    running = fixture.DstackOperatorTests.running

    def controller(self):
        outer = self

        class Runtime:
            def close(self, intent_id=None):
                outer.closed_intent = intent_id

            def readiness(self, binding, run):
                from studio_platform.inference.wangp_contract import HostReadiness

                # Transport must have committed exact identity before native boot.
                outer.assertEqual(
                    outer.store.load(binding["intent_id"])["provider_instance_id"],
                    "1234567",
                )
                return HostReadiness(
                    binding["manifest_digest"], binding["intent_id"], "c" * 32, True
                )

            def slot(self, intent_id):
                binding = outer.store.load(intent_id)
                profile = outer.service.profiles["5090-fl"]
                spec = WorkerSpec(
                    "dstack-test-slot",
                    "dstack-test",
                    "vast",
                    "1234567",
                    ("GPU-real-one",),
                    (profile["recipe_id"],),
                    binding["model_id"],
                    binding["configuration_id"],
                    "wangp-worker",
                    binding["manifest_digest"],
                    dispatch_backend="hatchet-v1",
                )
                return SlotConfig(
                    spec,
                    True,
                    "http://127.0.0.1:8199",
                    ("http://127.0.0.1:8199",),
                    "",
                    True,
                    runtime_config_file=str(
                        __import__('pathlib').Path(outer.temp.name) / "native.json"
                    ),
                )

        class Broker:
            def projection(self, slot):
                return {
                    "ready": True,
                    "observed_at": outer.now,
                    "heartbeat_at": outer.now,
                    "worker_id": slot.spec.worker_id,
                    "reason_code": None,
                }

        return DstackController(self.service, Runtime())

    def ready(self):
        node = self.start()["node_id"]
        self.running(node)
        controller = self.controller()
        self.assertEqual(controller.tick()["nodes"][0]["state"], "ready")
        self.consumer(node, controller)
        return node, controller

    def consumer(self, node, controller):
        from studio_platform.postgres_dispatch import record_consumer

        record_consumer(
            self.repo,
            controller.control,
            SimpleNamespace(
                kind="wangp-worker", slot_key=node, expected_incarnation="c" * 32
            ),
            controller.runtime.slot(node).spec.worker_id,
        )

    def test_exact_ready_registers_hatchet_slot_without_second_apply(self):
        node, controller = self.ready()
        controller.tick()
        self.assertEqual(len(self.client.applied), 1)
        with self.repo.engine.connect() as connection:
            worker = dict(
                connection.execute(select(registered_workers)).mappings().one()
            )
            self.assertEqual(worker["spec"]["dispatch_backend"], "hatchet-v1")
            self.assertIsNone(
                worker_window_reason(
                    connection, worker, self.now, deployment_profile_id=fixture.PROFILE
                )
            )

    def test_consumer_fence_instance_and_incarnation_fail_closed(self):
        node, controller = self.ready()
        worker = controller.control.get(controller.runtime.slot(node).spec.worker_id)
        for field, value in (
            ("worker_fence", worker["fence"] + 1),
            ("instance_id", "different"),
            ("runtime_incarnation", "d" * 32),
        ):
            with self.subTest(field=field), self.repo.transaction() as connection:
                original = (
                    connection.execute(
                        select(operator_nodes).where(operator_nodes.c.intent_id == node)
                    )
                    .mappings()
                    .one()
                )
                import copy

                payload = copy.deepcopy(original["payload"])
                payload["dstack"]["consumer_observation"][field] = value
                connection.execute(
                    update(operator_nodes)
                    .where(operator_nodes.c.intent_id == node)
                    .values(payload=payload)
                )
                self.assertEqual(
                    worker_window_reason(
                        connection,
                        worker,
                        self.now,
                        deployment_profile_id=fixture.PROFILE,
                    ),
                    "managed_queue_consumer_unconfirmed",
                )
            self.consumer(node, controller)
        from studio_platform.postgres_dispatch import record_consumer

        with self.assertRaisesRegex(ValueError, "binding_changed"):
            record_consumer(
                self.repo,
                controller.control,
                SimpleNamespace(
                    kind="wangp-worker", slot_key=node, expected_incarnation="d" * 32
                ),
                worker["id"],
            )

    def test_stale_proof_and_wrong_dispatch_cannot_admit(self):
        node, _ = self.ready()
        with self.repo.transaction() as connection:
            worker = dict(
                connection.execute(select(registered_workers)).mappings().one()
            )
            worker["spec"] = {**worker["spec"], "dispatch_backend": "legacy"}
            self.assertEqual(
                worker_window_reason(
                    connection, worker, self.now, deployment_profile_id=fixture.PROFILE
                ),
                "managed_dstack_binding_mismatch",
            )
            worker["spec"]["dispatch_backend"] = "hatchet-v1"
            self.assertEqual(
                worker_window_reason(
                    connection,
                    worker,
                    self.now + 61,
                    deployment_profile_id=fixture.PROFILE,
                ),
                "managed_queue_consumer_unconfirmed",
            )

    def test_native_ready_without_broker_consumer_cannot_admit(self):
        node = self.start()["node_id"]
        self.running(node)
        controller = self.controller()
        controller.tick()
        with self.repo.engine.connect() as connection:
            worker = dict(
                connection.execute(select(registered_workers)).mappings().one()
            )
            self.assertEqual(
                worker_window_reason(
                    connection, worker, self.now, deployment_profile_id=fixture.PROFILE
                ),
                "managed_queue_consumer_unconfirmed",
            )
        controller.tick()
        state = self.service.state(self.owner)
        self.assertFalse(state["nodes"][0]["ready"])
        self.assertTrue(state["nodes"][0]["native_ready"])

    def test_busy_native_session_refreshes_consumer_without_marking_slot_idle(self):
        node, controller = self.ready()
        self.now += 45
        from studio_platform.inference.wangp_contract import HostReadiness

        controller.runtime.readiness = lambda binding, run: HostReadiness(
            binding["manifest_digest"], binding["intent_id"], "c" * 32, False
        )
        self.capacity.readiness = controller.runtime.readiness
        self.assertEqual(controller.tick()["nodes"][0]["state"], "busy")
        # Parent observations cannot fabricate consumer liveness.
        self.assertEqual(
            self.store.load(node)["consumer_observation"]["observed_at"], self.now - 45
        )
        self.consumer(node, controller)
        self.assertEqual(
            self.store.load(node)["consumer_observation"]["observed_at"], self.now
        )
        with self.repo.engine.connect() as connection:
            worker = dict(
                connection.execute(select(registered_workers)).mappings().one()
            )
            self.assertIsNone(
                worker_window_reason(
                    connection, worker, self.now, deployment_profile_id=fixture.PROFILE
                )
            )
        self.assertEqual(len(self.client.applied), 1)

    def test_legacy_controller_does_not_rewrite_dstack_commands(self):
        from studio_platform.operator_controller import OperatorController

        node = self.start()["node_id"]
        self.service.node_command(self.owner, node, {}, "stop-original")
        with self.repo.engine.connect() as connection:
            prior = [
                dict(row)
                for row in connection.execute(select(operator_commands)).mappings()
            ]
        legacy = object.__new__(OperatorController)
        legacy.repo = self.repo
        legacy.shutting_down = False
        legacy._record_command = lambda *args: self.fail(
            "legacy changed a dstack command"
        )
        legacy._summarize_commands()
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                [
                    dict(row)
                    for row in connection.execute(select(operator_commands)).mappings()
                ],
                prior,
            )

    def test_manual_stop_retires_idle_worker_then_stops_owned_run(self):
        node, controller = self.ready()
        budget = self.repo.get_budget("owner-budget")
        self.service.node_command(self.owner, node, {}, "stop-1")
        result = controller.tick()["nodes"][0]
        self.assertEqual(result["state"], "stopping")
        self.assertEqual(len(self.client.stopped), 1)
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                connection.execute(select(registered_workers.c.state)).scalar_one(),
                "retired",
            )

    def _expired_queue_history(self):
        node, controller = self.ready()
        job = self.job()
        queue = TaskQueue(self.repo)
        claim = queue.claim("dstack-test-slot", job["pool"], lease_seconds=10)
        self.assertIsNotNone(claim)
        with self.repo.transaction() as connection:
            connection.execute(
                update(registered_workers)
                .where(registered_workers.c.id == "dstack-test-slot")
                .values(state="leased", current_job_id=job["id"])
            )
        self.now += 11
        self.assertEqual(queue.recover_expired()[0]["id"], job["id"])
        with self.repo.engine.connect() as connection:
            original_job = dict(
                connection.execute(select(jobs).where(jobs.c.id == job["id"]))
                .mappings()
                .one()
            )
            original_attempt = dict(
                connection.execute(
                    select(attempts).where(
                        attempts.c.id == original_job["current_attempt_id"]
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(original_job["status"], "queued")
        self.assertIsNone(original_job["lease_worker_id"])
        self.assertIsNone(original_job["lease_expires_at"])
        self.assertEqual(original_attempt["status"], "lease_expired")
        self.assertIsNone(original_attempt["submission_started_at"])
        self.assertIsNone(original_attempt["upstream_task_id"])
        return node, controller, original_job, original_attempt

    def test_expired_unsubmitted_queue_history_drains_without_changing_original_job(
        self,
    ):
        node, controller, job, attempt = self._expired_queue_history()
        budget = self.repo.get_budget("owner-budget")
        with self.repo.engine.connect() as connection:
            reservations = [
                dict(row)
                for row in connection.execute(select(budget_reservations)).mappings()
            ]
        self.service.node_command(self.owner, node, {}, "stop-expired-history")
        self.assertEqual(controller.tick()["nodes"][0]["state"], "stopping")
        self.assertEqual(len(self.client.stopped), 1)
        self.assertEqual(len(self.client.applied), 1)
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        with self.repo.engine.connect() as connection:
            self.assertEqual(
                dict(
                    connection.execute(select(jobs).where(jobs.c.id == job["id"]))
                    .mappings()
                    .one()
                ),
                job,
            )
            self.assertEqual(
                dict(
                    connection.execute(
                        select(attempts).where(attempts.c.id == attempt["id"])
                    )
                    .mappings()
                    .one()
                ),
                attempt,
            )
            self.assertEqual(
                [
                    dict(row)
                    for row in connection.execute(
                        select(budget_reservations)
                    ).mappings()
                ],
                reservations,
            )
            worker = connection.execute(select(registered_workers)).mappings().one()
            self.assertEqual(worker["state"], "retired")
            self.assertIsNone(worker["current_job_id"])

    def test_expired_queue_is_still_an_obligation_on_a_running_host(self):
        node, controller, job, attempt = self._expired_queue_history()
        with self.repo.engine.connect() as connection:
            intent = (
                connection.execute(
                    select(instance_intents).where(instance_intents.c.id == node)
                )
                .mappings()
                .one()
            )
        self.assertEqual(controller._obligations(intent)[1:3], (1, 0))
        self.service.node_command(self.owner, node, {}, "stop-expired-history")
        self.assertEqual(controller._obligations(intent)[1:3], (0, 0))
        with self.repo.transaction() as connection:
            connection.execute(
                update(attempts)
                .where(attempts.c.id == attempt["id"])
                .values(upstream_task_id="unknown-task")
            )
        self.assertEqual(controller._obligations(intent)[1:3], (1, 1))
        self.assertEqual(self.client.stopped, [])

    def test_final_stop_rechecks_expired_queue_and_rejects_live_or_uncertain_history(
        self,
    ):
        node, controller, job, attempt = self._expired_queue_history()
        self.service.node_command(self.owner, node, {}, "stop-expired-history")
        controller.control.drain("dstack-test-slot")
        controller.control.retire("dstack-test-slot", upstream_idle_confirmed=True)
        variants = (
            (jobs, job, {"lease_worker_id": "dstack-test-slot"}),
            (jobs, job, {"lease_expires_at": self.now + 60}),
            (jobs, job, {"status": "claimed"}),
            (jobs, job, {"status": "collecting"}),
            (jobs, job, {"status": "submission_unknown"}),
            (jobs, job, {"current_attempt_id": None}),
            (jobs, job, {"current_attempt_id": "missing-attempt"}),
            (attempts, attempt, {"status": "claimed"}),
            (attempts, attempt, {"submission_started_at": self.now}),
            (attempts, attempt, {"upstream_task_id": "unknown-task"}),
        )
        for table, original, changed in variants:
            with self.subTest(table=table.name, changed=changed):
                with self.repo.transaction() as connection:
                    connection.execute(
                        update(table)
                        .where(table.c.id == original["id"])
                        .values(**changed)
                    )
                binding = self.store.load(node)
                self.assertFalse(self.store.begin_stop(node, binding["run_id"]))
                self.assertFalse(self.store.load(node).get("stop_started", False))
                with self.repo.transaction() as connection:
                    connection.execute(
                        update(table)
                        .where(table.c.id == original["id"])
                        .values(**{key: original[key] for key in changed})
                    )
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(status="failed")
            )
            connection.execute(
                update(attempts)
                .where(attempts.c.id == attempt["id"])
                .values(upstream_task_id="unknown-task")
            )
        self.assertFalse(self.store.begin_stop(node, self.store.load(node)["run_id"]))
        with self.repo.transaction() as connection:
            connection.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(status=job["status"])
            )
            connection.execute(
                update(attempts)
                .where(attempts.c.id == attempt["id"])
                .values(upstream_task_id=None)
            )
        self.assertTrue(self.store.begin_stop(node, self.store.load(node)["run_id"]))
        self.assertEqual(self.client.stopped, [])

    def test_cold_preparation_does_not_consume_idle_window(self):
        node = self.start()["node_id"]
        self.now += 650
        controller = self.controller()
        self.assertEqual(controller.tick()["nodes"][0]["state"], "starting")
        self.assertEqual(self.client.stopped, [])
        self.running(node)
        self.assertEqual(controller.tick()["nodes"][0]["state"], "ready")
        self.assertEqual(self.client.stopped, [])

    def test_manual_stop_cancels_cold_download_without_waiting_for_ready(self):
        node = self.start()["node_id"]
        self.capacity.readiness = None
        self.running(node)
        binding = self.store.load(node)
        self.assertTrue(self.store.begin_bootstrap(node, binding["run_id"], "1234567"))
        self.assertTrue(
            self.store.begin_runtime_launch(node, binding["run_id"], "1234567")
        )
        budget = self.repo.get_budget("owner-budget")
        self.service.node_command(self.owner, node, {}, "cold-stop-original")
        controller = self.controller()
        controller.runtime.readiness = lambda *args: None
        self.assertEqual(controller.tick()["nodes"][0]["state"], "stopping")
        self.assertEqual(len(self.client.stopped), 1)
        self.assertEqual(len(self.client.applied), 1)
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        self.assertTrue(self.store.load(node)["stop_started"])
        with self.repo.engine.connect() as connection:
            self.assertEqual(list(connection.execute(select(registered_workers))), [])

    def test_cold_stop_requires_fresh_exact_identity_and_no_first_ready_history(self):
        node = self.start()["node_id"]
        self.capacity.readiness = None
        self.running(node)
        binding = self.store.load(node)
        self.store.begin_bootstrap(node, binding["run_id"], "1234567")
        self.store.begin_runtime_launch(node, binding["run_id"], "1234567")
        self.service.node_command(self.owner, node, {}, "cold-stop-original")
        with self.repo.engine.connect() as connection:
            original = dict(
                connection.execute(
                    select(operator_nodes).where(operator_nodes.c.intent_id == node)
                )
                .mappings()
                .one()
            )["payload"]
        import copy

        for change in (
            {"observed_at": self.now - 31},
            {"provider_instance_id": "another-instance"},
            {"first_runtime_ready_at": self.now - 1},
            {"runtime_incarnation": "c" * 32},
        ):
            with self.subTest(change=change):
                payload = copy.deepcopy(original)
                payload["dstack"].update(change)
                with self.repo.transaction() as connection:
                    connection.execute(
                        update(operator_nodes)
                        .where(operator_nodes.c.intent_id == node)
                        .values(payload=payload)
                    )
                self.assertFalse(self.store.begin_stop(node, binding["run_id"]))
                self.assertFalse(self.store.load(node).get("stop_started"))
        self.assertEqual(self.client.stopped, [])

    def test_hold_preserves_original_deadline_and_expired_idle_stops(self):
        node, controller = self.ready()
        deadline = self.repo.list_instance_intents()[0]["hard_deadline"]
        self.service.set_hold(self.owner, node, {"hold_seconds": 700}, "hold-1")
        self.now += 610
        controller.tick()
        self.assertEqual(self.client.stopped, [])
        self.assertEqual(
            self.repo.list_instance_intents()[0]["hard_deadline"], deadline
        )
        self.now += 100
        controller.tick()
        self.assertEqual(len(self.client.stopped), 1)

    def test_controller_will_not_replay_an_unjournaled_apply(self):
        node = self.start()["node_id"]
        with self.repo.transaction() as connection:
            row = connection.execute(select(operator_nodes)).mappings().one()
            payload = {
                **row["payload"],
                "dstack": {**row["payload"]["dstack"], "apply_started": False},
            }
            connection.execute(update(operator_nodes).values(payload=payload))
        self.assertEqual(
            self.controller().tick()["nodes"][0]["state"], "reconciliation_required"
        )
        self.assertEqual(len(self.client.applied), 1)

    def test_crash_before_apply_recovers_original_request_once(self):
        from studio_platform.dstack_capacity import DstackError

        preview = self.preview()
        with patch.object(
            self.capacity, "start", side_effect=DstackError("fake_preapply_crash")
        ):
            original = self.start(preview=preview)
        self.assertEqual(len(self.client.applied), 0)
        counts = self.counts()
        self.controller().tick()
        self.controller().tick()
        self.assertEqual(len(self.client.applied), 1)
        self.assertEqual(self.counts(), counts)
        again = self.start(preview=preview)
        self.assertEqual(again["node_id"], original["node_id"])
        self.assertEqual(len(self.client.applied), 1)

    def test_expired_never_applied_intent_releases_only_its_original_reservation(self):
        from studio_platform.dstack_capacity import DstackError

        with patch.object(
            self.capacity, "start", side_effect=DstackError("fake_preapply_crash")
        ):
            node = self.start()["node_id"]
        self.now += 1000
        self.assertEqual(self.controller().tick()["nodes"][0]["state"], "never_applied")
        self.assertEqual(len(self.client.applied), 0)
        self.assertEqual(self.store.load(node)["billing_state"], "no_charge_confirmed")

    def test_terminal_owned_run_cleans_only_local_resources(self):
        node, controller = self.ready()
        self.service.node_command(self.owner, node, {}, "stop-1")
        controller.tick()
        self.client.runs[self.store.load(node)["run_name"]]["status"] = "terminated"
        controller.tick()
        controller.tick()
        self.assertEqual(self.closed_intent, node)
        self.assertEqual(self.store.load(node)["billing_state"], "unsettled")

    def test_one_terminal_cleanup_failure_does_not_block_another_node(self):
        node, controller = self.ready()
        with self.repo.transaction() as connection:
            connection.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node)
                .values(runtime_state="stopped")
            )
        controller.runtime.close = lambda _: (_ for _ in ()).throw(
            TimeoutError("local cleanup")
        )
        nodes = controller._nodes() + [
            {"intent_id": "later-node", "runtime_state": "starting"}
        ]
        with (
            patch.object(controller, "_nodes", return_value=nodes),
            patch.object(
                controller,
                "_tick_node",
                return_value={"node_id": "later-node", "state": "starting"},
            ) as reconcile,
        ):
            result = controller.tick()["nodes"]
        self.assertEqual(result[0]["code"], "dstack_controller_observation_failed")
        self.assertEqual(result[1]["node_id"], "later-node")
        reconcile.assert_called_once_with(nodes[1])
