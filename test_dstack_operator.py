"""Original-ledger dstack commands, using isolated databases and fake HTTP only."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from sqlalchemy import func, insert, select, update

from studio_platform.auth import Principal
from studio_platform.dstack_capacity import DstackCapacity, DstackError
from studio_platform.dstack_operator import (DstackOperator, LedgerDstackStore,
    OperatorError, from_environment)
from studio_platform.inference.wangp_contract import HostReadiness
from studio_platform.operator_capacity import operator_commands, operator_nodes
from studio_platform.repository import (BudgetExceeded, attempts, budget_accounts, budget_reservations,
    instance_intents, jobs, registered_workers, scaler_actions)
from test_platform_repository import LedgerCase

PROFILE = "h3-pruned-rank8-int8-quanto-int8-vae-int8-sdpa-p4-lowram-v1"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey"


class Client:
    project = "sixnine"

    def __init__(self, store):
        self.store = store
        self.runs = {}
        self.applied = []
        self.stopped = []
        self.lost = False
        self.absent = False

    def plan(self, spec):
        return {"run_spec": spec, "job_plans": [{"offers":[{"backend":"vastai","region":"test-region",
            "price":.7,"availability":"available","backend_data":"SECRET",
            "instance":{"resources":{"cpus":12,"memory_mib":96*1024,"disk":{"size_mib":128*1024},
                "gpus":[{"name":"RTX5090","memory_mib":32*1024}]}}}]}]}

    def get(self, *, run_name=None, run_id=None):
        if self.absent:
            return None
        result = self.runs.get(run_name) if run_name else next(
            (run for run in self.runs.values() if run["id"] == run_id), None)
        return copy.deepcopy(result)

    def apply(self, spec):
        intent = spec["configuration"]["tags"]["sixnine_intent"]
        binding = self.store.load(intent)
        assert binding["apply_started"] is True
        with self.store.repo.engine.connect() as connection:
            assert connection.execute(select(budget_reservations).where(
                budget_reservations.c.reference_id == intent)).mappings().one()["state"] == "reserved"
        self.applied.append(intent)
        value = {"id": str(uuid.uuid4()), "project_name": self.project,
            "run_spec": copy.deepcopy(spec), "status": "submitted", "cost": .1}
        self.runs[spec["run_name"]] = value
        if self.lost:
            raise DstackError("dstack_api_unavailable")
        return copy.deepcopy(value)

    def stop(self, name):
        self.stopped.append(name)


class DstackOperatorTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.repo.configure_pool("dstack-test", max_instances=3, max_physical_gpus=3)
        self.settings = SimpleNamespace(tenant_id=self.scope.tenant_id,
            operator_capacity_owners=("superdan", "supervan"))
        self.owner = Principal("superdan", "browser-1")
        self.other_owner = Principal("supervan", "browser-2")
        self.store = LedgerDstackStore(self.repo, self.scope.tenant_id)
        self.client = Client(self.store)
        self.capacity = DstackCapacity(self.client, self.store, clock=self.repo.clock,
            readiness=lambda binding, run: HostReadiness(binding["manifest_digest"], binding["intent_id"], "c"*32, True))
        self.config = {"version": 1, "ssh_public_key": PUB,
            "policy": {"enabled": True, "expires_at": 5000., "max_instances": 3, "max_physical_gpus": 3,
                "max_hourly_cost_microusd": 3000000, "max_ttl_seconds": 1800,
                "idle_shutdown_seconds": 600, "observation_fresh_seconds": 30},
            "profiles": [{"id": "5090-fl", "owner_id": "superdan", "project_id": "project-1",
                "budget_account_ids": ["owner-budget"], "pool": "dstack-test", "backend": "vastai",
                "runtime_profile_id": PROFILE, "mode": "fl", "configuration_id": "test-native",
                "image": "example/native@sha256:" + "a"*64, "gpu_names": ["RTX5090"],
                "memory_gib": 96, "disk_gib": 128, "gpu_memory_gib": 30, "cpu_count": 12,
                "max_price_microusd": 750000, "max_ttl_seconds": 1800, "extra_reservation_microusd": 0}]}
        self.service = DstackOperator(self.repo, self.settings, self.capacity, self.config)

    def preview(self):
        return self.service.preview(self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900})

    def start(self, key="start-1", preview=None):
        preview = preview or self.preview()
        return self.service.start(self.owner, {"preview_id": preview["preview_id"]}, key)

    def counts(self):
        with self.repo.engine.connect() as connection:
            return tuple(connection.execute(select(func.count()).select_from(table)).scalar_one()
                for table in (instance_intents, budget_reservations, operator_nodes, scaler_actions))

    def running(self, node_id):
        binding = self.store.load(node_id)
        run = self.client.runs[binding["run_name"]]
        run.update(status="running", latest_job_submission={"job_provisioning_data": {
            "backend": "vastai", "instance_id": "gpu-123", "instance_type": {"resources": {"gpus": [{"name": "RTX5090"}]}}}})
        return self.capacity.observe(node_id)

    def test_reserves_atomically_before_apply_and_replay_does_not_rent(self):
        preview = self.preview()
        result = self.start(preview=preview)
        again = self.start(preview=preview)
        self.assertEqual(result, again)
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        self.assertEqual(len(self.client.applied), 1)
        with self.repo.engine.connect() as connection:
            account = connection.execute(select(budget_accounts)).mappings().one()
            self.assertEqual(account["reserved_microusd"], preview["reservation_microusd"])
            self.assertEqual(account["spent_microusd"], 0)
            command = connection.execute(select(operator_commands)).mappings().one()
            self.assertEqual(command["kind"], "dstack_start")

    def test_parallel_identical_start_has_one_original_reservation(self):
        preview = self.preview()
        results = self.parallel(lambda _: self.start(preview=preview))
        self.assertEqual(len({value["node_id"] for value in results}), 1)
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        self.assertEqual(len(self.client.applied), 1)

    def test_budget_failure_rolls_back_command_and_intent_without_apply(self):
        self.repo.configure_budget("owner-budget", tenant_id=self.scope.tenant_id,
            owner_id=self.scope.owner_id, limit_microusd=1)
        with self.assertRaises(BudgetExceeded):
            self.start()
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.client.applied, [])

    def test_global_or_hourly_limits_prevent_side_effects(self):
        self.repo.configure_capacity(max_instances=0, max_physical_gpus=0)
        with self.assertRaises(BudgetExceeded):
            self.start()
        self.assertEqual(self.client.applied, [])
        self.repo.configure_capacity(max_instances=10, max_physical_gpus=10)
        self.service.policy["max_hourly_cost_microusd"] = 1
        with self.assertRaisesRegex(OperatorError, "dstack_hourly_limit_exceeded"):
            self.start()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_profile_floors_and_mode_cannot_be_weakened(self):
        for field, value in (("memory_gib", 32), ("disk_gib", 80), ("gpu_memory_gib", 16),
                ("gpu_names", ["A100"]), ("mode", "other"), ("image", "runtime:latest")):
            config = copy.deepcopy(self.config)
            config["profiles"][0][field] = value
            with self.assertRaises((OperatorError, DstackError)):
                DstackOperator(self.repo, self.settings, self.capacity, config)

    def test_owner_isolation_and_machine_principal_denied(self):
        node = self.start()["node_id"]
        self.assertEqual(self.service.catalog(self.other_owner)["profiles"], [])
        self.assertEqual(self.service.state(self.other_owner)["nodes"], [])
        with self.assertRaisesRegex(OperatorError, "dstack_node_not_found"):
            self.service.node_command(self.other_owner, node, {}, "stop-1")
        with self.assertRaisesRegex(OperatorError, "operator_forbidden"):
            self.service.catalog(Principal("superdan", "api", machine=True))

    def test_29gib_profile_admission_binds_each_mode_and_preserves_legacy_floor(self):
        from studio_platform.runtime_catalog import engine_manifest, PROFILE_IDS
        revised = PROFILE_IDS[4]
        for mode in ('fl', 'ref'):
            config = copy.deepcopy(self.config)
            profile = config['profiles'][0]
            profile.update(runtime_profile_id=revised, mode=mode, memory_gib=29,
                           configuration_id='test-native-29gib-' + mode)
            service = DstackOperator(self.repo, self.settings, self.capacity, config)
            request = service._capacity_request(service.profiles['5090-fl'], str(uuid.uuid4()), 1., 121., 120)
            self.assertEqual(request.memory_gib, 29)
            self.assertEqual(request.manifest_digest, engine_manifest(revised, mode).digest)
            self.assertEqual(request.configuration_id, 'test-native-29gib-' + mode)
            profile['memory_gib'] = 28
            with self.assertRaisesRegex(OperatorError, 'resource_floor_invalid'):
                DstackOperator(self.repo, self.settings, self.capacity, config)
        original = copy.deepcopy(self.config)
        original['profiles'][0]['memory_gib'] = 29
        with self.assertRaisesRegex(OperatorError, 'resource_floor_invalid'):
            DstackOperator(self.repo, self.settings, self.capacity, original)
        self.assertEqual(self.client.applied, [])
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_lost_apply_and_missing_run_never_reapply_or_release_reservation(self):
        self.client.lost = True
        node = self.start()["node_id"]
        binding = self.store.load(node)
        self.client.absent = True
        request = self.service._capacity_request(self.service.profiles["5090-fl"], node,
            binding["created_at"], binding["hard_deadline"], 900)
        for _ in range(3):
            self.assertEqual(self.capacity.start(request, PUB)["state"], "creation_unknown")
        self.assertEqual(len(self.client.applied), 1)
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(budget_reservations.c.state)).scalar_one(), "reserved")

    def test_run_identity_and_incarnation_are_immutable(self):
        node = self.start()["node_id"]
        self.running(node)
        for key, value in (("run_id", str(uuid.uuid4())), ("provider_instance_id", "other-gpu"),
                ("runtime_incarnation", "d"*32)):
            with self.assertRaisesRegex(OperatorError, "dstack_observation_identity_changed"):
                self.store.record(node, {key: value, "state": "ready", "ready": True, "observed_at": self.now})

    def test_ready_requires_native_proof_and_fresh_observation(self):
        node = self.start()["node_id"]
        self.capacity.readiness = None
        self.running(node)
        self.assertFalse(self.service.state(self.owner)["nodes"][0]["ready"])
        self.capacity.readiness = lambda binding, run: HostReadiness(binding["manifest_digest"], node, "c"*32, True)
        self.running(node)
        state=self.service.state(self.owner)["nodes"][0]
        self.assertTrue(state["native_ready"])
        self.assertFalse(state["ready"])  # Native endpoint alone has no CPU consumer.
        self.now += 31
        self.assertFalse(self.service.state(self.owner)["nodes"][0]["ready"])
        self.assertFalse(self.service.state(self.owner)["nodes"][0]["native_ready"])

    def test_no_stock_or_unconfirmed_plan_cannot_offer_paid_start(self):
        self.client.plan=lambda spec:{"job_plans":[{"offers":[]}]}
        with self.assertRaisesRegex(OperatorError,"dstack_no_compatible_offers"):self.preview()
        self.client.plan=lambda spec:{"job_plans":[]}
        with self.assertRaisesRegex(OperatorError,"dstack_plan_unconfirmed"):self.preview()
        self.assertEqual(self.counts(),(0,0,0,0))

    def test_quote_projection_is_safe_and_unknown_is_not_confirmed_stock(self):
        original=self.client.plan
        def unknown(spec):
            value=original(spec)
            value["job_plans"][0]["offers"][0]["availability"]="unknown"
            return value
        self.client.plan=unknown
        preview=self.preview()
        self.assertFalse(preview["offers"][0]["inventory_confirmed"])
        self.assertNotIn("SECRET",json.dumps(preview))

    def test_hold_does_not_extend_deadline_increase_budget_or_repeat(self):
        node = self.start()["node_id"]
        first = self.service.set_hold(self.owner, node, {"hold_seconds": 300}, "hold-1")
        self.now += 10
        self.assertEqual(self.service.set_hold(self.owner, node, {"hold_seconds": 300}, "hold-1"), first)
        self.assertEqual(first["hard_deadline"], 1900)
        self.assertEqual(first["additional_reservation_microusd"], 0)
        with self.assertRaisesRegex(OperatorError, "dstack_hold_deadline_exceeded"):
            self.service.set_hold(self.owner, node, {"hold_seconds": 1000}, "hold-2")

    def test_stop_needs_owner_drain_and_preserves_unverified_bill(self):
        node = self.start()["node_id"]
        self.running(node)
        self.assertFalse(self.store.begin_stop(node, self.store.load(node)["run_id"]))
        self.service.node_command(self.owner, node, {}, "stop-1")
        self.assertFalse(self.service.state(self.owner)["nodes"][0]["ready"])
        self.capacity.stop(node)
        self.capacity.stop(node)
        self.assertEqual(len(self.client.stopped), 1)
        self.client.runs[self.store.load(node)["run_name"]]["status"] = "terminated"
        self.capacity.observe(node)
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(instance_intents.c.state)).scalar_one(), "destroying")
            self.assertEqual(connection.execute(select(budget_reservations.c.state)).scalar_one(), "reserved")

    def test_original_worker_and_job_obligations_block_stop(self):
        node = self.start()["node_id"]
        self.running(node)
        job = self.job()
        with self.repo.transaction() as connection:
            connection.execute(insert(registered_workers).values(id="worker-1", pool="dstack-test", provider="vast",
                instance_id="gpu-123", spec={}, spec_hash="a"*64, state="busy", current_job_id=job["id"],
                drain_requested=0, fence=1, expires_at=self.now+60, updated_at=self.now))
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(lease_worker_id="worker-1"))
        self.service.node_command(self.owner, node, {}, "stop-1")
        self.assertEqual(self.capacity.stop(node)["state"], "draining")
        with self.repo.transaction() as connection:
            connection.execute(update(registered_workers).values(state="retired", current_job_id=None))
        self.assertEqual(self.capacity.stop(node)["state"], "draining")
        with self.repo.transaction() as connection:
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="cancelled"))
        self.capacity.stop(node)
        self.assertEqual(len(self.client.stopped), 1)

    def test_consumed_expired_or_changed_preview_cannot_create(self):
        preview = self.preview()
        self.start(preview=preview)
        with self.assertRaisesRegex(OperatorError, "dstack_preview_consumed"):
            self.start(key="different", preview=preview)
        preview = self.preview()
        self.now += 121
        with self.assertRaisesRegex(OperatorError, "dstack_preview_expired"):
            self.start(key="expired", preview=preview)
        self.assertEqual(len(self.client.applied), 1)

    def test_bootstrap_journal_precedes_remote_start_and_unknown_blocks_stop(self):
        node = self.start()["node_id"]
        self.capacity.readiness = None
        self.running(node)
        binding = self.store.load(node)
        self.assertTrue(self.store.begin_bootstrap(node, binding["run_id"], "gpu-123"))
        self.assertFalse(self.store.begin_bootstrap(node, binding["run_id"], "gpu-123"))
        self.assertTrue(self.store.begin_runtime_launch(node, binding["run_id"], "gpu-123"))
        self.service.node_command(self.owner, node, {}, "stop-1")
        self.assertEqual(self.capacity.stop(node)["state"], "draining")
        self.assertEqual(self.client.stopped, [])

    def test_retired_worker_and_terminal_job_with_unknown_upstream_still_block_stop(self):
        node = self.start()["node_id"]
        self.running(node)
        job = self.job()
        with self.repo.transaction() as connection:
            connection.execute(insert(registered_workers).values(id="worker-1", pool="dstack-test", provider="vast",
                instance_id="gpu-123", spec={}, spec_hash="a"*64, state="retired", current_job_id=None,
                drain_requested=1, fence=1, expires_at=self.now-60, updated_at=self.now))
            connection.execute(update(jobs).where(jobs.c.id == job["id"]).values(status="failed"))
            connection.execute(insert(attempts).values(id=str(uuid.uuid4()), job_id=job["id"], number=1,
                status="submission_unknown", fence=1, worker_id="worker-1", created_at=self.now,
                updated_at=self.now, submission_started_at=self.now, upstream_stopped=0, collection_failures=0))
        self.service.node_command(self.owner, node, {}, "stop-1")
        self.assertEqual(self.capacity.stop(node)["state"], "draining")
        with self.repo.transaction() as connection:
            connection.execute(update(attempts).where(attempts.c.job_id == job["id"]).values(upstream_stopped=1))
        self.capacity.stop(node)
        self.assertEqual(len(self.client.stopped), 1)

    def test_cancel_releases_only_atomic_never_applied_proof(self):
        # Crash before the start journal is a provably unpaid reserved command.
        with patch.object(self.capacity, "start", side_effect=DstackError("dstack_client_not_started")):
            node = self.start()["node_id"]
        self.service.node_command(self.owner, node, {}, "stop-1")
        self.assertTrue(self.store.cancel_unapplied(node))
        self.assertFalse(self.store.cancel_unapplied(node))
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(budget_reservations.c.state)).scalar_one(), "released")
            self.assertEqual(connection.execute(select(budget_reservations.c.actual_cost_microusd)).scalar_one(), 0)
        node = self.start(key="started-2")["node_id"]
        self.service.node_command(self.owner, node, {}, "stop-2")
        self.assertFalse(self.store.cancel_unapplied(node))

    def test_disabled_factory_has_no_client_io_and_protected_config_no_plaintext_token(self):
        with patch.dict(os.environ, {"DSTACK_OPERATOR_CONFIG": ""}):
            self.assertFalse(from_environment(self.repo, self.settings).state(self.owner)["enabled"])
        root = Path(self.temp.name)
        token = root / "token"
        token.write_text("fake-private-token-32-characters-long")
        pub = root / "key.pub"
        pub.write_text(PUB)
        filename = root / "config.json"
        config = copy.deepcopy(self.config)
        config.pop("ssh_public_key")
        config.update(endpoint="http://127.0.0.1:3000", project="sixnine", token_file=str(token), ssh_public_key_file=str(pub))
        filename.write_text(json.dumps(config))
        for path in (filename, token, pub):
            path.chmod(0o600)
        with patch.dict(os.environ, {"DSTACK_OPERATOR_CONFIG": str(filename)}), patch("httpx.Client.post", side_effect=AssertionError("network forbidden")):
            service = from_environment(self.repo, self.settings)
            self.assertTrue(service.catalog(self.owner)["enabled"])
            self.assertNotIn("fake-private-token", json.dumps(service.config))
            service.capacity.client.close()
