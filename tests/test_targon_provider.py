"""Offline Targon lifecycle fixtures; no real credentials, VM, or generation."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock

import httpx

from studio_platform.scaler import CreationNotSubmitted, LaunchSpec
from studio_platform.targon_provider import (BASE_URL, PROFILE, SERVICE, KEY_VARIABLE,
    TargonError, TargonIdleProof, TargonManifest, TargonProvider)


TAG = "f1111111-1111-4111-8111-111111111111"
INSTANCE = "wkl-fixture123"


class CleanupGuard:
    def __init__(self):
        self.receipts = {}
        self.calls = []

    def arm(self, instance_id, deadline):
        self.calls.append((instance_id, deadline))
        self.receipts[instance_id] = {"instance_id":instance_id,"deadline":deadline,"armed":True,"independent":True}

    def proof(self, instance_id):
        return self.receipts.get(instance_id)


class TargonProviderTests(unittest.TestCase):
    def test_exact_sku_and_quote_rechecked_before_workload_mutation(self):
        for selection in (
            {"provider":"targon","offer_id":"other-sku","gpu_count":1,"hourly_cost_microusd":1690000},
            {"provider":"targon","offer_id":"rtx6000b-small","gpu_count":2,"hourly_cost_microusd":1690000},
            {"provider":"targon","offer_id":"rtx6000b-small","gpu_count":1,"hourly_cost_microusd":1600000},
        ):
            with self.subTest(selection=selection):
                provider=self.provider()
                with self.assertRaises(CreationNotSubmitted):
                    provider.create_selected_for_intent(self.tag,self.launch,selected_offer=selection,
                        hard_deadline=self.now+7200,intent_created_at=self.now)
                self.assertFalse(any(method=="POST" for method,path in self.requests))

    def test_exact_targon_resource_preserves_existing_vm_lifecycle(self):
        provider=self.provider()
        result=provider.create_selected_for_intent(self.tag,self.launch,
            selected_offer={"provider":"targon","offer_id":"rtx6000b-small","gpu_count":1,
                            "hourly_cost_microusd":1690000},
            hard_deadline=self.now+7200,intent_created_at=self.now)
        self.assertEqual(result.instance_id,INSTANCE)
        self.assertEqual(sum(method=="POST" for method,path in self.requests),2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="h3-targon-test-")
        self.now = 1800000000.0
        self.tag = TAG
        self.manifest = TargonManifest(configuration_id="fixture",model_id="h3-int8",org_slug="fixture-org",
            resource_name="rtx6000b-small",image_name="ubuntu-fixture",ssh_key_ids=("shk-fixture",),gpu_count=1,
            hourly_cost_cap_microusd=1690000,approved_until=self.now+10000,minimum_ram_gib=96,minimum_disk_gib=128,
            allow_preflight_only_price_cap=True,allow_controller_lifetime=True)
        self.launch = LaunchSpec("targon","fixture","h3-int8",offer_id="rtx6000b-small",image_id="ubuntu-fixture")
        self.requests, self.workloads, self.clients = [], [], []
        self.guard = CleanupGuard()
        self.fail_register = self.fail_deploy = self.fail_delete = False
        self.missing_after_delete = False
        self.inventory = [{"name":"rtx6000b-small","type":"vm","available":3,"cost_per_hour":1.69,
            "spec":{"gpu_model":"RTX-PRO-6000B","gpu_count":1,"memory_mib":128000,"disk_size_mib":327680}}]
        self.loader = Mock(return_value=SimpleNamespace(service=SERVICE,profile=PROFILE,base_url=BASE_URL,
            primary_key_variable=KEY_VARIABLE,api_key="fixture-secret"))

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.temp.cleanup()

    def workload(self, status="registered", **changes):
        return {"uid":INSTANCE,"name":self.tag.replace("-",""),"type":"VM","image":"ubuntu-fixture",
            "resource":{"name":"rtx6000b-small","gpu_count":1},"cost_per_hour":1.69,
            "created_at":datetime.fromtimestamp(self.now,timezone.utc).isoformat(),
            "state":{"status":status,"public_ip":"8.8.8.8","ssh_port":4022},**changes}

    def handler(self, request):
        self.requests.append((request.method,request.url.path))
        self.assertEqual(request.headers["authorization"],"Bearer fixture-secret")
        self.assertEqual(request.url.host,"api.targon.com")
        path = request.url.path
        if path.endswith("/inventory"):
            return httpx.Response(200,json=self.inventory)
        if path.endswith("/workloads"):
            if request.method == "GET":
                return httpx.Response(200,json={"items":self.workloads,"next_cursor":None})
            payload = json.loads(request.content)
            self.assertEqual(payload,{"type":"VM","name":self.tag.replace("-",""),"image":"ubuntu-fixture",
                "resource_name":"rtx6000b-small","ssh_keys":["shk-fixture"],
                "vm_config":{"hostname":self.tag.replace("-","")}})
            self.workloads = [self.workload()]
            if self.fail_register:
                raise httpx.ReadTimeout("fixture-secret")
            return httpx.Response(201,json=self.workloads[0])
        if path.endswith("/deploy"):
            self.workloads[0]["state"]["status"] = "provisioning"
            if self.fail_deploy:
                raise httpx.ReadTimeout("fixture-secret")
            return httpx.Response(200,json=self.workloads[0])
        if path.endswith("/"+INSTANCE):
            if request.method == "DELETE":
                self.workloads[0]["state"]["status"] = "deleted"
                if self.fail_delete:
                    raise httpx.ReadTimeout("fixture-secret")
                return httpx.Response(204)
            if not self.workloads or self.missing_after_delete and self.workloads[0]["state"]["status"] == "deleted":
                return httpx.Response(404,json={"error":"not found"})
            return httpx.Response(200,json=self.workloads[0])
        self.fail("unexpected request")

    def provider(self, **changes):
        options = dict(enabled=True,manifests=(self.manifest,),journal_dir=Path(self.temp.name)/"journal",
            loader=self.loader,transport=httpx.MockTransport(self.handler),clock=lambda:self.now,cleanup_guard=self.guard)
        options.update(changes)
        result = TargonProvider(**options)
        self.clients.append(result)
        return result

    def create(self, provider):
        return provider.create_for_intent(TAG,self.launch,hard_deadline=self.now+7200,intent_created_at=self.now)

    def mutations(self):
        return [value for value in self.requests if value[0] != "GET"]

    def test_inert_default_and_exact_central_identity(self):
        provider = TargonProvider(loader=self.loader)
        self.loader.assert_not_called()
        with self.assertRaisesRegex(TargonError,"provider_disabled"):
            provider.preflight_availability(self.launch)
        self.loader.assert_not_called()
        bad = Mock(return_value=SimpleNamespace(service=SERVICE,profile="wrong",base_url=BASE_URL,
            primary_key_variable=KEY_VARIABLE,api_key="fixture-secret"))
        provider = self.provider(loader=bad)
        with self.assertRaisesRegex(TargonError,"profile_unavailable_or_mismatched"):
            self.create(provider)
        self.assertFalse(self.requests)

    def test_register_deploy_running_ssh_idle_and_lifetime(self):
        provider = self.provider()
        provider.validate_launch(self.launch,physical_gpus=1,slots=1,reserved_cost_microusd=3380000,
            hard_deadline=self.now+7200)
        self.loader.assert_not_called()
        fact = self.create(provider)
        self.assertEqual((fact.state,fact.instance_id),("starting",INSTANCE))
        self.assertEqual(len(self.mutations()),2)
        self.assertEqual(self.guard.calls,[(INSTANCE,self.now+7200)])
        self.workloads[0]["state"]["status"] = "running"
        self.assertEqual(provider.reconcile(TAG,INSTANCE).state,"running")
        self.assertFalse(provider.reconcile(TAG,INSTANCE).idle_confirmed)
        provider._idle_probe = lambda tag,instance:TargonIdleProof(instance,self.now,self.now-60,True)
        self.assertTrue(provider.reconcile(TAG,INSTANCE).idle_confirmed)
        self.assertEqual(provider.ssh_connection(TAG,INSTANCE),
            {"instance_id":INSTANCE,"host":"8.8.8.8","port":4022,"username":"ubuntu"})
        value = provider.lifetime(TAG,INSTANCE,local_created_at=self.now)
        self.assertEqual(value["enforcement"],"independent_watchdog")
        self.assertEqual(value["safe_deadline"],self.now+7200)
        self.assertFalse(value["provider_ttl_confirmed"])
        self.assertIsNone(value["provider_removal_scheduled_at"])
        self.assertIsNone(provider.billing(TAG,INSTANCE))
        self.assertNotIn("fixture-secret","".join(path.read_text() for path in (Path(self.temp.name)/"journal").glob("*.json")))

    def test_lost_registration_recovers_first_deploy_once_across_restart(self):
        self.fail_register = True
        provider = self.provider()
        with self.assertRaisesRegex(TargonError,"request_unconfirmed"):
            self.create(provider)
        provider = self.provider()
        fact = provider.reconcile(TAG)
        self.assertEqual(fact.instance_id,INSTANCE)
        self.assertEqual(len(self.mutations()),2)
        provider.reconcile(TAG,INSTANCE)
        with self.assertRaisesRegex(TargonError,"already_submitted"):
            self.create(provider)
        self.assertEqual(len(self.mutations()),2)

    def test_sparse_post_acknowledgements_use_exact_get_before_deploy_and_readiness(self):
        def sparse(request):
            response = self.handler(request)
            if request.method == "POST":
                # Registration supplies only a UID; deployment may be an empty
                # acknowledgement for the already bound UID.
                return httpx.Response(200,json={} if request.url.path.endswith("/deploy") else {"uid":INSTANCE})
            return response
        provider = self.provider(transport=httpx.MockTransport(sparse))
        fact = self.create(provider)
        self.assertEqual((fact.state,fact.instance_id),("starting",INSTANCE))
        self.assertEqual(provider._journal.read(TAG)["phase"],"deployed")
        workload_route = "/tha/v3/orgs/fixture-org/workloads"
        self.assertEqual(self.requests[-4:],[
            ("POST",workload_route),("GET",workload_route+"/"+INSTANCE),
            ("POST",workload_route+"/"+INSTANCE+"/deploy"),("GET",workload_route+"/"+INSTANCE)])
        self.assertEqual(self.guard.calls,[(INSTANCE,self.now+7200)])

    def test_registration_observation_failure_retains_uid_and_recovers_without_listing(self):
        fail_get = True
        def sparse(request):
            response = self.handler(request)
            if request.method == "POST" and request.url.path.endswith("/workloads"):
                return httpx.Response(201,json={"uid":INSTANCE})
            if fail_get and request.method == "GET" and request.url.path.endswith("/"+INSTANCE):
                raise httpx.ReadTimeout("fixture-secret")
            return response
        provider = self.provider(transport=httpx.MockTransport(sparse))
        with self.assertRaisesRegex(TargonError,"request_unconfirmed"):
            self.create(provider)
        marker = provider._journal.read(TAG)
        self.assertEqual((marker["phase"],marker["instance_id"]),("register_started",INSTANCE))
        self.assertEqual(len(self.mutations()),1)
        self.assertFalse(self.guard.calls)
        fail_get = False
        self.requests.clear()
        provider = self.provider(transport=httpx.MockTransport(sparse))
        provider.reconcile(TAG)
        self.assertEqual(self.requests[0],("GET","/tha/v3/orgs/fixture-org/workloads/"+INSTANCE))
        self.assertEqual(len(self.mutations()),1)  # Only the first deploy, never re-register.
        self.assertEqual(provider._journal.read(TAG)["deadline"],marker["deadline"])

    def test_post_get_404_is_unknown_not_permission_to_repeat_registration(self):
        def invisible(request):
            response = self.handler(request)
            if request.method == "GET" and request.url.path.endswith("/"+INSTANCE):
                return httpx.Response(404)
            return response
        provider = self.provider(transport=httpx.MockTransport(invisible))
        with self.assertRaisesRegex(TargonError,"identity_unconfirmed"):
            self.create(provider)
        fact = provider.reconcile(TAG)
        self.assertEqual((fact.state,fact.instance_id),("unknown",INSTANCE))
        self.assertFalse(fact.absence_confirmed)
        self.assertFalse(self.guard.calls)
        with self.assertRaisesRegex(TargonError,"already_submitted"):
            self.create(provider)
        self.assertEqual(len(self.mutations()),1)
        self.provider().reconcile(TAG)
        self.assertEqual(len(self.mutations()),2)

    def test_sparse_registration_cannot_adopt_foreign_exact_get_identity(self):
        def foreign(request):
            response = self.handler(request)
            if request.method == "POST" and request.url.path.endswith("/workloads"):
                return httpx.Response(201,json={"uid":INSTANCE})
            if request.method == "GET" and request.url.path.endswith("/"+INSTANCE):
                return httpx.Response(200,json=self.workload(name="foreign-workload"))
            return response
        provider = self.provider(transport=httpx.MockTransport(foreign))
        with self.assertRaisesRegex(TargonError,"identity_unconfirmed"):
            self.create(provider)
        with self.assertRaisesRegex(TargonError,"identity_unconfirmed"):
            provider.reconcile(TAG)
        self.assertFalse(self.guard.calls)
        self.assertEqual(len(self.mutations()),1)
        self.assertEqual(provider._journal.read(TAG)["phase"],"register_started")

    def test_conflicting_post_identity_is_not_ignored_even_if_get_would_match(self):
        def conflict(request):
            response = self.handler(request)
            if request.method == "POST" and request.url.path.endswith("/deploy"):
                return httpx.Response(200,json={"uid":"foreign-workload"})
            return response
        provider = self.provider(transport=httpx.MockTransport(conflict))
        with self.assertRaisesRegex(TargonError,"instance_identity_conflict"):
            self.create(provider)
        self.assertEqual(provider._journal.read(TAG)["phase"],"deploy_started")
        self.provider().reconcile(TAG)
        self.assertEqual(len(self.mutations()),2)

    def test_lost_post_deploy_get_keeps_single_deploy_barrier(self):
        def delayed(request):
            response = self.handler(request)
            if (request.method == "GET" and request.url.path.endswith("/"+INSTANCE)
                    and self.workloads[0]["state"]["status"] == "provisioning"):
                raise httpx.ReadTimeout("fixture-secret")
            return response
        provider = self.provider(transport=httpx.MockTransport(delayed))
        with self.assertRaisesRegex(TargonError,"request_unconfirmed"):
            self.create(provider)
        self.assertEqual(provider._journal.read(TAG)["phase"],"deploy_started")
        self.workloads[0]["state"]["status"] = "registered"
        provider = self.provider()
        provider.reconcile(TAG)
        provider.reconcile(TAG,INSTANCE)
        self.assertEqual(len(self.mutations()),2)

    def test_ambiguous_deploy_is_never_repeated_even_if_get_says_registered(self):
        self.fail_deploy = True
        with self.assertRaisesRegex(TargonError,"request_unconfirmed"):
            self.create(self.provider())
        self.workloads[0]["state"]["status"] = "registered"
        provider = self.provider()
        self.assertEqual(provider.reconcile(TAG).instance_id,INSTANCE)
        provider.reconcile(TAG,INSTANCE)
        self.assertEqual(len(self.mutations()),2)
        self.workloads = []
        self.assertEqual(provider.reconcile(TAG,INSTANCE).state,"unknown")
        self.assertIsNone(provider.billing(TAG,INSTANCE))

    def test_empty_listing_after_lost_registration_never_proves_absence(self):
        self.fail_register = True
        with self.assertRaises(TargonError):
            self.create(self.provider())
        self.workloads = []
        fact = self.provider().reconcile(TAG)
        self.assertEqual(fact.state,"unknown")
        self.assertFalse(fact.absence_confirmed)
        self.assertEqual(len(self.mutations()),1)

    def test_deploy_requires_exact_independent_cleanup_receipt(self):
        guard = Mock()
        for proof in (None,{"instance_id":"other","deadline":self.now+7200,"independent":True,"armed":True},
                {"instance_id":INSTANCE,"deadline":self.now+7201,"independent":True,"armed":True},
                {"instance_id":INSTANCE,"deadline":self.now+7200,"independent":False,"armed":True}):
            with self.subTest(proof=proof):
                guard.proof.return_value = proof
                provider = self.provider(cleanup_guard=guard)
                if not self.workloads:
                    with self.assertRaisesRegex(TargonError,"independent_cleanup_unconfirmed"):
                        self.create(provider)
                else:
                    with self.assertRaisesRegex(TargonError,"independent_cleanup_unconfirmed"):
                        provider.reconcile(TAG)
                self.assertEqual(len(self.mutations()),1)
                self.assertFalse(provider.execution_allowed(TAG,INSTANCE))
                with self.assertRaisesRegex(TargonError,"independent_cleanup_unconfirmed"):
                    provider.lifetime(TAG,INSTANCE,local_created_at=self.now)
        self.provider().reconcile(TAG)
        self.assertEqual(len(self.mutations()),2)

    def test_expired_registered_workload_is_not_deployed_and_deadline_never_renews(self):
        provider = self.provider(cleanup_guard=None)
        with self.assertRaises(TargonError):
            self.create(provider)
        created = self.now
        self.now += 7201
        provider = self.provider()
        provider.reconcile(TAG,INSTANCE)
        self.assertEqual(len(self.mutations()),1)
        self.assertFalse(provider.execution_allowed(TAG,INSTANCE))
        with self.assertRaisesRegex(TargonError,"independent_cleanup_unconfirmed"):
            provider.lifetime(TAG,INSTANCE,local_created_at=created)

    def test_delete_acknowledgement_and_exact_get_reconcile_preserve_pending_cost(self):
        provider = self.provider()
        self.create(provider)
        self.missing_after_delete = True
        fact = provider.destroy(TAG,INSTANCE)
        self.assertEqual(fact.state,"destroyed")
        self.assertIsNone(fact.actual_cost_microusd)
        provider = self.provider()
        self.assertEqual(provider.reconcile(TAG,INSTANCE).state,"destroyed")
        self.assertEqual(provider.destroy(TAG,INSTANCE).state,"destroyed")
        self.assertEqual(len(self.mutations()),3)
        self.assertIsNone(provider.billing(TAG,INSTANCE))

    def test_lost_delete_and_404_are_unknown_until_explicit_deleted_state(self):
        provider = self.provider()
        self.create(provider)
        self.fail_delete, self.missing_after_delete = True, True
        with self.assertRaises(TargonError):
            provider.destroy(TAG,INSTANCE)
        provider = self.provider()
        self.assertEqual(provider.destroy(TAG,INSTANCE).state,"unknown")
        self.assertEqual(len(self.mutations()),3)
        self.missing_after_delete = False
        self.assertEqual(provider.reconcile(TAG,INSTANCE).state,"destroyed")

    def test_retained_delete_intent_never_reports_preparing_or_running(self):
        def pending_delete(request):
            response = self.handler(request)
            if request.method == "DELETE":
                self.workloads[0]["state"]["status"] = "provisioning"
            return response
        provider = self.provider(transport=httpx.MockTransport(pending_delete))
        self.create(provider)
        fact = provider.destroy(TAG,INSTANCE)
        self.assertEqual((fact.state,fact.instance_id),("unknown",INSTANCE))
        self.assertIsNone(fact.preparation_stage)
        self.assertIsNone(fact.provider_status)
        self.assertIsNone(fact.actual_cost_microusd)
        self.assertTrue(provider._journal.read(TAG)["delete_acknowledged"])
        self.workloads[0]["state"]["status"] = "running"
        idle_probe = Mock(return_value=TargonIdleProof(INSTANCE,self.now,self.now-60,True))
        provider = self.provider(idle_probe=idle_probe)
        fact = provider.destroy(TAG,INSTANCE)
        self.assertEqual(fact.state,"unknown")
        self.assertIsNone(fact.preparation_stage)
        self.assertFalse(fact.idle_confirmed)
        idle_probe.assert_not_called()
        self.assertFalse(provider.execution_allowed(TAG,INSTANCE))
        self.assertEqual(len(self.mutations()),3)
        self.assertIsNone(provider.billing(TAG,INSTANCE))
        self.workloads[0]["state"]["status"] = "deleted"
        self.assertEqual(provider.reconcile(TAG,INSTANCE).state,"destroyed")

    def test_independent_guard_removal_proof_reconciles_external_delete(self):
        provider = self.provider()
        self.create(provider)
        self.workloads = []
        proof = {"instance_id":INSTANCE,"deadline":self.now+7200,"independent":True,"removed":True,
                 "evidence":"exact_uid_404_after_delete_ack"}
        self.guard.removal_proof = lambda instance:proof
        self.assertEqual(provider.reconcile(TAG,INSTANCE).state,"destroyed")
        self.assertIsNone(provider.billing(TAG,INSTANCE))
        self.assertEqual(len(self.mutations()),2)

    def test_identity_ssh_and_manifest_mismatches_fail_closed(self):
        provider = self.provider()
        self.create(provider)
        self.workloads[0]["name"] = "foreign-workload"
        with self.assertRaisesRegex(TargonError,"identity_unconfirmed"):
            provider.destroy(TAG,INSTANCE)
        self.assertEqual(len(self.mutations()),2)
        self.workloads[0] = self.workload("running")
        self.workloads[0]["state"]["public_ip"] = "127.0.0.1"
        with self.assertRaisesRegex(TargonError,"ssh_coordinates_unverified"):
            provider.ssh_connection(TAG,INSTANCE)
        provider = self.provider(manifests=(replace(self.manifest,approved_until=self.now+20000),))
        with self.assertRaisesRegex(TargonError,"manifest_identity_conflict"):
            provider.reconcile(TAG,INSTANCE)

    def test_inventory_and_pure_budget_checks_prevent_registration(self):
        provider = self.provider()
        with self.assertRaisesRegex(TargonError,"capacity_or_reservation_mismatch"):
            provider.validate_launch(self.launch,physical_gpus=1,slots=1,reserved_cost_microusd=1,
                hard_deadline=self.now+7200)
        self.loader.assert_not_called()
        self.inventory[0]["cost_per_hour"] = 1.70
        with self.assertRaises(CreationNotSubmitted):
            self.create(provider)
        self.assertFalse(self.mutations())
        self.assertFalse((Path(self.temp.name)/"journal"/(TAG+".json")).exists())

    def test_provider_failures_are_static_and_do_not_leak_response_or_key(self):
        def failure(request):
            return httpx.Response(500,json={"error":"fixture-secret"})
        provider = self.provider(transport=httpx.MockTransport(failure))
        with self.assertRaisesRegex(TargonError,"^targon_request_unconfirmed$"):
            self.create(provider)
        self.assertFalse(self.mutations())

    def test_guard_loss_persists_unverified_controller_lifetime_and_blocks_worker_admission(self):
        from studio_platform.auth import Principal
        from studio_platform.operator_boot import _admission_window
        from studio_platform.operator_capacity import DeploymentBinding, OperatorCapacity, OperatorRegistry
        from studio_platform.operator_controller import OperatorController
        from test_platform_repository import LedgerCase
        ledger = LedgerCase()
        ledger.setUp()
        self.addCleanup(ledger.tearDown)
        self.now = ledger.now
        self.manifest = replace(self.manifest,approved_until=self.now+10000)
        binding = DeploymentBinding(binding_id="targon-test",runtime_profile_id="h3-test-int8",
            gpu_type="RTX PRO 6000 Blackwell Workstation Edition",gpu_count=1,pool="operator-test",
            configuration_id="fixture",model_id="h3-int8",recipe_ids=("h3-base-fl2va-v1",),
            engine_manifest_digest="a"*64,launch=self.launch,scope=ledger.scope,budget_account_ids=("owner-budget",),
            hourly_cost_microusd=1690000,reservation_per_node_microusd=3380000,expires_at=self.now+10000,enabled=True)
        service = OperatorCapacity(ledger.repo,SimpleNamespace(operator_capacity_owners=("superdan",)),
            OperatorRegistry([binding],qualified_providers=("targon",)))
        ledger.repo.configure_pool("operator-test",max_instances=2,max_physical_gpus=2)
        actor = Principal("superdan","browser",auth_mode="password")
        service.update_policy(actor,{"expected_version":0,"enabled":True,"max_instances":2,"max_physical_gpus":2,
            "max_hourly_cost_microusd":3380000,"idle_shutdown_seconds":600,"max_ttl_seconds":3600})
        chosen = {"provider":"targon","runtime_profile_id":"h3-test-int8","gpu_type":binding.gpu_type,
            "mode":"fl","node_count":1,"gpu_count":1,"ttl_seconds":3600}
        def handler(request):
            if request.method == "POST" and request.url.path.endswith("/workloads"):
                self.tag = str(uuid.UUID(json.loads(request.content)["name"]))
            return self.handler(request)
        provider = self.provider(transport=httpx.MockTransport(handler))
        # Controller's bound-provider seam returns a reason, while create's
        # inventory validation is covered separately above.
        provider.preflight_availability = lambda launch:None
        guards = []
        boot = SimpleNamespace(set_start_guard=guards.append,tick=lambda *a,**kw:"ready",request_drain=lambda:None)
        controller = OperatorController(service,provider_factory=lambda bound:provider,
            boot_factory=lambda *args:boot,enabled=True)
        preview = service.preview(actor,chosen)
        self.assertTrue(preview["can_start"],preview["blockers"])
        service.start(actor,{"preview_id":preview["preview_id"]},"targon-lifetime-test")
        controller.tick()
        self.workloads[0]["state"]["status"] = "running"
        controller.tick()
        intent = ledger.repo.list_instance_intents()[0]
        self.assertTrue(_admission_window(ledger.repo,intent["id"],binding)[2])
        self.guard.receipts.clear()
        controller.tick()
        node = service.state(actor)["nodes"][0]
        self.assertEqual(node["provider_lifetime_state"],"unverified")
        self.assertFalse(_admission_window(ledger.repo,intent["id"],binding)[2])
        self.assertTrue(guards)
        self.assertFalse(guards[-1]())
        self.assertEqual(len(self.mutations()),2)


if __name__ == "__main__":
    unittest.main()
