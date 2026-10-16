"""Terminal uncertainty changes cadence, never invoices; isolated fake capacity."""
import copy
import json
import threading
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy import insert, select, update

from studio_platform.demand_wakeup import run_demand_loop
from studio_platform.dstack_capacity import terminal_no_allocation
from studio_platform.dstack_controller import DstackController, _terminal_review_candidate
from studio_platform.operator_capacity import operator_commands, operator_nodes
from studio_platform.repository import (attempts, budget_accounts, budget_reservations,
    instance_intents, jobs, registered_workers, scaler_actions)
from test_dstack_capacity import no_capacity_run
import test_dstack_operator as fixture
from test_platform_repository import LedgerCase


class PassiveReviewTests(LedgerCase):
    preview = fixture.DstackOperatorTests.preview
    start = fixture.DstackOperatorTests.start

    def setUp(self):
        super().setUp()
        fixture.DstackOperatorTests._setup_dstack(self)
        self.client.provider_inventory = Mock(side_effect=lambda backend: {
            "backend": backend, "complete": True, "instance_ids": [], "observed_at": self.now})
        self.client._post = Mock(return_value=[])
        self.client.get = Mock(wraps=self.client.get)
        self.processes = SimpleNamespace(children={}, stop=Mock(), ensure=Mock())
        self.controller = DstackController(self.service, SimpleNamespace(readiness=Mock()),
            worker_process=self.processes)

    def unknown(self):
        node_id = self.start()["node_id"]
        binding = self.store.load(node_id)
        run = no_capacity_run(self.client.runs[binding["run_name"]])
        marker = {"version": 1, "outcome": "unknown", "attempted_count": 1, "attempts": [
            {"ask_id": "12345", "http_status": 400, "code": "mutation_unknown",
             "outcome": "unknown", "contract_id": None}]}
        run["latest_job_submission"]["termination_reason_message"] = "sixnine_vast_attempts_v1:" + json.dumps(marker)
        run["jobs"][0]["job_submissions"][0] = copy.deepcopy(run["latest_job_submission"])
        self.client.runs[binding["run_name"]] = run
        self.service.node_command(self.owner, node_id, {}, "original-stop")
        self.client.get.reset_mock()
        self.before_money = self.money()
        result = self.controller.tick()
        self.assertEqual(result["nodes"][0]["code"], "dstack_terminal_review_waiting_safety")
        self.assertFalse(self.controller.has_demand())
        self.assertEqual(self.money(), self.before_money)
        return node_id

    def money(self):
        with self.repo.engine.connect() as connection:
            return [[dict(row) for row in connection.execute(select(table).order_by(table.c.id)).mappings()]
                for table in (budget_accounts, budget_reservations)]

    def ledger(self):
        with self.repo.engine.connect() as connection:
            return [[dict(row) for row in connection.execute(select(table)).mappings()]
                for table in (operator_nodes, operator_commands, instance_intents, scaler_actions,
                              budget_accounts, budget_reservations, registered_workers, jobs, attempts)]

    def node(self, node_id):
        with self.repo.engine.connect() as connection:
            return dict(connection.execute(select(operator_nodes).where(
                operator_nodes.c.intent_id == node_id)).mappings().one())

    def test_first_existing_read_and_post_observation_stamp_keep_unknown_holds(self):
        node_id = self.unknown()
        self.assertEqual(self.client.get.call_count, 1)
        self.assertEqual(self.client.provider_inventory.call_count, 1)
        self.assertEqual(self.client.stopped, [])
        node = self.node(node_id)
        binding = node["payload"]["dstack"]
        self.assertEqual(node["runtime_state"], "creation_unknown")
        self.assertEqual(binding["billing_state"], "unsettled")
        self.assertNotIn("no_allocation_proof", binding)
        self.assertFalse(terminal_no_allocation(self.client.runs[binding["run_name"]], binding))
        before = self.ledger()
        self.controller.tick()
        self.assertFalse(self.controller.has_demand())
        result = self.controller.safety_sweep()
        self.assertEqual(result["business_writes"], 0)
        self.assertEqual(self.ledger(), before)
        self.assertEqual(self.client.get.call_count, 1)

    def test_existing_loop_cannot_postpone_safety_and_refreshes_once_at_900_seconds(self):
        self.unknown()
        before = self.ledger()
        stop, waits = threading.Event(), []
        clock = [0]
        outer = self
        class Wake:
            def subscribe(self): pass
            def wait(self, seconds):
                waits.append(seconds)
                clock[0] += seconds
                outer.now = 1000 + clock[0]
                if len(waits) == 2:
                    stop.set()
        run_demand_loop(stop, Wake(), self.controller.has_demand,
            lambda: self.fail("unchanged uncertainty must not run the fast actor"),
            active_seconds=15, safety=self.controller.safety_sweep, clock=lambda: clock[0])
        self.assertEqual(waits, [900, 900])
        self.assertEqual(self.client.get.call_count, 2)  # first read + one due exact-run read
        self.assertEqual(self.client.provider_inventory.call_count, 5)  # initial + two shared complete sweeps
        self.assertEqual(self.ledger(), before)

    def test_direct_tick_refuses_expired_stamp_and_observes_original_again(self):
        self.unknown()
        self.now += 900
        self.controller.tick()
        self.assertEqual(self.client.get.call_count, 2)
        self.assertEqual(self.money(), self.before_money)

    def test_new_command_and_changed_row_immediately_restore_fast_path(self):
        node_id = self.unknown()
        self.service.node_command(self.owner, node_id, {}, "second-stop-same-clock")
        self.assertTrue(self.controller.has_demand())
        self.assertNotIn(node_id, self.controller._passive_unknown)
        self.controller.tick()
        self.assertFalse(self.controller.has_demand())
        with self.repo.transaction() as connection:
            connection.execute(update(scaler_actions).where(scaler_actions.c.intent_id == node_id)
                .values(last_observed_at=self.now+1))
        self.assertTrue(self.controller.has_demand())

    def test_global_job_unsafe_attempt_and_cpu_child_always_win(self):
        node_id = self.unknown()
        job = self.job()
        with self.repo.transaction() as connection:
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(
                execution_plan={**job["execution_plan"], "dispatch_backend": "hatchet-v1"}))
        self.assertTrue(self.controller.has_demand())
        self.assertNotIn(node_id, self.controller._passive_unknown)
        with self.repo.transaction() as connection:
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="failed"))
        self.controller.tick()
        self.assertFalse(self.controller.has_demand())
        with self.repo.transaction() as connection:
            connection.execute(insert(attempts).values(id=str(uuid.uuid4()), job_id=job["id"], worker_id="original",
                fence=1, number=1, status="failed", created_at=self.now, updated_at=self.now,
                submission_started_at=self.now, upstream_task_id="original-task", upstream_stopped=0))
        self.assertTrue(self.controller.has_demand())
        with self.repo.transaction() as connection:
            connection.execute(update(attempts).where(attempts.c.job_id == job["id"]).values(upstream_stopped=1))
        self.controller.tick()
        self.assertFalse(self.controller.has_demand())
        self.processes.children[node_id] = object()
        self.assertTrue(self.controller.has_demand())

    def test_failed_partial_stale_or_nonempty_safety_inventory_cannot_suppress_work(self):
        node_id = self.unknown()
        for bad in (RuntimeError("PRIVATE not logged"),
                {"backend": "vastai", "complete": False, "instance_ids": [], "observed_at": self.now},
                {"backend": "vastai", "complete": True, "instance_ids": ["unexpected"], "observed_at": self.now},
                {"backend": "vastai", "complete": True, "instance_ids": [], "observed_at": self.now-31}):
            with self.subTest(bad=type(bad).__name__):
                before = self.ledger()
                def inventory(backend):
                    if backend == "vastai":
                        if isinstance(bad, Exception): raise bad
                        return bad
                    return {"backend": backend, "complete": True, "instance_ids": [], "observed_at": self.now}
                self.client.provider_inventory.side_effect = inventory
                self.controller.safety_sweep()
                self.assertTrue(self.controller.has_demand())
                self.assertNotIn(node_id, self.controller._passive_unknown)
                self.assertEqual(self.ledger(), before)
                self.client.provider_inventory.side_effect = lambda backend: {
                    "backend": backend, "complete": True, "instance_ids": [], "observed_at": self.now}
                self.controller.tick()
                self.assertFalse(self.controller.has_demand())

    def test_due_missing_nonterminal_or_wrong_owned_run_restores_fast_without_writes(self):
        node_id = self.unknown()
        original = self.client.get.side_effect
        binding = self.node(node_id)["payload"]["dstack"]
        good = copy.deepcopy(self.client.runs[binding["run_name"]])
        for bad in (None, {**good, "status": "running"}, {**good, "project_name": "other-owner"}):
            with self.subTest(bad=bad and bad["status"]):
                self.now += 900
                before = self.ledger()
                self.client.get.side_effect = lambda **kwargs: bad
                self.controller.safety_sweep()
                self.assertTrue(self.controller.has_demand())
                self.assertEqual(self.ledger(), before)
                self.client.get.side_effect = original
                self.controller.tick()
                self.assertFalse(self.controller.has_demand())

    def test_explicit_allocation_runtime_or_duplicate_marker_is_not_a_candidate(self):
        node_id = self.unknown()
        binding = self.node(node_id)["payload"]["dstack"]
        run = copy.deepcopy(self.client.runs[binding["run_name"]])
        self.assertTrue(_terminal_review_candidate(run, binding, self.client.project))
        for changes in ({"provider_instance_id": "123"}, {"runtime_incarnation": "ever-loaded"},
                {"first_runtime_ready_at": self.now}, {"bootstrap_started": True}):
            self.assertFalse(_terminal_review_candidate(run, {**binding, **changes}, self.client.project))
        for message in ('sixnine_vast_attempts_v1:{"version":1,"version":1}',
                run["latest_job_submission"]["termination_reason_message"].replace('"contract_id": null', '"contract_id": "123"')):
            changed = copy.deepcopy(run)
            changed["latest_job_submission"]["termination_reason_message"] = message
            changed["jobs"][0]["job_submissions"][0] = copy.deepcopy(changed["latest_job_submission"])
            self.assertFalse(_terminal_review_candidate(changed, binding, self.client.project))


if __name__ == "__main__":
    unittest.main()
