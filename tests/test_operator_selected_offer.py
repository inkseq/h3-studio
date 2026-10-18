"""Exact allocation consent through the real ledger/controller; fake cloud only."""
from types import SimpleNamespace
from sqlalchemy import insert, select, update

from studio_platform.auth import Principal
from studio_platform.capacity_market import publish_observation
from studio_platform.operator_capacity import (OperatorCapacity, OperatorRegistry, DeploymentBinding,
    OperatorError, operator_heartbeats, operator_commands, operator_nodes, command_binding_fingerprint)
from studio_platform.operator_controller import OperatorController
from studio_platform.runtime_catalog import PROFILE_IDS, get_profile, public_catalog
from studio_platform.scaler import LaunchSpec
from test_platform_repository import LedgerCase
from test_operator_capacity import OperatorProvider, FakeBoot
from test_capacity_candidates import offer


class SelectedProvider(OperatorProvider):
    def __init__(self):
        super().__init__()
        self.selected=[]

    def create_selected_for_intent(self,tag,launch,*,selected_offer,hard_deadline,intent_created_at):
        self.selected.append(dict(selected_offer))
        return self.create(tag,launch,hard_deadline=hard_deadline)


class SelectedOfferTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.actor=Principal("superdan","browser",auth_mode="password")
        self.row=offer()
        profile=get_profile(PROFILE_IDS[0])
        self.binding=DeploymentBinding(binding_id="selected",runtime_profile_id=PROFILE_IDS[0],
            gpu_type="RTX 5090",gpu_count=1,execution_slots=1,pool="test",configuration_id="selected",
            model_id=profile["model_id"],recipe_ids=("h3-base-ref2va-v1",),engine_manifest_digest="a"*64,
            launch=LaunchSpec("lium","selected",profile["model_id"]),scope=self.scope,
            budget_account_ids=("owner-budget",),hourly_cost_microusd=1000000,
            reservation_per_node_microusd=2000000,expires_at=self.now+10000,max_ttl_seconds=7200,
            min_ttl_seconds=120,enabled=True,filters={"min_ram_gib":96,"min_disk_gib":128})
        self.registry=OperatorRegistry((self.binding,),catalog=public_catalog)
        self.service=OperatorCapacity(self.repo,SimpleNamespace(operator_capacity_owners=("superdan",)),self.registry)
        self.repo.configure_pool("test",max_instances=4,max_physical_gpus=4)
        self.service.update_policy(self.actor,{"expected_version":0,"enabled":True,"max_instances":4,
            "max_physical_gpus":4,"max_hourly_cost_microusd":4000000,"idle_shutdown_seconds":600,
            "max_ttl_seconds":7200})
        self.provider=SelectedProvider()
        self.controller=OperatorController(self.service,provider_factory=lambda binding:self.provider,
            boot_factory=lambda *args:FakeBoot(self,*args),enabled=True)
        self.chosen={"runtime_profile_id":PROFILE_IDS[0],"mode":"ref","provider":"lium",
            "gpu_type":"RTX 5090","gpu_count":1,"node_count":1,"ttl_seconds":3600,"offer_id":self.row["offer_id"]}
        self.publish()

    def publish(self,rows=None):
        publish_observation(self.repo,{"provider":"lium","observed_at":self.now,
            "status":"ok","offers":[self.row] if rows is None else rows})

    def preview(self):
        result=self.service.preview(self.actor,self.chosen)
        self.assertTrue(result["can_start"],result["blockers"])
        self.assertEqual(result["selected_offer"]["offer_id"],self.row["offer_id"])
        return result

    def test_exact_offer_round_trip_replay_and_worker_identity(self):
        preview=self.preview()
        result=self.service.start(self.actor,{"preview_id":preview["preview_id"]},"selected-once")
        self.controller.tick()
        self.assertEqual(len(self.provider.creates),1)
        self.assertEqual(self.provider.selected,[self.row])
        with self.repo.engine.connect() as connection:
            node=connection.execute(select(operator_nodes)).mappings().one()
            command=connection.execute(select(operator_commands)).mappings().one()
        self.assertEqual(node["payload"]["selection"]["offer_id"],self.row["offer_id"])
        self.assertEqual(command["payload"]["selected_offer"],self.row)
        self.assertNotEqual(command["payload"]["binding_hash"],self.binding.fingerprint)
        self.assertEqual(node["binding_hash"],self.binding.fingerprint)
        self.assertEqual(len(self.service.state(self.actor)["nodes"][0]["slots"]),1)
        self.now+=400
        replay=self.service.start(self.actor,{"preview_id":preview["preview_id"]},"selected-once")
        self.assertEqual(replay["operation"]["id"],result["operation"]["id"])
        self.assertEqual(len(self.provider.creates),1)

    def test_pending_command_is_fenced_against_old_controller_even_after_lease_change(self):
        preview=self.preview()
        self.service.start(self.actor,{"preview_id":preview["preview_id"]},"rolling-upgrade")
        with self.repo.engine.connect() as connection:
            command=connection.execute(select(operator_commands)).mappings().one()
        # This exact comparison is the previous controller's authorization gate.
        self.assertNotEqual(self.binding.fingerprint,command["payload"]["binding_hash"])
        self.assertFalse(self.repo.list_instance_intents())
        self.assertFalse(self.provider.creates)
        self.assertEqual(command_binding_fingerprint(self.binding,{k:v for k,v in self.chosen.items()
            if k!="offer_id"}),self.binding.fingerprint)
        self.controller.tick()
        self.assertEqual(len(self.provider.creates),1)

    def test_changed_quote_requires_new_consent_before_reservation(self):
        preview=self.preview()
        self.row={**self.row,"hourly_cost_microusd":800000,"price_per_gpu_hour_microusd":800000}
        self.publish()
        with self.assertRaisesRegex(OperatorError,"operator_offer_changed"):
            self.service.start(self.actor,{"preview_id":preview["preview_id"]},"changed")
        self.assertFalse(self.repo.list_instance_intents())
        self.assertFalse(self.provider.creates)

    def test_accepted_missing_offer_blocks_without_falling_back_to_another(self):
        preview=self.preview()
        self.service.start(self.actor,{"preview_id":preview["preview_id"]},"missing")
        self.publish([offer("other-executor")])
        self.controller.tick()
        self.assertFalse(self.provider.creates)
        self.assertFalse(self.repo.list_instance_intents())
        with self.repo.engine.connect() as connection:
            command=connection.execute(select(operator_commands)).mappings().one()
        self.assertEqual(command["reason_code"],"operator_offer_unavailable")

    def test_old_controller_cannot_admit_exact_offer_command(self):
        self.registry.inventory_required=True
        with self.repo.transaction() as connection:
            connection.execute(insert(operator_heartbeats).values(id="global",controller_id="old-controller",
                observed_at=self.now,state="running"))
        preview=self.service.preview(self.actor,self.chosen)
        self.assertIn({"code":"operator_exact_offer_controller_unavailable"},preview["blockers"])
        with self.repo.transaction() as connection:
            connection.execute(update(operator_heartbeats).values(controller_id=self.controller.leader_id))
        self.preview()
        self.now+=31
        blocked=self.service.preview(self.actor,self.chosen)
        self.assertIn({"code":"operator_exact_offer_controller_unavailable"},blocked["blockers"])

    def test_stale_market_cannot_be_confirmed(self):
        preview=self.preview()
        # Stock may predate the preview, even while the preview itself is fresh.
        self.now+=110
        preview=self.preview()
        self.now+=11
        with self.assertRaisesRegex(OperatorError,"operator_offer_observation_unavailable"):
            self.service.start(self.actor,{"preview_id":preview["preview_id"]},"stale")
        self.assertFalse(self.provider.creates)

