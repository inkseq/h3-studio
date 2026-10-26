"""Demand wake races and original-row obligations; no supplier or GPU calls."""

import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import uuid

from sqlalchemy import insert, select, update

from studio_platform.demand_wakeup import (
    DemandWakeup,
    IDLE_SAFETY_SECONDS,
    notify_demand,
    run_demand_loop,
)
from studio_platform.dstack_controller import DstackController
from studio_platform.operator_capacity import operator_commands, operator_nodes
from studio_platform.repository import attempts, budget_accounts, jobs, outbox
from test_platform_repository import LedgerCase


class DemandLoopTests(unittest.TestCase):
    def test_maintenance_wait_is_interrupted_by_the_original_commit_hint(self):
        stop, notified = threading.Event(), []
        work = Mock()
        waits = []

        class Wake:
            def subscribe(self):
                pass

            def wait(self, seconds):
                waits.append(seconds)
                if len(waits) == 1:
                    notified.append(True)
                    return "notified"
                stop.set()

        run_demand_loop(
            stop, Wake(), lambda: True, work,
            active_seconds=lambda: 15 if notified else 60,
        )
        self.assertEqual(waits, [60, 15])
        self.assertEqual(work.call_count, 2)

    def test_idle_blocks_until_commit_hint_then_runs_original_demand(self):
        stop = threading.Event()
        state = {"active": False, "clock": 0, "sweeps": 0, "work": 0}
        order, waits = [], []

        class Wake:
            def subscribe(self):
                order.append("subscribed")

            def wait(self, seconds):
                waits.append(seconds)
                state["clock"] += seconds
                if len(waits) == 1:
                    state["active"] = True  # A committed start arrives while idle.
                else:
                    stop.set()

        def demand():
            order.append("scan")
            self.assertEqual(order[0], "subscribed")
            return state["active"]

        def work():
            state["work"] += 1

        def sweep():
            state["sweeps"] += 1

        run_demand_loop(
            stop,
            Wake(),
            demand,
            work,
            active_seconds=15,
            safety=sweep,
            clock=lambda: state["clock"],
        )
        self.assertEqual(waits, [IDLE_SAFETY_SECONDS, 15])
        self.assertEqual((state["sweeps"], state["work"]), (1, 1))

    def test_idle_safety_is_rare_and_dispatcher_has_no_separate_provider_loop(self):
        for safety in (False, True):
            stop = threading.Event()
            clock, waits, sweeps = [0], [], []

            class Wake:
                def subscribe(self):
                    pass

                def wait(self, seconds):
                    waits.append(seconds)
                    clock[0] += seconds
                    if len(waits) == 2:
                        stop.set()

            run_demand_loop(
                stop,
                Wake(),
                lambda: False,
                lambda: self.fail("idle must not execute its work loop"),
                active_seconds=2,
                safety=(lambda: sweeps.append(clock[0])) if safety else None,
                clock=lambda: clock[0],
            )
            self.assertEqual(waits, [900, 900])
            self.assertEqual(sweeps, [0, 900] if safety else [])

    def test_reconnect_and_scan_wait_race_recheck_durable_work(self):
        stop, active = threading.Event(), [False]
        scans, work = [], []

        class Wake:
            def subscribe(self):
                pass

            def wait(self, seconds):
                # The waiter reconnects/subscribes; committed rows survive a
                # missing notification and are found by the following scan.
                active[0] = True
                return "reconnected"

        def run():
            work.append("same original intent")
            stop.set()

        def demand():
            scans.append(active[0])
            return active[0]

        run_demand_loop(stop, Wake(), demand, run, active_seconds=15)
        self.assertEqual(work, ["same original intent"])
        self.assertEqual(scans, [False, False, True])

    def test_notify_is_fixed_and_once_per_transaction_not_payload_authority(self):
        connection = SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql"), info={}, execute=Mock()
        )
        notify_demand(connection)
        notify_demand(connection)
        connection.execute.assert_called_once()
        self.assertEqual(
            connection.execute.call_args.args[1],
            {"channel": "sixnine_business_demand_v1", "payload": "changed"},
        )


class DemandLedgerTests(LedgerCase):
    def controller(self):
        service = SimpleNamespace(
            repo=self.repo,
            settings=SimpleNamespace(tenant_id=self.scope.tenant_id),
            capacity=SimpleNamespace(store=SimpleNamespace()),
            registration_capacity_usage=Mock(),
        )
        return DstackController(service, Mock())

    def modern_job(self):
        job = self.job()
        with self.repo.transaction() as c:
            c.execute(
                update(jobs)
                .where(jobs.c.id == job["id"])
                .values(
                    execution_plan={
                        **job["execution_plan"],
                        "dispatch_backend": "hatchet-v1",
                    }
                )
            )
        return job

    def test_queued_future_cancel_collect_unknown_and_terminal_unsafe_keep_awake(self):
        controller = self.controller()
        self.assertFalse(controller.has_demand())
        job = self.modern_job()
        for status in (
            "waiting_capacity",
            "queued",
            "claimed",
            "submitting",
            "running",
            "collecting",
            "submission_unknown",
            "cancel_requested",
            "recovery_hold",
        ):
            with self.repo.transaction() as c:
                c.execute(
                    update(jobs)
                    .where(jobs.c.id == job["id"])
                    .values(status=status, not_before=self.now + 3600)
                )
            self.assertTrue(controller.has_demand(), status)
        with self.repo.transaction() as c:
            c.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(status="failed")
            )
        self.assertFalse(controller.has_demand())
        with self.repo.transaction() as c:
            c.execute(
                insert(attempts).values(
                    id=str(uuid.uuid4()),
                    job_id=job["id"],
                    worker_id="original",
                    fence=1,
                    number=1,
                    status="failed",
                    created_at=self.now - 5,
                    updated_at=self.now,
                    submission_started_at=self.now - 4,
                    upstream_task_id="original-task",
                    upstream_stopped=0,
                )
            )
        self.assertTrue(controller.has_demand())
        with self.repo.transaction() as c:
            c.execute(
                update(attempts)
                .where(attempts.c.job_id == job["id"])
                .values(upstream_stopped=1)
            )
        self.assertFalse(controller.has_demand())

    def test_historical_stopped_rows_do_not_poll_but_live_node_and_cleanup_do(self):
        controller = self.controller()
        self.repo.configure_pool("test-pool", max_instances=10, max_physical_gpus=10)
        intent = self.repo.reserve_instance_intent(
            self.scope,
            "test-pool",
            "original",
            physical_gpus=1,
            slots=1,
            reserved_cost_microusd=1,
            hard_deadline=self.now + 3600,
            budget_account_ids=("owner-budget",),
            provider="vast",
            dry_run=False,
        )
        node_id, command_id = intent["id"], str(uuid.uuid4())
        with self.repo.transaction() as c:
            c.execute(
                insert(operator_commands).values(
                    id=command_id,
                    actor="original",
                    idempotency_key="original",
                    request_hash="a" * 64,
                    kind="dstack_start",
                    state="accepted",
                    payload={},
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            c.execute(
                insert(operator_nodes).values(
                    intent_id=node_id,
                    command_id=command_id,
                    ordinal=0,
                    binding_id="original",
                    binding_hash="a" * 64,
                    desired_state="stopped",
                    runtime_state="stopped",
                    payload={
                        "tenant_id": self.scope.tenant_id,
                        "capacity_backend": "dstack-v1",
                        "dstack": {"billing_state": "unsettled"},
                    },
                    updated_at=self.now,
                )
            )
        self.assertFalse(controller.has_demand())
        with self.repo.transaction() as c:
            c.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(runtime_state="draining")
            )
        self.assertTrue(controller.has_demand())
        with self.repo.transaction() as c:
            c.execute(
                update(operator_nodes)
                .where(operator_nodes.c.intent_id == node_id)
                .values(runtime_state="stopped")
            )
        controller._cleanup_pending.add(node_id)
        self.assertTrue(controller.has_demand())
        controller.tick()
        self.assertFalse(controller.has_demand())

    def test_safety_observation_reports_untracked_allocations_without_business_changes(
        self,
    ):
        controller = self.controller()
        client = SimpleNamespace(
            project="sixnine",
            _post=Mock(return_value=[{"id": "untracked-run"}]),
            provider_inventory=Mock(
                side_effect=lambda name: {
                    "complete": True,
                    "instance_ids": ["123"] if name == "vastai" else [],
                    "observed_at": self.now,
                }
            ),
        )
        controller.capacity.client = client
        with self.repo.engine.connect() as c:
            before = [dict(x) for x in c.execute(select(budget_accounts)).mappings()]
        result = controller.safety_sweep()
        self.assertEqual(result["untracked_active_runs"], 1)
        self.assertEqual(
            result["untracked_provider_allocations"], {"vastai": 1, "runpod": 0}
        )
        self.assertEqual(result["provider_mutations"], 0)
        with self.repo.engine.connect() as c:
            self.assertEqual(
                [dict(x) for x in c.execute(select(budget_accounts)).mappings()], before
            )
        self.assertEqual(client.provider_inventory.call_count, 2)

    def test_real_postgres_commit_rollback_and_gap_notifications(self):
        if self.repo.engine.dialect.name != "postgresql":
            self.skipTest("requires explicit isolated local PostgreSQL")
        stop = threading.Event()
        wake = DemandWakeup(self.repo, stop)
        try:
            self.assertTrue(wake.subscribe())
            with self.repo.transaction() as c:
                self.repo._emit(
                    c,
                    "dstack.start_requested",
                    str(uuid.uuid4()),
                    {"PRIVATE": "not a notification"},
                )
                self.repo._emit(c, "dstack.stop_requested", str(uuid.uuid4()), {})
                self.assertEqual(wake.wait(0.02), "timeout")
            self.assertEqual(wake.wait(1), "notified")
            self.assertEqual(wake.wait(0), "timeout")
            with self.assertRaisesRegex(RuntimeError, "rollback"):
                with self.repo.transaction() as c:
                    self.repo._emit(c, "dstack.start_requested", str(uuid.uuid4()), {})
                    raise RuntimeError("rollback")
            self.assertEqual(wake.wait(0.02), "timeout")
            # Commit after the demand scan but before wait: already LISTENing.
            with self.repo.transaction() as c:
                self.repo._emit(c, "dstack.stop_requested", str(uuid.uuid4()), {})
            self.assertEqual(wake.wait(1), "notified")
            stop.set()
            wake.interrupt()
            self.assertEqual(wake.wait(900), "stopped")
        finally:
            wake.close()
