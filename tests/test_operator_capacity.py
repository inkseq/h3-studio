"""Isolated database + injected fake provider; no cloud/model/API requests."""
from dataclasses import asdict, replace
import json
import io
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update,insert

from studio_platform.auth import Principal
from studio_platform.control import WorkerControl, WorkerSpec
from studio_platform.operator_capacity import (DeploymentBinding, OperatorCapacity, OperatorError, OperatorRegistry,
    operator_commands, operator_nodes, selection,operator_inventory,operator_heartbeats, public_bootstrap)
from studio_platform.operator_controller import OperatorController, main, provider_lifetime_current
from studio_platform.operator_routes import register_routes
from studio_platform.repository import instance_intents, jobs, registered_workers, scaler_actions, scaler_receipts
from studio_platform.scaler import LaunchSpec, ProviderFact
from test_platform_repository import LedgerCase
from test_platform_scaler import FakeProvider


class OperatorProvider(FakeProvider):
    provider_id="lium"
    def lifetime(self,tag,instance_id,*,local_created_at,maximum_hours):
        return {"instance_id":instance_id,"safe_deadline":local_created_at+maximum_hours*3600}


class FakeBoot:
    def __init__(self,case,binding,intent,chosen):
        self.case,self.binding,self.intent=case,binding,intent
        self.ticks=[]
        self.drained=False
        self.cancelled=False
        self.closed=False
    def request_drain(self): self.drained=True
    def cancel_preparation(self): self.cancelled=True
    def close(self): self.closed=True
    def set_start_guard(self,guard): self.guard=guard
    def shutdown_status(self):
        return {"ownership_known":True,"children_done":self.drained}
    def release_after_drain(self):
        if not self.drained: raise ValueError("not_drained")
        self.closed=True
    def tick(self,intent_id,*,stopping=False):
        self.ticks.append((intent_id,stopping))
        if stopping: return "draining"
        control=WorkerControl(self.case.repo)
        for ordinal in range(self.binding.execution_slots):
            spec=WorkerSpec("worker-"+intent_id+"-"+str(ordinal),self.binding.pool,"lium",self.intent["provider_instance_id"],
                tuple("GPU-"+str(i) for i in range(self.binding.gpu_count)) if self.binding.execution_slots==1 else ("GPU-"+str(ordinal),),
                self.binding.recipe_ids,self.binding.model_id,self.binding.configuration_id,
                backend="wangp-worker",engine_manifest_digest=self.binding.engine_manifest_digest)
            worker=control.register(spec)
            if not worker["current_job_id"]: control.mark_ready(spec.worker_id,upstream_idle_confirmed=True)
        return "ready"


class OperatorCase(LedgerCase):
    def setUp(self):
        super().setUp()
        self.actor=Principal("superdan","browser",auth_mode="password")
        self.settings=SimpleNamespace(operator_capacity_owners=("superdan",))
        self.binding=DeploymentBinding(binding_id="test-bf16",runtime_profile_id="h3-test-bf16",
            gpu_type="NVIDIA RTX PRO 6000 Blackwell",gpu_count=1,pool="operator-test",
            configuration_id="test-config",model_id="test-h3",recipe_ids=("h3-base-fl2va-v1",),
            engine_manifest_digest="a"*64,launch=LaunchSpec("lium","test-config","test-h3"),scope=self.scope,
            budget_account_ids=("owner-budget",),hourly_cost_microusd=360_000,
            reservation_per_node_microusd=1_000_000,expires_at=10_000,enabled=True,
            boot={"source_dir":"/private/test-only"})
        self.registry=OperatorRegistry([self.binding])
        self.service=OperatorCapacity(self.repo,self.settings,self.registry)
        self.repo.configure_pool("operator-test",max_instances=10,max_physical_gpus=10)
        self.provider=OperatorProvider()
        self.controller=OperatorController(self.service,provider_factory=lambda binding:self.provider,
            boot_factory=lambda *args:FakeBoot(self,*args),enabled=True,leader_id="test-leader")
        self.policy={"expected_version":0,"enabled":True,"max_instances":4,"max_physical_gpus":4,
            "max_hourly_cost_microusd":2_000_000,"idle_shutdown_seconds":600,"max_ttl_seconds":3600}
        self.service.update_policy(self.actor,self.policy)
        self.chosen={"runtime_profile_id":self.binding.runtime_profile_id,"gpu_type":self.binding.gpu_type,
            "mode":"fl","node_count":1,"gpu_count":1,"ttl_seconds":3600}

    def create(self,*,chosen=None,key="start-one"):
        preview=self.service.preview(self.actor,chosen or self.chosen)
        self.assertTrue(preview["can_start"],preview["blockers"])
        return self.service.start(self.actor,{"preview_id":preview["preview_id"]},key)["operation"]


class OperatorTests(OperatorCase):
    def test_cookie_operator_required_and_machine_key_never_grants_rental(self):
        for actor,code in ((None,"operator_login_required"),(Principal("supervan","browser"),"operator_forbidden"),
            (Principal("superdan","pat",machine=True,all_projects=True),"operator_forbidden")):
            for function in (lambda:self.service.state(actor),lambda:self.service.preview(actor,self.chosen),
                lambda:self.service.update_policy(actor,self.policy)):
                with self.assertRaisesRegex(OperatorError,code): function()
        self.assertTrue(self.service.state(self.actor)["operator"]["permissions"]["start"])

    def test_explicit_second_operator_can_manage_capacity_but_keys_cannot(self):
        self.settings.operator_capacity_owners=("superdan","supervan")
        other=Principal("supervan","browser",auth_mode="password")
        preview=self.service.preview(other,self.chosen)
        self.assertTrue(preview["can_start"])
        operation=self.service.start(other,{"preview_id":preview["preview_id"]},"supervan-start")["operation"]
        self.assertEqual(operation["kind"],"start")
        self.assertTrue(self.service.state(other)["operator"]["permissions"]["stop"])
        for actor in (Principal("outsider","browser"),
                      Principal("supervan","pat",machine=True,all_projects=True),
                      Principal("superdan","pat",machine=True,all_projects=True)):
            with self.assertRaisesRegex(OperatorError,"operator_forbidden"):
                self.service.preview(actor,self.chosen)

    def test_production_inventory_gates_preview_and_new_start_but_not_replay(self):
        self.registry.inventory_required=True
        blocked=self.service.preview(self.actor,self.chosen)
        self.assertFalse(blocked["can_start"])
        self.assertIn({"code":"operator_inventory_stale"},blocked["blockers"])
        with self.repo.transaction() as connection:
            connection.execute(insert(operator_heartbeats).values(id="global",controller_id="ctl",observed_at=self.now,state="running"))
            connection.execute(insert(operator_inventory).values(binding_id=self.binding.binding_id,
                binding_hash=self.binding.fingerprint,controller_id="ctl",observed_at=self.now,status="available"))
        preview=self.service.preview(self.actor,self.chosen)
        self.assertTrue(preview["can_start"])
        self.now+=31
        with self.assertRaisesRegex(OperatorError,"operator_inventory_stale"):
            self.service.start(self.actor,{"preview_id":preview["preview_id"]},"inventory-once")
        with self.repo.transaction() as connection:
            connection.execute(update(operator_heartbeats).values(observed_at=self.now))
        result=self.service.start(self.actor,{"preview_id":preview["preview_id"]},"inventory-once")
        self.now+=400
        self.assertEqual(self.service.start(self.actor,{"preview_id":preview["preview_id"]},"inventory-once"),result)

    def test_runtime_start_guard_rejects_stop_disabled_policy_and_expired_lease(self):
        self.create();self.controller.tick()
        boot=next(iter(self.controller.boots.values()))
        self.assertTrue(boot.guard())
        self.service.update_policy(self.actor,{**self.policy,"expected_version":1,"enabled":False})
        self.assertFalse(boot.guard())
        self.service.update_policy(self.actor,{**self.policy,"expected_version":2})
        self.assertTrue(boot.guard())
        state=self.service.state(self.actor);node=state["nodes"][0]
        self.service.node_command(self.actor,node["id"],{"expected_version":node["version"]},"stop-guard","stop")
        self.assertFalse(boot.guard())
        with self.repo.transaction() as connection:
            connection.execute(update(operator_nodes).values(desired_state="running"))
        self.assertTrue(boot.guard())
        self.now+=120
        self.assertFalse(boot.guard())

    def test_provider_minimum_ttl_is_visible_and_blocks_start(self):
        self.registry.bindings[self.binding.binding_id]=replace(self.binding,max_ttl_seconds=7200,min_ttl_seconds=3780)
        preview=self.service.preview(self.actor,self.chosen)
        self.assertEqual(preview["minimum_ttl_seconds"],3780)
        self.assertIn({"code":"operator_ttl_below_provider_minimum"},preview["blockers"])
        self.assertFalse(preview["can_start"])

    def test_pool_pause_blocks_legacy_preview_and_stale_preview_without_reservation(self):
        budget=self.repo.get_budget("owner-budget")
        for instances,gpus in ((0,1),(1,0),(0,0)):
            with self.subTest(instances=instances,gpus=gpus):
                self.repo.configure_pool(self.binding.pool,max_instances=1,max_physical_gpus=1)
                before=self.service.preview(self.actor,self.chosen)
                self.assertTrue(before["can_start"])
                self.repo.configure_pool(self.binding.pool,max_instances=instances,max_physical_gpus=gpus)
                paused=self.service.preview(self.actor,self.chosen)
                self.assertFalse(paused["can_start"])
                self.assertEqual(paused["blockers"],[{"code":"operator_pool_paused"}])
                with self.assertRaisesRegex(OperatorError,"operator_pool_paused"):
                    self.service.start(self.actor,{"preview_id":before["preview_id"]},"paused-start")
        self.assertEqual(self.repo.get_budget("owner-budget"),budget)
        self.assertEqual(self.repo.list_instance_intents(),[])
        self.assertEqual(self.provider.creates,[])
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.scalar(select(func.count()).select_from(operator_commands)),0)

    def test_pool_pause_preserves_existing_start_idempotency(self):
        before=self.service.preview(self.actor,self.chosen)
        body={"preview_id":before["preview_id"]}
        original=self.service.start(self.actor,body,"before-pause")
        self.repo.configure_pool(self.binding.pool,max_instances=0,max_physical_gpus=0)
        self.assertEqual(self.service.start(self.actor,body,"before-pause"),original)
        self.assertFalse(self.service.preview(self.actor,self.chosen)["can_start"])
        self.assertEqual(self.repo.list_instance_intents(),[])

    def test_provider_termination_window_shortens_before_boot_without_resetting_money(self):
        self.registry.bindings[self.binding.binding_id]=replace(self.binding,max_ttl_seconds=7200)
        self.service.update_policy(self.actor,{**self.policy,"expected_version":1,"max_ttl_seconds":7200})
        self.provider.lifetime=lambda tag,instance_id,**kw:{"instance_id":instance_id,
            "safe_deadline":kw["local_created_at"]+3600-600}
        operation=self.create(chosen={**self.chosen,"ttl_seconds":7200})
        original_boot=self.controller.boot_factory
        def check_before_boot(binding,intent,chosen):
            self.assertEqual(intent["hard_deadline"],self.now+3000)
            with self.repo.engine.connect() as connection:
                node=connection.execute(select(operator_nodes).where(operator_nodes.c.intent_id==intent["id"])).mappings().one()
            self.assertTrue(provider_lifetime_current(node["payload"],intent,self.now))
            return original_boot(binding,intent,chosen)
        self.controller.boot_factory=check_before_boot
        self.controller.tick()
        intent=self.repo.list_instance_intents()[0]
        budget=self.repo.get_budget("owner-budget")
        self.assertEqual(intent["hard_deadline"],4000)
        with self.repo.engine.connect() as connection:
            command=connection.execute(select(operator_commands).where(operator_commands.c.id==operation["id"])).mappings().one()
        self.assertEqual(command["payload"]["hard_deadline"],8200)
        # A later, longer supplier window never renews a retained node.
        self.provider.lifetime=lambda tag,instance_id,**kw:{"instance_id":instance_id,"safe_deadline":7000}
        self.controller.tick()
        self.assertEqual(self.repo.list_instance_intents()[0]["hard_deadline"],4000)
        self.assertEqual(self.repo.get_budget("owner-budget"),budget)
        node=self.service.state(self.actor)["nodes"][0]
        self.assertEqual(node["provider_safe_deadline"],4000)
        self.assertEqual(node["provider_lifetime_state"],"verified")

    def test_unknown_or_wrong_lifetime_cannot_publish_workers(self):
        self.provider.lifetime=lambda *args,**kw:{"instance_id":"wrong","safe_deadline":4000}
        self.create();self.controller.tick()
        self.assertFalse(self.controller.boots)
        node=self.service.state(self.actor)["nodes"][0]
        self.assertEqual(node["runtime_state"],"provider_lifetime_unverified")
        self.assertEqual(node["slots"],[])
        self.assertEqual(node["hard_deadline"],4600)
        self.assertEqual(len(self.provider.creates),1)
        def unknown(*args,**kw): raise TimeoutError("not public")
        self.provider.lifetime=unknown
        self.controller.tick()
        self.assertEqual(len(self.provider.creates),1)
        self.assertFalse(self.controller.boots)

    def test_lifetime_loss_and_staleness_close_late_admission(self):
        self.create();self.controller.tick()
        boot=next(iter(self.controller.boots.values()))
        self.assertTrue(boot.guard())
        self.now+=31
        self.assertFalse(boot.guard())
        self.controller.tick()
        self.assertTrue(boot.guard())
        self.provider.lifetime=lambda *args,**kw:None
        self.controller.tick()
        self.assertFalse(boot.guard())
        self.assertEqual(self.service.state(self.actor)["nodes"][0]["provider_lifetime_state"],"unverified")

    def test_lifetime_nan_expired_or_out_of_bound_are_unverified(self):
        self.create()
        for value in (float("nan"),True,self.now,self.now+20000):
            self.provider.lifetime=lambda tag,instance_id,**kw:{"instance_id":instance_id,"safe_deadline":value}
            self.controller.tick()
            self.assertFalse(self.controller.boots)
        self.assertEqual(len(self.provider.creates),1)

    def test_lifetime_response_after_fence_expiry_cannot_shorten_or_start(self):
        self.create()
        def expired_lease(tag,instance_id,**kw):
            self.now+=121
            return {"instance_id":instance_id,"safe_deadline":4000}
        self.provider.lifetime=expired_lease
        self.controller.tick()
        self.assertFalse(self.controller.boots)
        self.assertEqual(self.repo.list_instance_intents()[0]["hard_deadline"],4600)
        with self.repo.engine.connect() as connection:
            row=connection.execute(select(operator_nodes)).mappings().one()
        self.assertNotIn("lifetime",row["payload"])

    def test_shutdown_stops_new_rents_drains_owned_nodes_and_keeps_budget_and_deadline(self):
        self.create();self.controller.tick()
        first=self.repo.list_instance_intents()[0]
        budget=self.repo.get_budget("owner-budget")
        self.create(key="pending-start")
        self.controller.request_shutdown()
        self.assertEqual(self.controller.tick()["state"],"draining")
        self.assertEqual(len(self.provider.creates),1)
        node=self.service.state(self.actor)["nodes"][0]
        self.assertEqual(node["desired_state"],"drained")
        self.assertEqual(self.repo.get_budget("owner-budget"),budget)
        self.assertEqual(self.repo.list_instance_intents()[0]["hard_deadline"],first["hard_deadline"])
        self.assertEqual(self.provider.destroys,[])
        status=self.controller.shutdown_status()
        self.assertEqual(status["state"],"shutdown_complete")
        self.assertFalse(status["cloud_removal_confirmed"])
        self.assertFalse(status["billing_settled"])
        self.assertTrue(next(iter(self.controller.boots.values())).closed)

    def test_shutdown_retains_tunnels_for_active_collection_or_unknown_ownership(self):
        self.create();self.controller.tick()
        boot=next(iter(self.controller.boots.values()))
        self.controller.request_shutdown();self.controller.tick()
        for proof,code in (({"ownership_known":True,"children_done":False},"operator_collection_still_running"),
                ({"ownership_known":False,"children_done":False},"operator_child_ownership_unconfirmed")):
            boot.shutdown_status=lambda:proof
            result=self.controller.shutdown_status()
            self.assertEqual(result["state"],"shutdown_waiting")
            self.assertEqual(result["pending"][0]["code"],code)
            self.assertFalse(boot.closed)
        boot.shutdown_status=lambda:{"ownership_known":True,"children_done":True}
        self.assertEqual(self.controller.shutdown_status()["state"],"shutdown_complete")

    def test_sigterm_mid_allocation_does_not_rent_second_node_or_boot_new_runtime(self):
        self.create(chosen={**self.chosen,"node_count":2})
        handlers={}
        self.provider.on_create=lambda _:handlers[signal.SIGTERM](signal.SIGTERM,None)
        def install(signum,handler): handlers[signum]=handler
        with patch.dict(sys.modules,{"operator_test_factory":SimpleNamespace(factory=lambda _:self.controller)}), \
             patch("studio_platform.operator_controller.signal.signal",side_effect=install), \
             patch("studio_platform.operator_controller.time.sleep",side_effect=AssertionError("must finish")), \
             patch("sys.stdout",new_callable=io.StringIO) as output:
            result=main(["--factory","operator_test_factory:factory","--config","unused","--enabled"])
        self.assertEqual(result,0)
        self.assertEqual(len(self.provider.creates),1)
        self.assertFalse(self.controller.boots)
        self.assertEqual(self.service.state(self.actor)["nodes"][0]["desired_state"],"drained")
        self.assertIn('"cloud_removal_confirmed": false',output.getvalue())

    def test_cli_grace_timeout_reports_attention_without_closing_live_collectors(self):
        self.create();self.controller.tick()
        boot=next(iter(self.controller.boots.values()))
        proof={"ownership_known":False,"children_done":False}
        boot.shutdown_status=lambda:dict(proof)
        self.controller.request_shutdown()
        self.controller.shutdown_started_at=0
        def finish_after_report(_):
            self.assertFalse(boot.closed)
            proof.update(ownership_known=True,children_done=True)
        with patch.dict(sys.modules,{"operator_test_factory":SimpleNamespace(factory=lambda _:self.controller)}), \
             patch("studio_platform.operator_controller.signal.signal"), \
             patch("studio_platform.operator_controller.time.sleep",side_effect=finish_after_report), \
             patch("studio_platform.operator_controller.time.monotonic",return_value=31), \
             patch("sys.stdout",new_callable=io.StringIO) as output:
            result=main(["--factory","operator_test_factory:factory","--config","unused","--enabled","--shutdown-grace-seconds","30"])
        self.assertEqual(result,0)
        self.assertIn('"shutdown_attention_required"',output.getvalue())
        self.assertTrue(boot.closed)

    def test_late_guard_rejects_changed_binding_and_original_deadline(self):
        self.create();self.controller.tick()
        boot=next(iter(self.controller.boots.values()))
        self.assertTrue(boot.guard())
        self.registry.bindings[self.binding.binding_id]=replace(self.binding,enabled=False)
        self.assertFalse(boot.guard())
        self.registry.bindings[self.binding.binding_id]=replace(self.binding,configuration_id="different",
            launch=replace(self.binding.launch,configuration_id="different"))
        self.assertFalse(boot.guard())
        self.registry.bindings[self.binding.binding_id]=self.binding
        with self.repo.transaction() as connection:
            connection.execute(update(instance_intents).values(hard_deadline=self.now))
        self.assertFalse(boot.guard())

    def test_preview_only_and_no_fake_generation_jobs(self):
        preview=self.service.preview(self.actor,self.chosen)
        self.assertTrue(preview["can_start"])
        self.assertEqual(preview["estimated_hourly_cost_microusd"],360_000)
        self.assertEqual(self.provider.creates,[])
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.execute(select(func.count()).select_from(instance_intents)).scalar_one(),0)
            self.assertEqual(connection.execute(select(func.count()).select_from(jobs)).scalar_one(),0)
        value=json.dumps(preview)+json.dumps(self.service.state(self.actor))
        self.assertNotIn("/private",value)
        self.assertNotIn("launch",value)

    def test_start_idempotency_survives_expiry_policy_change_and_forbids_new_key(self):
        preview=self.service.preview(self.actor,self.chosen)
        body={"preview_id":preview["preview_id"]}
        first=self.service.start(self.actor,body,"stable")
        self.now+=180
        self.service.update_policy(self.actor,{**self.policy,"expected_version":1,"enabled":False})
        self.assertEqual(first,self.service.start(self.actor,body,"stable"))
        with self.assertRaisesRegex(OperatorError,"operator_idempotency_conflict"):
            self.service.start(self.actor,{"preview_id":"different"},"stable")
        with self.assertRaisesRegex(OperatorError,"operator_preview_already_confirmed"):
            self.service.start(self.actor,body,"different-key")

    def test_policy_compare_and_set_does_not_modify_budget_or_existing_deadline(self):
        self.create()
        self.controller.tick()
        original=self.repo.list_instance_intents()[0]
        budget=self.repo.get_budget("owner-budget")
        self.service.update_policy(self.actor,{**self.policy,"expected_version":1,"max_instances":1,"max_ttl_seconds":120})
        with self.assertRaisesRegex(OperatorError,"operator_policy_version_conflict"):
            self.service.update_policy(self.actor,{**self.policy,"expected_version":1})
        self.assertEqual(self.repo.get_budget("owner-budget"),budget)
        self.assertEqual(self.repo.list_instance_intents()[0]["hard_deadline"],original["hard_deadline"])

    def test_pending_commands_count_towards_global_gpu_and_hourly_caps(self):
        self.service.update_policy(self.actor,{**self.policy,"expected_version":1,"max_instances":2,"max_physical_gpus":2})
        self.create(chosen={**self.chosen,"node_count":2})
        blocked=self.service.preview(self.actor,self.chosen)
        self.assertFalse(blocked["can_start"])
        self.assertIn({"code":"operator_gpu_limit"},blocked["blockers"])
        self.controller.tick()
        self.assertEqual(len(self.provider.creates),2)
        state=self.service.state(self.actor)
        self.assertEqual(state["summary"]["gpus_allocated"],2)
        self.assertEqual(state["summary"]["hourly_cost_microusd"],720_000)

    def test_concurrent_start_confirmation_serializes_capacity(self):
        self.service.update_policy(self.actor,{**self.policy,"expected_version":1,"max_instances":1,"max_physical_gpus":1})
        previews=[self.service.preview(self.actor,self.chosen) for _ in range(8)]
        def start(index):
            try:
                return self.service.start(self.actor,{"preview_id":previews[index]["preview_id"]},"k-"+str(index))
            except OperatorError as error: return error.code
        results=self.parallel(start)
        self.assertEqual(sum(isinstance(value,dict) for value in results),1)
        self.controller.tick()
        self.assertEqual(len(self.provider.creates),1)

    def test_same_start_key_concurrently_creates_only_one_command(self):
        preview=self.service.preview(self.actor,self.chosen)
        values=self.parallel(lambda _:self.service.start(self.actor,{"preview_id":preview["preview_id"]},"same"))
        self.assertEqual(len({value["operation"]["id"] for value in values}),1)
        self.controller.tick()
        self.assertEqual(len(self.provider.creates),1)

    def test_controller_disabled_does_not_call_any_factory(self):
        self.create()
        controller=OperatorController(self.service,provider_factory=lambda _:self.fail("provider factory called"))
        self.assertEqual(controller.tick()["state"],"disabled")
        self.assertEqual(self.repo.list_instance_intents(),[])

    def test_unqualified_binding_or_missing_boot_hook_cannot_rent(self):
        self.registry.bindings[self.binding.binding_id]=replace(self.binding,enabled=False)
        preview=self.service.preview(self.actor,self.chosen)
        self.assertIn({"code":"operator_deployment_not_qualified"},preview["blockers"])
        self.registry.bindings[self.binding.binding_id]=self.binding
        operation=self.create()
        self.controller.boot_factory=None
        self.controller.tick()
        self.assertEqual(self.provider.creates,[])
        state=self.service.state(self.actor)["operations"][0]
        self.assertEqual((state["id"],state["state"],state["reason_code"]),
            (operation["id"],"blocked","operator_bootstrap_unconfigured"))

    def test_complete_start_requires_real_exact_worker_registration(self):
        self.create()
        report=self.controller.tick()
        self.assertEqual(report["errors"],0)
        self.assertEqual(self.service.state(self.actor)["operations"][0]["state"],"completed")
        self.assertEqual(self.repo.list_instance_intents()[0]["state"],"ready")
        self.controller.tick()
        self.assertEqual(len(self.provider.creates),1)
        self.assertEqual(self.service.state(self.actor)["summary"]["slots_ready"],1)

    def test_lying_boot_ready_is_not_model_readiness(self):
        self.create()
        self.controller.boot_factory=lambda *args:SimpleNamespace(tick=lambda *args,**kwargs:"ready")
        self.controller.tick()
        state=self.service.state(self.actor)
        self.assertEqual(state["nodes"][0]["runtime_state"],"awaiting_qualified_workers")
        self.assertEqual(state["operations"][0]["state"],"waiting")
        self.assertEqual(state["summary"]["slots_ready"],0)

    def test_unknown_boot_cause_survives_api_read_without_new_rental_or_settlement(self):
        self.create()
        report={"state":"blocked","reason_code":"bootstrap_reconciliation_required","slots":[{
            "state":"bootstrap_reconciliation_required","phase":"runtime_start_unknown",
            "failure_phase":"runtime_manifest","error_code":"wangp_configuration_permissions",
            "error_type":"ValueError","log":"SECRET"}]}
        self.controller.boot_factory=lambda *args:SimpleNamespace(tick=lambda *args,**kwargs:report)
        self.controller.tick()
        original=self.repo.list_instance_intents()[0]
        budget=self.repo.get_budget("owner-budget")
        for _ in range(3):
            self.now+=5
            self.controller.tick()
        state=OperatorCapacity(self.repo,self.settings,self.registry).state(self.actor)
        node=state['nodes'][0]
        self.assertEqual(node['runtime_state'],'blocked')
        self.assertEqual(node['reason_code'],'bootstrap_reconciliation_required')
        self.assertEqual(node['bootstrap']['observed_at'],self.now)
        self.assertEqual(node['bootstrap']['slots'][0]['error_code'],'wangp_configuration_permissions')
        self.assertEqual(node['bootstrap']['slots'][0]['failure_phase'],'runtime_manifest')
        self.assertEqual(state['operations'][0]['state'],'blocked')
        self.assertEqual(state['operations'][0]['reason_code'],'bootstrap_reconciliation_required')
        self.assertEqual(len(self.provider.creates),1)
        self.assertEqual(self.provider.destroys,[])
        current=self.repo.list_instance_intents()[0]
        self.assertEqual((current['id'],current['hard_deadline'],current['state']),
            (original['id'],original['hard_deadline'],'starting'))
        self.assertEqual(self.repo.get_budget('owner-budget'),budget)
        self.assertNotIn('SECRET',json.dumps(state))

    def test_public_bootstrap_masks_malformed_private_fields(self):
        value=public_bootstrap({'state':{'secret':'SECRET'},'reason_code':['SECRET'],
            'observed_at':float('nan'),'log':'SECRET','slots':[{'state':{'secret':'SECRET'},
                'phase':['SECRET'],'failure_phase':'SECRET','error_code':'https://private.invalid/?token=SECRET',
                'error_type':{'secret':'SECRET'},'message':'SECRET'}]})
        self.assertEqual(value['state'],'blocked')
        self.assertEqual(value['slots'][0]['error_code'],'UnclassifiedBootstrapFailure')
        self.assertNotIn('observed_at',value)
        self.assertNotIn('SECRET',json.dumps(value))

    def test_live_registration_does_not_erase_boot_reconciliation_hold(self):
        self.create()
        factory=self.controller.boot_factory
        def boot(*args):
            value=factory(*args)
            original_tick=value.tick
            def tick(*a,**kw):
                original_tick(*a,**kw)
                return {'state':'blocked','reason_code':'bootstrap_reconciliation_required','slots':[]}
            value.tick=tick
            return value
        self.controller.boot_factory=boot
        self.controller.tick()
        self.assertEqual(self.service.state(self.actor)['nodes'][0]['runtime_state'],'blocked')
        self.assertEqual(self.repo.list_instance_intents()[0]['state'],'starting')

    def test_response_lost_is_reconciled_without_duplicate_create(self):
        self.provider.create_uncertain=True
        self.create()
        self.controller.tick()
        self.controller.tick()
        self.assertEqual(len(self.provider.creates),1)
        self.assertEqual(self.service.state(self.actor)["operations"][0]["state"],"completed")

    def test_crash_before_provider_call_never_resubmits_after_restart(self):
        class Crash(BaseException): pass
        self.create()
        coordinator=self.controller._coordinator(self.binding)
        with patch.object(coordinator,"_call",side_effect=Crash),self.assertRaises(Crash):
            self.controller.tick()
        self.assertEqual(self.provider.creates,[])
        self.assertEqual(len(self.repo.list_instance_intents()),1)
        self.now+=61
        replacement=OperatorController(self.service,provider_factory=lambda _:self.provider,
            boot_factory=lambda *args:FakeBoot(self,*args),enabled=True,leader_id="replacement")
        replacement.tick()
        self.assertEqual(self.provider.creates,[])
        self.assertEqual(self.repo.list_instance_intents()[0]["state"],"creation_unknown")
        self.assertEqual(self.service.state(self.actor)["operations"][0]["state"],"unknown")
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"],1_000_000)

    def test_budget_failure_never_creates_provider_instance(self):
        self.repo.configure_budget("owner-budget",tenant_id=self.scope.tenant_id,owner_id=self.scope.owner_id,limit_microusd=1)
        self.create()
        self.controller.tick()
        self.assertEqual(self.provider.creates,[])
        self.assertEqual(self.repo.list_instance_intents(),[])
        self.assertEqual(self.service.state(self.actor)["operations"][0]["state"],"blocked")

    def test_stop_requires_fresh_idle_proof_and_preserves_billing_reservation(self):
        self.create()
        self.controller.tick()
        node=self.service.state(self.actor)["nodes"][0]
        command=self.service.node_command(self.actor,node["id"],{"expected_version":node["version"]},"stop-1","stop")
        self.controller.tick()
        self.assertEqual(self.provider.destroys,[])
        intent=self.repo.list_instance_intents()[0]
        self.provider.facts[intent["id"]]=ProviderFact("running",intent["provider_instance_id"],idle_confirmed=True,idle_since=self.now)
        self.controller.tick()
        self.assertEqual(len(self.provider.destroys),1)
        self.assertEqual(self.repo.list_instance_intents()[0]["state"],"destroyed")
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"],1_000_000)
        self.controller.tick()
        self.assertEqual(len(self.provider.destroys),1)
        replay=self.service.node_command(self.actor,node["id"],{"expected_version":node["version"]},"stop-1","stop")
        self.assertEqual(replay["operation"]["id"],command["operation"]["id"])
        self.assertEqual(replay["operation"]["node_ids"],[node["id"]])

    def test_unknown_instance_stop_never_treats_absence_as_not_created(self):
        class Crash(BaseException): pass
        self.create()
        with patch.object(self.controller._coordinator(self.binding),"_call",side_effect=Crash),self.assertRaises(Crash):
            self.controller.tick()
        self.controller.tick()
        node=self.service.state(self.actor)["nodes"][0]
        self.service.node_command(self.actor,node["id"],{"expected_version":node["version"]},"stop-unknown","stop")
        self.now+=5000
        self.controller.tick()
        self.assertEqual(self.provider.destroys,[])
        self.assertEqual(self.provider.creates,[])
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"],1_000_000)

    def test_pending_provider_removal_survives_restart_without_preparation_or_duplicate_delete(self):
        self.create();self.controller.tick()
        node=self.service.state(self.actor)["nodes"][0]
        intent=self.repo.list_instance_intents()[0]
        budget=self.repo.get_budget("owner-budget")
        self.service.node_command(self.actor,node["id"],{"expected_version":node["version"]},"stop-pending","stop")
        self.provider.facts[intent["id"]]=ProviderFact("running",intent["provider_instance_id"],
            idle_confirmed=True,idle_since=self.now)
        def pending(tag,instance_id):
            self.provider.destroys.append((tag,instance_id))
            self.provider.facts[tag]=ProviderFact("unknown",instance_id)
            return self.provider.facts[tag]
        self.provider.destroy=pending
        self.controller.tick()
        self.assertEqual(self.repo.list_instance_intents()[0]["state"],"destroying")
        self.assertEqual(self.service.state(self.actor)["nodes"][0]["runtime_state"],"removal_pending")
        self.assertEqual(self.repo.get_budget("owner-budget"),budget)
        self.assertEqual(len(self.provider.destroys),1)

        self.now+=121
        replacement=OperatorController(self.service,provider_factory=lambda _:self.provider,
            boot_factory=lambda *args:self.fail("Removal must never bootstrap a worker"),
            enabled=True,leader_id="replacement")
        replacement.tick()
        current=self.service.state(self.actor)["nodes"][0]
        self.assertEqual((current["state"],current["runtime_state"]),("destroying","removal_pending"))
        self.assertEqual(self.repo.list_instance_intents()[0]["hard_deadline"],intent["hard_deadline"])
        self.assertEqual(self.repo.get_budget("owner-budget"),budget)
        self.assertEqual(len(self.provider.creates),1)
        self.assertEqual(len(self.provider.destroys),1)
        self.provider.facts[intent["id"]]=ProviderFact("destroyed",intent["provider_instance_id"])
        self.now+=60
        replacement.tick()
        current=self.service.state(self.actor)["nodes"][0]
        self.assertEqual((current["state"],current["runtime_state"]),("destroyed","destroyed"))
        self.assertEqual(self.repo.get_budget("owner-budget"),budget)

    def test_removal_feedback_times_out_without_mutating_ledger_or_calling_provider(self):
        self.create();self.controller.tick()
        node=self.service.state(self.actor)["nodes"][0]
        self.assertIsNone(node["removal_confirmation"])
        self.service.node_command(self.actor,node["id"],{"expected_version":node["version"]},"stop-feedback","stop")
        with self.repo.transaction() as connection:
            connection.execute(update(instance_intents).where(instance_intents.c.id==node["id"]).values(state="destroying"))
            connection.execute(update(scaler_actions).where(scaler_actions.c.intent_id==node["id"]).values(
                destroy_started_at=self.now,last_observed_at=self.now,last_observation={"state":"unknown","private":"do-not-expose"}))
            connection.execute(insert(scaler_receipts).values(id="feedback-claim",intent_id=node["id"],
                operation="removal_check_started",observed_at=self.now+60,facts={"state":"unknown"}))
        budget=self.repo.get_budget("owner-budget")
        calls=(len(self.provider.creates),len(self.provider.destroys))
        self.now+=299
        feedback=self.service.state(self.actor)["nodes"][0]["removal_confirmation"]
        self.assertEqual(feedback["state"],"pending")
        self.assertEqual(feedback["next_check_at"],feedback["requested_at"]+120)
        self.assertEqual(feedback["last_checked_at"],feedback["requested_at"])
        self.now+=1
        for _ in range(3):
            state=self.service.state(self.actor)
            self.assertEqual(state["nodes"][0]["removal_confirmation"]["state"],"overdue")
            self.assertNotIn("do-not-expose",json.dumps(state))
        self.assertEqual(self.repo.get_budget("owner-budget"),budget)
        self.assertEqual(calls,(len(self.provider.creates),len(self.provider.destroys)))
        self.assertEqual(self.repo.list_instance_intents()[0]["state"],"destroying")
        with self.repo.transaction() as connection:
            connection.execute(update(instance_intents).where(instance_intents.c.id==node["id"]).values(state="destroyed"))
            connection.execute(update(scaler_actions).where(scaler_actions.c.intent_id==node["id"]).values(
                last_observed_at=self.now,last_observation={"state":"destroyed"}))
        feedback=self.service.state(self.actor)["nodes"][0]["removal_confirmation"]
        self.assertEqual(feedback["state"],"confirmed")
        self.assertIsNone(feedback["next_check_at"])
        self.assertIsNone(feedback["reason_code"])

    def test_drain_stops_admission_but_does_not_force_destroy(self):
        self.create()
        self.controller.tick()
        node=self.service.state(self.actor)["nodes"][0]
        self.service.node_command(self.actor,node["id"],{"expected_version":node["version"]},"drain-1","drain")
        self.controller.tick()
        self.assertEqual(self.provider.destroys,[])
        state=self.service.state(self.actor)
        self.assertEqual(state["nodes"][0]["desired_state"],"drained")
        self.assertEqual(state["nodes"][0]["slots"][0]["state"],"draining")
        self.assertEqual(next(op for op in state["operations"] if op["kind"]=="drain")["state"],"completed")

    def test_stale_worker_prevents_safe_stop_even_provider_reports_idle(self):
        self.create()
        self.controller.tick()
        node=self.service.state(self.actor)["nodes"][0]
        self.service.node_command(self.actor,node["id"],{"expected_version":node["version"]},"stop-stale","stop")
        self.provider.facts[node["id"]]=ProviderFact("running",node["provider_instance_id"],idle_confirmed=True,idle_since=self.now)
        self.now+=180
        self.controller.tick()
        self.assertEqual(self.provider.destroys,[])

    def test_multiple_cards_can_be_one_node_with_two_execution_slots(self):
        binding=replace(self.binding,gpu_count=2,execution_slots=2,hourly_cost_microusd=720_000)
        self.registry.bindings[binding.binding_id]=binding
        self.create(chosen={**self.chosen,"gpu_count":2})
        self.controller.tick()
        state=self.service.state(self.actor)
        self.assertEqual(state["summary"]["nodes_active"],1)
        self.assertEqual(state["summary"]["gpus_allocated"],2)
        self.assertEqual(state["summary"]["slots_ready"],2)
        self.assertEqual(state["operations"][0]["state"],"completed")

    def test_unmanaged_existing_capacity_is_not_silently_assigned_zero_cost(self):
        self.repo.configure_pool("external",max_instances=1,max_physical_gpus=1)
        self.repo.reserve_instance_intent(self.scope,"external","legacy",physical_gpus=1,slots=1,
            reserved_cost_microusd=100_000,hard_deadline=5000,budget_account_ids=("owner-budget",),dry_run=False,provider="lium")
        preview=self.service.preview(self.actor,self.chosen)
        self.assertIn({"code":"operator_unpriced_existing_capacity"},preview["blockers"])
        self.assertIsNone(self.service.state(self.actor)["summary"]["hourly_cost_microusd"])

    def test_registry_json_file_retains_exact_binding_and_is_not_public(self):
        path=Path(self.temp.name)/"registry.json"
        path.write_text(json.dumps({"schema_version":1,"bindings":[asdict(self.binding)]}),encoding="utf-8")
        loaded=OperatorRegistry.from_file(path)
        self.assertEqual(loaded.get(self.binding.binding_id).fingerprint,self.binding.fingerprint)
        self.assertNotIn("/private",json.dumps(loaded.catalog()))
        path.write_text(json.dumps({"schema_version":1,"bindings":[{"surprise":"value"}]}),encoding="utf-8")
        with self.assertRaisesRegex(OperatorError,"operator_registry_file_invalid"): OperatorRegistry.from_file(path)

    def test_invalid_country_types_raise_safe_validation_not_typeerror(self):
        for country in ([{}],[[]],["us"],["US","US"]):
            with self.assertRaisesRegex(OperatorError,"operator_countries_invalid"):
                selection({**self.chosen,"filters":{"allowed_countries":country}})

    def test_cli_disabled_does_not_import_factory_or_open_config(self):
        with patch("studio_platform.operator_controller.importlib.import_module",side_effect=AssertionError("unsafe import")):
            self.assertEqual(main(["--factory","missing:factory","--config","missing.json"]),0)

    def test_http_routes_require_operator_and_expose_no_store(self):
        app=FastAPI()
        app.state.repository=self.repo
        app.state.settings=self.settings
        actor=[self.actor]
        @app.middleware("http")
        async def principal(request:Request,call_next):
            request.state.principal=actor[0]
            return await call_next(request)
        register_routes(app,service=self.service)
        with TestClient(app) as client:
            response=client.get("/v1/operator/capacity/state")
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers["cache-control"],"no-store")
            preview=client.post("/v1/operator/capacity/previews",json=self.chosen)
            started=client.post("/v1/operator/capacity/starts",json={"preview_id":preview.json()["preview_id"]},
                headers={"Idempotency-Key":"http-start"})
            self.assertEqual(started.status_code,202)
            actor[0]=Principal("superdan","pat",machine=True)
            for path in ("state","catalog","offers?runtime_profile_id=test&gpu_type=test&mode=fl"):
                self.assertEqual(client.get("/v1/operator/capacity/"+path).status_code,403)
            self.assertEqual(client.post("/v1/operator/capacity/previews",json=self.chosen).status_code,403)

    def test_pending_start_rejects_changed_policy_or_immutable_binding(self):
        self.create()
        self.service.update_policy(self.actor,{**self.policy,"expected_version":1,"max_instances":3})
        self.controller.tick()
        self.assertEqual(self.provider.creates,[])
        self.assertEqual(self.service.state(self.actor)["operations"][0]["reason_code"],"operator_policy_changed")
        self.create(key="fresh-start")
        self.registry.bindings[self.binding.binding_id]=replace(self.binding,configuration_id="changed",
            launch=replace(self.binding.launch,configuration_id="changed"))
        self.controller.tick()
        self.assertEqual(self.provider.creates,[])
        self.assertIn("operator_binding_changed",[op["reason_code"] for op in self.service.state(self.actor)["operations"]])

    def test_disabled_binding_preserves_existing_cleanup_authority(self):
        self.create()
        self.controller.tick()
        self.registry.bindings[self.binding.binding_id]=replace(self.binding,enabled=False)
        node=self.service.state(self.actor)["nodes"][0]
        self.service.node_command(self.actor,node["id"],{"expected_version":node["version"]},"disable-stop","stop")
        self.provider.facts[node["id"]]=ProviderFact("running",node["provider_instance_id"],idle_confirmed=True,idle_since=self.now)
        self.controller.tick()
        self.assertEqual(len(self.provider.destroys),1)
        self.assertEqual(len(self.provider.creates),1)

    def test_opposite_generation_modes_resolve_different_deployments(self):
        ref=replace(self.binding,binding_id="test-ref",recipe_ids=("h3-base-ref2va-v1",),
            configuration_id="test-ref-config",launch=replace(self.binding.launch,configuration_id="test-ref-config"))
        self.registry.bindings[ref.binding_id]=ref
        self.assertEqual(self.registry.resolve(selection(self.chosen)).binding_id,self.binding.binding_id)
        self.assertEqual(self.registry.resolve(selection({**self.chosen,"mode":"ref"})).binding_id,ref.binding_id)
        with self.assertRaisesRegex(OperatorError,"operator_selection_invalid"):
            selection({key:value for key,value in self.chosen.items() if key!="mode"})

    def test_second_controller_cannot_boot_or_create_while_first_holds_pool_fence(self):
        self.create()
        first=self.controller._coordinator(self.binding)
        self.assertIsNotNone(first.acquire(self.binding.pool,self.controller.leader_id))
        replacement=OperatorController(self.service,provider_factory=lambda _:self.provider,
            boot_factory=lambda *args:self.fail("another leader booted"),enabled=True,leader_id="competitor")
        replacement.tick()
        self.assertEqual(self.provider.creates,[])
        self.controller.tick()
        replacement.tick()
        self.assertEqual(len(self.provider.creates),1)

    def test_cross_pool_capacity_competition_keeps_one_global_ceiling(self):
        self.service.update_policy(self.actor,{**self.policy,"expected_version":1,"max_instances":1,"max_physical_gpus":1})
        self.create()
        self.repo.configure_pool("other-pool",max_instances=1,max_physical_gpus=1)
        self.repo.reserve_instance_intent(self.scope,"other-pool","outside",physical_gpus=1,slots=1,
            reserved_cost_microusd=100_000,hard_deadline=5000,budget_account_ids=("owner-budget",),dry_run=False,provider="lium")
        self.controller.tick()
        self.assertEqual(self.provider.creates,[])
        self.assertEqual(len(self.repo.list_instance_intents()),1)

    def test_busy_and_collecting_attempt_prevents_operator_stop(self):
        self.create()
        self.controller.tick()
        node=self.service.state(self.actor)["nodes"][0]
        worker_id=node["slots"][0]["id"]
        plan=self.repo.create_plan(self.scope,{"recipe_id":"h3-base-fl2va-v1","deployment_profile_id":self.binding.runtime_profile_id,"request":{"model":"test-h3"}},
            {"pool":self.binding.pool,"backend":"wangp-worker","configuration_id":self.binding.configuration_id,
             "engine_manifest_digest":self.binding.engine_manifest_digest,"enabled":True},expires_at=9000)
        job=self.repo.create_job(self.scope,plan["id"],"busy-test")
        control=WorkerControl(self.repo)
        claim=control.claim(worker_id,self.binding.pool,lease_seconds=3600)
        self.assertIsNotNone(claim)
        control.queue.begin_submission(claim.lease)
        control.queue.record_submitted(claim.lease,"private-runtime-task")
        control.queue.begin_collection(claim.lease)
        fresh=self.service.state(self.actor)["nodes"][0]
        self.service.node_command(self.actor,node["id"],{"expected_version":fresh["version"]},"busy-stop","stop")
        self.provider.facts[node["id"]]=ProviderFact("running",node["provider_instance_id"],idle_confirmed=True,idle_since=self.now)
        self.controller.tick()
        self.assertEqual(self.provider.destroys,[])
        self.assertEqual(self.repo.get_job(self.scope,job["id"])["status"],"collecting")

    def test_idle_600_uses_application_obligations_then_destroys_once(self):
        self.create()
        self.controller.tick()
        node=self.service.state(self.actor)["nodes"][0]
        self.provider.facts[node["id"]]=ProviderFact("running",node["provider_instance_id"],idle_confirmed=True,idle_since=0)
        self.controller.tick()
        self.now+=599
        control=WorkerControl(self.repo)
        control.mark_ready(node["slots"][0]["id"],upstream_idle_confirmed=True)
        self.controller.tick()
        self.assertEqual(self.provider.destroys,[])
        self.now+=1
        self.controller.tick()
        self.assertEqual(len(self.provider.destroys),1)

    def test_stale_snapshot_is_explicit_and_does_not_invent_telemetry(self):
        self.create()
        self.controller.tick()
        self.now+=121
        state=self.service.state(self.actor)
        self.assertTrue(state["controller"]["stale"])
        self.assertTrue(state["nodes"][0]["stale"])
        self.assertTrue(state["nodes"][0]["slots"][0]["stale"])
        self.assertEqual(state["summary"]["slots_ready"],0)

    def test_revoked_authority_after_reservation_proves_not_submitted(self):
        self.create()
        guard=self.controller._authorize_start
        calls=[]
        def revoked(connection,command,binding):
            calls.append(True)
            if len(calls)>1: raise OperatorError("operator_policy_changed")
            return guard(connection,command,binding)
        with patch.object(self.controller,"_authorize_start",side_effect=revoked):
            self.controller.tick()
        self.assertEqual(self.provider.creates,[])
        self.assertEqual(self.repo.list_instance_intents()[0]["state"],"destroyed")
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"],0)

    def test_mixed_failed_and_ready_allocations_are_partial_without_replacement(self):
        original=self.provider.create
        def one_failure(tag,launch,*,hard_deadline):
            if not self.provider.creates:
                self.provider.creates.append((tag,launch,hard_deadline))
                return ProviderFact("not_created",actual_cost_microusd=0,absence_confirmed=True)
            return original(tag,launch,hard_deadline=hard_deadline)
        self.provider.create=one_failure
        operation=self.create(chosen={**self.chosen,"node_count":2})
        self.controller.tick()
        state=self.service.state(self.actor)
        result=next(op for op in state["operations"] if op["id"]==operation["id"])
        self.assertEqual(result["state"],"partial")
        self.assertEqual(result["reason_code"],"operator_partial_capacity")
        self.assertEqual(len(result["node_ids"]),2)
        self.assertEqual(state["summary"]["slots_ready"],1)
        self.controller.tick()
        self.assertEqual(len(self.provider.creates),2)
        self.assertEqual(len(self.repo.list_instance_intents()),2)
        self.assertEqual(self.repo.get_budget("owner-budget")["reserved_microusd"],1_000_000)

    def test_controller_observation_failure_persists_degraded_heartbeat(self):
        self.create()
        self.controller.tick()
        with patch.object(self.controller,"_observe",side_effect=RuntimeError("private-url-must-not-leak")):
            outcome=self.controller.tick()
        self.assertEqual(outcome["state"],"degraded")
        state=self.service.state(self.actor)
        self.assertEqual(state["controller"]["state"],"degraded")
        self.assertFalse(state["controller"]["stale"])
        self.assertEqual(state["nodes"][0]["runtime_state"],"observation_failed")
        self.assertNotIn("private-url",json.dumps(state))

    def test_historical_retired_worker_does_not_hold_current_drain_open(self):
        self.create()
        self.controller.tick()
        node=self.service.state(self.actor)["nodes"][0]
        control=WorkerControl(self.repo)
        old=control.get(node["slots"][0]["id"])
        control.retire(old["id"],upstream_idle_confirmed=True)
        values=dict(old["spec"])
        values["worker_id"]="replacement-worker"
        values["physical_gpu_ids"]=tuple(values["physical_gpu_ids"])
        values["recipe_ids"]=tuple(values["recipe_ids"])
        control.register(WorkerSpec(**values))
        control.mark_ready("replacement-worker",upstream_idle_confirmed=True)
        fresh=self.service.state(self.actor)["nodes"][0]
        self.service.node_command(self.actor,node["id"],{"expected_version":fresh["version"]},"drain-replacement","drain")
        self.controller.tick()
        state=self.service.state(self.actor)
        self.assertEqual(next(op for op in state["operations"] if op["kind"]=="drain")["state"],"completed")
        self.assertEqual(control.get(old["id"])["state"],"retired")
        self.assertEqual(control.get("replacement-worker")["drain_requested"],1)
        self.assertEqual(self.provider.destroys,[])
