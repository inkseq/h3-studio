"""Registration capacity reconciliation, isolated ledger and fake reads only."""
import copy
import json
from unittest.mock import Mock, patch

from sqlalchemy import select, update

from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.dstack_controller import DstackController
from studio_platform.fleet import SlotConfig
from studio_platform.inference.wangp_contract import HostReadiness
from studio_platform.repository import (BudgetExceeded, Conflict, budget_reservations,
    instance_intents, registered_devices, registered_workers, scaler_receipts)
from test_platform_repository import LedgerCase
import test_dstack_operator as fixture
from test_dstack_capacity import no_capacity_run


class RegistrationCapacityTests(LedgerCase):
    preview = fixture.DstackOperatorTests.preview
    start = fixture.DstackOperatorTests.start

    def setUp(self):
        super().setUp()
        fixture.DstackOperatorTests._setup_dstack(self)
        config = copy.deepcopy(self.config)
        config["profiles"].append({**config["profiles"][0], "id": "5090-ref", "mode": "ref",
            "configuration_id": "test-native-ref"})
        self.service = fixture.DstackOperator(self.repo, self.settings, self.capacity, config)
        self.repo.configure_pool("dstack-test", max_instances=10, max_physical_gpus=10)
        self.service.policy.update(max_instances=10, max_physical_gpus=10,
            max_hourly_cost_microusd=10_000_000)
        self.service.provider_inventory = Mock(side_effect=lambda backend: {
            "backend": backend, "complete": True, "observed_at": self.now, "instance_ids": []})

    def node(self, identity, *, terminal=False, mode="fl"):
        preview = self.service.preview(self.owner, {"profile_id": "5090-" + mode, "ttl_seconds": 900})
        node = self.start(key="start-" + identity, preview=preview)["node_id"]
        binding = self.store.load(node)
        run = self.client.runs[binding["run_name"]]
        run.update(status="running", latest_job_submission={"job_provisioning_data": {
            "backend": "vastai", "instance_id": identity,
            "instance_type": {"resources": {"gpus": [{"name": "RTX5090"}]}}}})
        self.capacity.observe(node)
        if terminal:
            run["status"] = "terminated"
            self.capacity.observe(node)
        return node

    def spec(self, node):
        binding = self.store.load(node)
        return WorkerSpec("dstack-" + node.replace("-", ""), "dstack-test", "vast",
            binding["provider_instance_id"], ("GPU-" + binding["provider_instance_id"],),
            ({"fl": "h3-base-fl2va-v1", "ref": "h3-base-ref2va-v1"}[binding["mode"]],),
            binding["model_id"], binding["configuration_id"],
            "wangp-worker", binding["manifest_digest"], dispatch_backend="hatchet-v1")

    def controller(self):
        outer = self
        class Runtime:
            def close(self, intent_id=None):
                pass
            def readiness(self, binding, run):
                return HostReadiness(binding["manifest_digest"], binding["intent_id"], "c" * 32, True)
            def slot(self, intent_id):
                return SlotConfig(outer.spec(intent_id), True, "http://127.0.0.1:8199",
                    ("http://127.0.0.1:8199",), "", True,
                    runtime_config_file=str(__import__("pathlib").Path(outer.temp.name) / "native.json"))
        class Broker:
            def projection(self, slot):
                return {"ready": True, "observed_at": outer.now, "heartbeat_at": outer.now,
                    "worker_id": slot.spec.worker_id, "reason_code": None}
        return DstackController(self.service, Runtime(), broker_readiness=Broker())

    def snapshot(self):
        with self.repo.engine.connect() as connection:
            return ([dict(row) for row in connection.execute(select(instance_intents)).mappings()],
                [dict(row) for row in connection.execute(select(budget_reservations)).mappings()],
                self.repo.get_budget("owner-budget"))

    def usage(self, node):
        spec = self.spec(node)
        with self.repo.transaction() as connection:
            self.repo._lock_capacity(connection)
            return self.service.registration_capacity_usage(connection, spec)

    def test_nine_original_intents_allow_two_existing_slots_with_seven_proven_inactive(self):
        historical = [self.node(str(2000010 + i), terminal=True) for i in range(7)]
        current = [self.node("2000020"), self.node("2000021", mode="ref")]
        self.repo.configure_capacity(max_instances=4, max_physical_gpus=4)
        before = self.snapshot()
        with self.repo.engine.connect() as connection:
            self.assertEqual(self.repo._global_usage(connection)["instances"], 9)
        controller = self.controller()
        results = {item["node_id"]: item for item in controller.tick()["nodes"]}
        self.assertTrue(all(results[node]["state"] == "ready" for node in current))
        # The first native observations retain their timestamps/state, and
        # there is no rental, bill settlement or historical destruction mark.
        after = self.snapshot()
        for old in historical:
            self.assertEqual(next(row for row in after[0] if row["id"] == old),
                next(row for row in before[0] if row["id"] == old))
        self.assertEqual(after[1:], before[1:])
        self.assertEqual(len(self.client.applied), 9)
        with self.repo.engine.connect() as connection:
            workers = list(connection.execute(select(registered_workers)).mappings())
            self.assertEqual(len(workers), 2)
            self.assertTrue(all(row["state"] == "ready" for row in workers))
            self.assertEqual({tuple(row["spec"]["recipe_ids"]) for row in workers},
                {("h3-base-fl2va-v1",), ("h3-base-ref2va-v1",)})
            self.assertEqual(connection.execute(select(registered_devices.c.state)).scalars().all(), ["owned", "owned"])

    def test_never_allocated_history_does_not_block_registration_or_settle_reservation(self):
        failed = self.start(key="no-capacity-original")["node_id"]
        binding = self.store.load(failed)
        self.client.runs[binding["run_name"]] = no_capacity_run(self.client.runs[binding["run_name"]])
        self.capacity.observe(failed)
        live = self.node("2000001")
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        before = self.snapshot()
        self.assertEqual(self.usage(live)["resources"], {("vast", "2000001"): 1})
        WorkerControl(self.repo, capacity_usage=self.service.registration_capacity_usage).register(self.spec(live))
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(next(row for row in before[0] if row["id"] == failed)["provider_instance_id"])
        self.assertTrue(any(row["reference_id"] == failed and row["state"] == "reserved" for row in before[1]))
        self.service.provider_inventory.assert_not_called()

    def test_terminal_absent_allocation_before_identity_journal_does_not_recount_null_intent(self):
        old = self.start(key='unobserved-allocation')['node_id']
        binding = self.store.load(old)
        self.client.runs[binding['run_name']].update(status='failed', latest_job_submission={
            'job_provisioning_data':{'backend':'vastai', 'instance_id':'2000030',
                'instance_type':{'resources':{'gpus':[{'name':'RTX5090'}]}}}})
        # No earlier observation recorded an allocation ID. The supplier has
        # already removed this exact instance: terminal state is still usable
        # without fabricating physical telemetry or a ledger provider ID.
        original_identity = self.client.vast_instance_identity
        self.client.vast_instance_identity = lambda identity: (None if identity == '2000030'
            else original_identity(identity))
        self.assertEqual(self.capacity.observe_terminal(old)['state'], 'stopped')
        self.assertIsNone(self.store.load(old).get('provider_instance_id'))
        live = self.node('2000001')
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        before = self.snapshot()
        self.assertEqual(self.usage(live)['resources'], {('vast', '2000001'):1})
        WorkerControl(self.repo, capacity_usage=self.service.registration_capacity_usage).register(self.spec(live))
        self.assertEqual(self.snapshot(), before)
        for inventory in (None, {'backend':'vastai', 'complete':True,
                'observed_at':self.now-31, 'instance_ids':[]},
                {'backend':'vastai', 'complete':True, 'observed_at':self.now, 'instance_ids':['2000030']}):
            with self.subTest(inventory=inventory):
                self.service.provider_inventory = Mock(return_value=inventory)
                self.assertEqual(self.usage(live)['resources'][('vast', 'intent:'+old)], 1)
        self.service.provider_inventory = Mock(return_value={
            'backend':'vastai', 'complete':True, 'observed_at':self.now, 'instance_ids':[]})
        with self.repo.transaction() as connection:
            connection.execute(update(instance_intents).where(instance_intents.c.id==old)
                .values(provider_instance_id='conflicting-ledger-instance'))
        self.assertEqual(self.usage(live)['instances'], 2)

    def test_unknown_terminal_history_does_not_block_two_accounted_live_slots_or_settle_money(self):
        failed = self.start(key="unknown-original")["node_id"]
        binding = self.store.load(failed)
        run = no_capacity_run(self.client.runs[binding["run_name"]])
        message = "sixnine_vast_attempts_v1:" + json.dumps({"version": 1, "outcome": "unknown",
            "attempted_count": 1, "attempts": [{"ask_id": "12345", "http_status": 400,
                "code": "mutation_unknown", "outcome": "unknown", "contract_id": None}]})
        run["latest_job_submission"]["termination_reason_message"] = message
        run["jobs"][0]["job_submissions"][0]["termination_reason_message"] = message
        self.client.runs[binding["run_name"]] = run
        self.capacity.observe(failed)
        live = [self.node("2000020"), self.node("2000021", mode="ref")]
        self.service.provider_inventory = Mock(return_value={"backend": "vastai", "complete": True,
            "observed_at": self.now, "instance_ids": ["2000020", "2000021"]})
        self.repo.configure_capacity(max_instances=2, max_physical_gpus=2)
        original_run, original_binding, before = copy.deepcopy(run), self.store.load(failed), self.snapshot()
        self.assertEqual(self.usage(live[0])["instances"], 2)
        control = WorkerControl(self.repo, capacity_usage=self.service.registration_capacity_usage)
        for node in live:
            control.register(self.spec(node))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.store.load(failed), original_binding)
        self.assertEqual(self.client.runs[binding["run_name"]], original_run)
        held = [row for row in before[1] if row["reference_id"] == failed]
        self.assertTrue(held)
        self.assertTrue(all(row["state"] == "reserved" and row["actual_cost_microusd"] is None for row in held))
        self.service.provider_inventory.return_value["instance_ids"].append("2999999")
        self.assertEqual(self.usage(live[0])["instances"], 3)

    def test_overlay_retains_legacy_instances_and_unreleased_devices(self):
        old = self.node("2000002", terminal=True)
        live = self.node("2000001")
        self.repo.configure_pool("legacy", max_instances=1, max_physical_gpus=1)
        legacy = self.repo.reserve_instance_intent(self.scope, "legacy", "legacy-intent",
            provider="lium", physical_gpus=1, hard_deadline=2000,
            reserved_cost_microusd=100_000, budget_account_ids=["owner-budget"], dry_run=False)
        WorkerControl(self.repo).register(self.spec(old))
        usage = self.usage(live)
        self.assertEqual(usage["resources"], {("vast", "2000001"): 1, ("vast", "2000002"): 1,
            ("lium", "intent:" + legacy["id"]): 1})
        self.assertEqual((usage["instances"], usage["physical_gpus"]), (3, 3))
        self.assertEqual(next(row for row in self.snapshot()[0] if row["id"] == old)["state"], "destroying")

    def test_partial_wrong_stale_present_or_failed_account_reads_cannot_free_capacity(self):
        self.node("2000002", terminal=True)
        live = self.node("2000001")
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        good = {"backend": "vastai", "complete": True, "observed_at": self.now, "instance_ids": []}
        cases = [None, {**good, "backend": "runpod"}, {**good, "complete": False},
            {**good, "observed_at": self.now-31}, {**good, "instance_ids": ["2000002"]}]
        before = self.snapshot()
        for value in cases:
            with self.subTest(value=value):
                self.service.provider_inventory = Mock(return_value=value)
                with self.assertRaisesRegex(BudgetExceeded, "global_capacity_exceeded"):
                    WorkerControl(self.repo, capacity_usage=self.service.registration_capacity_usage).register(self.spec(live))
                self.assertEqual(self.snapshot(), before)
        self.service.provider_inventory = Mock(side_effect=ValueError("PRIVATE-ACCOUNT-SECRET"))
        with self.assertRaisesRegex(BudgetExceeded, "global_capacity_exceeded"):
            WorkerControl(self.repo, capacity_usage=self.service.registration_capacity_usage).register(self.spec(live))
        self.assertEqual(self.snapshot(), before)

    def test_absent_inventory_does_not_exclude_running_or_identity_drifted_history(self):
        old = self.node("2000002")
        live = self.node("2000001")
        self.assertEqual(self.usage(live)["instances"], 2)
        run = self.client.runs[self.store.load(old)["run_name"]]
        run["status"] = "terminated"
        self.capacity.observe(old)
        with self.repo.transaction() as connection:
            connection.execute(update(instance_intents).where(instance_intents.c.id == old)
                .values(provider_instance_id="different-ledger-instance"))
        self.assertEqual(self.usage(live)["instances"], 2)

    def test_current_run_change_cannot_register_from_old_native_readiness(self):
        node = self.node("2000001")
        run = self.client.runs[self.store.load(node)["run_name"]]
        run["status"] = "terminated"
        with self.assertRaisesRegex(fixture.OperatorError, "dstack_registration_run_unconfirmed"):
            WorkerControl(self.repo, capacity_usage=self.service.registration_capacity_usage).register(self.spec(node))
        with self.repo.engine.connect() as connection:
            self.assertIsNone(connection.execute(select(registered_workers.c.id)).first())

    def test_current_instance_or_backend_drift_and_malformed_identity_cannot_register(self):
        node = self.node("2000001")
        spec = self.spec(node)
        run = self.client.runs[self.store.load(node)["run_name"]]
        original = copy.deepcopy(run["latest_job_submission"])
        before = self.snapshot()
        cases = [{"instance_id": "replacement-different-instance"}, {"backend": "runpod"},
            {"instance_id": None}, {"instance_id": {"private": "PRIVATE-SECRET"}},
            {"instance_id": "PRIVATE-SECRET:/bad-id"}]
        for changed in cases:
            with self.subTest(fields=tuple(changed)):
                run["latest_job_submission"] = copy.deepcopy(original)
                run["latest_job_submission"]["job_provisioning_data"].update(changed)
                with self.assertRaisesRegex(fixture.OperatorError, "dstack_registration_run_unconfirmed"):
                    WorkerControl(self.repo, capacity_usage=self.service.registration_capacity_usage).register(spec)
                self.assertEqual(self.snapshot(), before)
                with self.repo.engine.connect() as connection:
                    self.assertIsNone(connection.execute(select(registered_workers.c.id)).first())
                    self.assertIsNone(connection.execute(select(registered_devices.c.gpu_id)).first())

    def test_current_actual_multigpu_or_unconfirmed_gpu_shape_is_rejected(self):
        node = self.node("2000001")
        spec = self.spec(node)
        run = self.client.runs[self.store.load(node)["run_name"]]
        resource = run["latest_job_submission"]["job_provisioning_data"]["instance_type"]["resources"]
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        before = self.snapshot()
        for gpus in ([{"name": "RTX5090"}, {"name": "RTX5090"}], [], None, "PRIVATE-SECRET"):
            with self.subTest(shape=type(gpus).__name__):
                resource["gpus"] = gpus
                with self.assertRaisesRegex(fixture.OperatorError, "dstack_registration_allocation_unconfirmed"):
                    WorkerControl(self.repo, capacity_usage=self.service.registration_capacity_usage).register(spec)
                self.assertEqual(self.snapshot(), before)
                with self.repo.engine.connect() as connection:
                    self.assertIsNone(connection.execute(select(registered_workers.c.id)).first())
                    self.assertIsNone(connection.execute(select(registered_devices.c.gpu_id)).first())

    def test_other_included_actual_allocation_counts_above_original_intent(self):
        other = self.node("2000003")
        live = self.node("2000001")
        run = self.client.runs[self.store.load(other)["run_name"]]
        run["latest_job_submission"]["job_provisioning_data"]["instance_type"]["resources"]["gpus"] = [
            {"name": "RTX5090"}, {"name": "RTX5090"}]
        before = self.snapshot()
        with patch.object(self.client, "get", wraps=self.client.get) as read:
            usage = self.usage(live)
            self.assertEqual(read.call_count, 2)  # No additional provider/run read.
        self.assertEqual(usage["resources"], {("vast", "2000003"): 2, ("vast", "2000001"): 1})
        self.assertEqual((usage["instances"], usage["physical_gpus"]), (2, 3))
        self.repo.configure_capacity(max_instances=2, max_physical_gpus=2)
        with self.assertRaisesRegex(BudgetExceeded, "global_capacity_exceeded"):
            WorkerControl(self.repo, capacity_usage=self.service.registration_capacity_usage).register(self.spec(live))
        self.assertEqual(self.snapshot(), before)
        with self.repo.engine.connect() as connection:
            self.assertIsNone(connection.execute(select(registered_workers.c.id)).first())

    def test_registration_capacity_pause_and_existing_idempotency_are_preserved(self):
        node = self.node("2000001")
        control = WorkerControl(self.repo, capacity_usage=self.service.registration_capacity_usage)
        self.repo.configure_capacity()
        with self.assertRaisesRegex(BudgetExceeded, "global_capacity_exceeded"):
            control.register(self.spec(node))
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        worker = control.register(self.spec(node))
        with patch.object(self.service, "registration_capacity_usage", side_effect=AssertionError("should not read")):
            control._capacity_usage = self.service.registration_capacity_usage
            self.assertEqual(control.register(self.spec(node)), worker)
        from dataclasses import replace
        with self.assertRaisesRegex(Conflict, "physical_gpu_already_owned"):
            control.register(replace(self.spec(node), worker_id="another-worker"))

    def test_static_blocker_is_projected_separately_then_cleared_without_repeated_receipts(self):
        self.node("2000002", terminal=True)
        live = self.node("2000001")
        self.repo.configure_capacity(max_instances=1, max_physical_gpus=1)
        self.service.provider_inventory = Mock(return_value=None)
        controller = self.controller()
        before = self.snapshot()[1:]
        for _ in range(2):
            results = {row["node_id"]: row for row in controller.tick()["nodes"]}
            self.assertEqual(results[live]["code"], "global_capacity_exceeded")
        node = next(row for row in self.service.state(self.owner)["nodes"] if row["node_id"] == live)
        self.assertTrue(node["native_ready"])
        self.assertFalse(node["ready"])
        self.assertEqual(node["dispatch_reason_code"], "global_capacity_exceeded")
        self.assertEqual(node["registration_reason_code"], "global_capacity_exceeded")
        with self.repo.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(scaler_receipts).where(
                scaler_receipts.c.intent_id == live, scaler_receipts.c.operation == "registration")).all()), 1)
        self.service.provider_inventory = Mock(return_value={"backend": "vastai", "complete": True,
            "observed_at": self.now, "instance_ids": ["2000001"]})
        controller.tick()
        node = next(row for row in self.service.state(self.owner)["nodes"] if row["node_id"] == live)
        self.assertIsNone(node["registration_reason_code"])
        self.assertTrue(node["ready"])
        self.assertEqual(self.snapshot()[1:], before)

    def test_arbitrary_exception_text_never_becomes_public_code(self):
        node = self.node("2000001")
        controller = self.controller()
        for error in (BudgetExceeded("PRIVATE-SECRET"), ValueError("PRIVATE-SECRET"),
                BudgetExceeded("global_capacity_exceeded", "PRIVATE-SECRET")):
            with self.subTest(error=type(error).__name__), patch.object(controller, "_tick_node", side_effect=error):
                result = controller.tick()["nodes"][0]
                self.assertEqual(result["code"], "dstack_controller_observation_failed")
                self.assertNotIn("PRIVATE", str(result))
        with self.assertRaises(fixture.OperatorError):
            self.store.record_registration(node, "known-worker", "PRIVATE-SECRET")
