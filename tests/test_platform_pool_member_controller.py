"""E2 transaction primitives: synthetic provider, isolated SQLite/PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import select

from studio_platform.on_demand_scaler import OnDemandConfig, json_config
from studio_platform.autoscale import ScalePolicy
from studio_platform.capabilities import MODEL, compile_request
from studio_platform.capacity import ColdStartCoordinator
from studio_platform.execution_policy import ExecutionPolicies
from studio_platform.pool_member_controller import MemberLaunchCoordinator, port_for_member
from studio_platform.production_scaler import ScalerError, save
from studio_platform.repository import (Conflict, Scope, capacity_pool_members, instance_intents,
    scaler_actions, scaler_receipts)
from studio_platform.scaler import LaunchSpec, ScaleCoordinator
from studio_platform.settings import Settings
from studio_platform.service_policy import TWO_MEMBER_MODE, validate_service_config, validate_service_policy
from test_platform_repository import LedgerCase
from test_platform_api import generation_request
from test_platform_execution_policy import policy
from test_platform_scaler import FakeProvider
import test_platform_pool_members as fixture
import test_platform_production_scaler as production
import test_platform_service_policy as service


class MemberLaunchTests(LedgerCase):
    waiter = fixture.PoolMemberTests.waiter
    worker = fixture.PoolMemberTests.worker
    write = fixture.PoolMemberTests.write
    approve = fixture.PoolMemberTests.approve
    rows = fixture.PoolMemberTests.rows

    def setUp(self):
        super().setUp()
        self.value = policy(self.now)
        self.value.update(pool="cold-pool", configuration_id="cold-config", budget_accounts=["owner:{owner_id}"],
            backend="wangp-worker", engine_manifest_digest=fixture.DIGEST)
        self.value["envelope"]["controls"].pop("ref_image_size")
        self.path = Path(self.temp.name)/"synthetic-policy.json"
        self.write()
        self.settings = Settings(Path(self.temp.name)/"data", generation_enabled=True,
            execution_backend="wangp-worker", execution_policy_file=self.path)
        self.compiled, self.fingerprint = compile_request(generation_request(), lambda _: None)
        for owner in ("superdan", "supervan"):
            self.repo.configure_budget("owner:"+owner, tenant_id=self.scope.tenant_id, owner_id=owner, limit_microusd=10_000_000)
        self.repo.configure_budget("capacity-budget", tenant_id=self.scope.tenant_id, limit_microusd=10_000_000)
        self.billing_scope = Scope(self.scope.tenant_id, "operator", "platform-capacity")
        self.launch = LaunchSpec("test-only", "cold-config", MODEL)
        self.scale_policy = ScalePolicy(dry_run=False, max_instances=2, max_physical_gpus=2,
            new_instance_physical_gpus=1, new_instance_slots=1, cold_start_s=30,
            queue_target_s=60, min_improvement_s=1, cooldown_s=0, idle_before_drain_s=600,
            approved_remaining_microusd=10_000_000, instance_reservation_microusd=200_000, hard_deadline=self.now+3600)
        self.provider = FakeProvider()
        self.policies = ExecutionPolicies(self.settings, self.repo)
        self.repo.configure_pool("cold-pool", max_instances=2, max_physical_gpus=2)
        self.grant = self.approve(backend="wangp-worker", engine_manifest_digest=fixture.DIGEST, pool_members=fixture.MEMBERS)
        self.admission = self.policies.evaluate(self.compiled, self.scope, self.fingerprint)
        self.assertTrue(self.admission.execution["enabled"], self.admission.execution)
        self.controller = ColdStartCoordinator(self.repo, enabled=True,
            approval_guard=self.policies.capacity_approval_current, activation_guard=self.policies.activation_allowed)
        self.scaler = ScaleCoordinator(self.repo, provider=self.provider, enabled=True,
            preparation_timeout_s=120, preparation_binding="b"*64)
        self.launcher = MemberLaunchCoordinator(self.scaler,
            approval_guard=self.policies.capacity_approval_current, job_guard=self.policies.activation_allowed,
            budget_ceiling_microusd=10_000_000)
        self.lease = self.scaler.acquire("cold-pool", "pool-owner")

    def create(self, member):
        return self.launcher.create_once(self.lease, "cold-approval", member)

    def test_no_confirmation_never_reserves_or_contacts_provider(self):
        self.assertEqual(self.create("a"), {"state": "no_confirmed_pool_demand"})
        self.assertEqual(self.rows(instance_intents), [])
        self.assertEqual(self.provider.creates, [])

    def test_two_original_members_use_one_selector_distinct_intents_and_atomic_start_barriers(self):
        job = self.waiter()
        original = self.provider.create
        def inspect(tag, launch, **kwargs):
            with self.repo.engine.connect() as connection:
                binding = connection.execute(select(capacity_pool_members).where(
                    capacity_pool_members.c.intent_id == tag)).mappings().one()
                action = connection.execute(select(scaler_actions).where(scaler_actions.c.intent_id == tag)).mappings().one()
                intent = connection.execute(select(instance_intents).where(instance_intents.c.id == tag)).mappings().one()
                phase = self.scaler.preparation(connection, intent)
            self.assertEqual(binding["approval_hash"], self.grant["approval_hash"])
            self.assertEqual(action["launch_spec"], asdict(self.launch))
            self.assertEqual(intent["state"], "creating")
            self.assertEqual(phase["phase"], "awaiting_provider")
            return original(tag, launch, **kwargs)
        self.provider.create = inspect
        first, second = self.create("a"), self.create("b")
        self.assertNotEqual(first["intent_id"], second["intent_id"])
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 400_000)
        self.assertEqual(len(self.rows(scaler_actions)), 2)
        self.assertEqual(len([r for r in self.rows(scaler_receipts) if r["operation"] == "provider_preparation"]), 2)
        with self.repo.engine.connect() as conn:
            node = conn.execute(select(instance_intents).where(instance_intents.c.id == second["intent_id"])).mappings().one()
        self.worker("b", instance=node["provider_instance_id"])
        self.controller.advance_once("cold-approval")
        after = self.repo.get_job(self.scope, job["id"])
        self.assertEqual(after["status"], "queued")
        self.assertEqual(after["request"], job["request"])

    def test_racing_same_member_never_duplicates_rental_or_reservation(self):
        self.waiter()
        with ThreadPoolExecutor(2) as executor:
            values = list(executor.map(lambda _: self.create("a"), range(2)))
        self.assertEqual(len({v["intent_id"] for v in values}), 1)
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 200_000)

    def test_action_failure_rolls_back_membership_instance_and_budget_together(self):
        self.waiter()
        with patch.object(self.scaler, "_preparation_receipt", side_effect=RuntimeError("synthetic persistence failure")):
            with self.assertRaises(RuntimeError):
                self.create("a")
        for table in (capacity_pool_members, instance_intents, scaler_actions, scaler_receipts):
            self.assertEqual(self.rows(table), [])
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 0)
        self.assertEqual(self.provider.creates, [])

    def test_unknown_a_is_not_recreated_and_does_not_block_original_b(self):
        self.waiter()
        original = self.provider.create
        def unknown(tag, launch, **kwargs):
            original(tag, launch, **kwargs)
            raise TimeoutError("synthetic lost ACK")
        self.provider.create = unknown
        first = self.create("a")
        self.assertEqual(first["provider_state"], "unknown")
        self.provider.create = original
        second = self.create("b")
        self.assertEqual(second["state"], "creation_observed")
        again = self.create("a")
        self.assertEqual(again["state"], "member_already_bound")
        self.assertEqual(again["intent_id"], first["intent_id"])
        self.assertEqual(len(self.provider.creates), 2)
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 400_000)

    def test_uncertain_execution_alone_keeps_obligation_but_does_not_finance_unused_b(self):
        original = self.waiter()
        first = self.create("a")
        with self.repo.engine.connect() as connection:
            row = connection.execute(select(instance_intents).where(instance_intents.c.id == first["intent_id"])).mappings().one()
        control, worker = self.worker("a", instance=row["provider_instance_id"])
        self.controller.advance_once("cold-approval")
        claim = control.claim(worker.worker_id, "cold-pool")
        control.queue.begin_submission(claim.lease)
        control.queue.mark_submission_unknown(claim.lease)
        control.observe(worker.worker_id, original["id"])
        self.assertEqual(self.create("b"), {"state": "no_confirmed_pool_demand"})
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 200_000)
        self.waiter("new-independent-work")
        self.assertEqual(self.create("b")["state"], "creation_observed")
        self.assertEqual(self.repo.get_job(self.scope, original["id"])["current_attempt_id"], claim.lease.attempt_id)

    def test_pre_post_cancel_retains_proof_of_no_call_and_refunds_only_that_member(self):
        job = self.waiter()
        original = self.launcher.before_create
        def cancelled(*args):
            self.repo.request_cancel(self.scope, job["id"])
            return original(*args)
        with patch.object(self.launcher, "before_create", side_effect=cancelled):
            value = self.create("a")
        self.assertEqual(value["provider_state"], "not_created")
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.repo.get_budget("capacity-budget")["reserved_microusd"], 0)

    def test_foreign_unbound_live_intent_is_not_adopted_or_hidden(self):
        self.waiter()
        foreign = self.repo.reserve_instance_intent(self.billing_scope, "cold-pool", "foreign",
            physical_gpus=1, slots=1, reserved_cost_microusd=200_000, hard_deadline=self.now+1000,
            budget_account_ids=["capacity-budget"], dry_run=False, provider="test-only")
        with self.assertRaisesRegex(Conflict, "unknown_member"):
            self.create("a")
        self.assertEqual([r["id"] for r in self.rows(instance_intents)], [foreign["id"]])
        self.assertEqual(self.provider.creates, [])

    def test_member_ports_stay_fixed_after_out_of_order_creation_and_reconstruction(self):
        self.waiter()
        second, first = self.create("b"), self.create("a")
        cfg = SimpleNamespace(service_policy={**service.service_policy(), "mode": TWO_MEMBER_MODE,
            "member_ids": ["a", "b"]}, capacity_approval_id="cold-approval", port_start=19310,
            work_dir=Path(self.temp.name), fingerprint=lambda: "c"*64)
        save(cfg.work_dir/"cycle-state.json", {"config_hash": cfg.fingerprint(), "ports": {}})
        self.assertEqual(port_for_member(cfg, self.repo, second["intent_id"]), 19311)
        self.assertEqual(port_for_member(cfg, self.repo, first["intent_id"]), 19310)
        self.assertEqual(port_for_member(cfg, self.repo, second["intent_id"]), 19311)
        value = json.loads((cfg.work_dir/"cycle-state.json").read_text())
        value["ports"][first["intent_id"]] = 19311
        save(cfg.work_dir/"cycle-state.json", value)
        with self.assertRaisesRegex(ScalerError, "port_identity"):
            port_for_member(cfg, self.repo, first["intent_id"])


class MemberConfigurationTests(unittest.TestCase):
    def test_explicit_two_member_config_uses_one_real_selector_and_preserves_authority(self):
        root = Path(__file__).resolve().parents[1]
        base = production.configuration(root)
        authority = service.service_policy(end=base.hard_deadline)
        authority.update(mode=TWO_MEMBER_MODE, member_ids=["a", "b"])
        values = dict(vars(base))
        values.update(service_policy=authority, allowed_owners=authority["owner_ids"], max_cycles=None,
            scale_policy={**base.scale_policy, "idle_before_drain_s": 600},
            launches=base.launches[:1], manifests=base.manifests[:1])
        config = OnDemandConfig(**values)
        self.assertEqual(validate_service_config(config), authority)
        self.assertEqual(len(config.launches), 1)
        self.assertEqual(config.hard_deadline, base.hard_deadline)
        self.assertEqual(config.budget_account_ids, base.budget_account_ids)
        saved = json_config(config)
        self.assertEqual(OnDemandConfig(**saved).fingerprint(), config.fingerprint())
        with self.assertRaises(ScalerError):
            replace(config, launches=base.launches, manifests=base.manifests)
        with self.assertRaises(ValueError):
            replace(config, scale_policy={**config.scale_policy, "max_instances": 3})

    def test_member_shape_is_explicit_and_does_not_alter_legacy_policy(self):
        legacy = service.service_policy()
        self.assertEqual(validate_service_policy(legacy), legacy)
        for members in ([], ["a"], ["a", "a"], ["b", "a"], ["a", "b", "c"], ["a", "../b"]):
            with self.subTest(members=members), self.assertRaises(ValueError):
                validate_service_policy({**legacy, "mode": TWO_MEMBER_MODE, "member_ids": members})
