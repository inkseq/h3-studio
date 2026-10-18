"""Inventory recommendations are advisory; fake suppliers, no network or GPU."""
from dataclasses import asdict
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select, func, delete

from studio_platform.auth import Principal
from studio_platform.capacity_market import market_projection, publish_observation, market_inventory
from studio_platform.capacity_scan import MarketScanner
from studio_platform.operator_capacity import (OperatorRegistry, OperatorCapacity, OperatorError,
    DeploymentBinding, selection, operator_commands)
from studio_platform.operator_routes import register_routes
from studio_platform.repository import request_hash, capacity_gate
from studio_platform.runtime_catalog import PROFILE_IDS
from studio_platform.scaler import LaunchSpec
from test_platform_repository import LedgerCase


def offer(provider="targon", gpu="RTX PRO 6000 Blackwell", **changes):
    return {"provider":provider,"offer_id":"rtx6000b-small","gpu_type":gpu,"gpu_count":1,"available_count":9,
        "ram_gib":125,"disk_gib":320,"download_mbps":None,"country":None,"cpu_cores":32,
        "hourly_cost_microusd":1690000,"price_per_gpu_hour_microusd":1690000,"kind":"vm",
        "unverified_fields":["download_mbps","country","gpu_edition"],**changes}


def single_offer(offer_id, gpu="RTX 5090", provider="lium", **changes):
    return offer(provider, gpu, **{"offer_id":offer_id,"available_count":1,"download_mbps":300,
        "country":"SG","hourly_cost_microusd":750000,"price_per_gpu_hour_microusd":750000,
        "unverified_fields":[],**changes})


class MarketTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.chosen=selection({"runtime_profile_id":PROFILE_IDS[0],"mode":"ref","gpu_type":"RTX 5090",
            "gpu_count":1,"node_count":1,"ttl_seconds":7200})
        self.registry=OperatorRegistry()
        self.actor=Principal("superdan","browser",auth_mode="password")
        self.service=OperatorCapacity(self.repo,SimpleNamespace(operator_capacity_owners=("superdan",)),self.registry)

    def publish(self, provider, rows=(), status="ok", **extra):
        return publish_observation(self.repo,{"provider":provider,"status":status,"offers":list(rows),
            "observed_at":self.now,**extra})

    def project(self, chosen=None):
        with self.repo.engine.connect() as connection:
            return market_projection(connection,self.registry,chosen or self.chosen,self.now)

    def qualify(self, gpu="RTX 5090"):
        self.registry=OperatorRegistry([DeploymentBinding(binding_id="quantity-fixture",
            runtime_profile_id=PROFILE_IDS[0],gpu_type=gpu,gpu_count=1,pool="test",
            configuration_id="fixture",model_id="test",recipe_ids=("h3-base-ref2va-v1",),
            engine_manifest_digest="a"*64,launch=LaunchSpec("lium","fixture","test"),scope=self.scope,
            budget_account_ids=("owner-budget",),hourly_cost_microusd=750000,reservation_per_node_microusd=1500000,
            expires_at=self.now+10000,max_ttl_seconds=7200,enabled=True,filters={"min_ram_gib":96,
                "min_disk_gib":128,"min_download_mbps":200,"max_price_per_gpu_hour_microusd":850000,
                "allowed_countries":["SG"]})])

    def test_matching_quantity_aggregates_hosts_without_changing_quotes(self):
        self.qualify()
        self.publish("lium",[single_offer("first"),single_offer("second",hourly_cost_microusd=800000,
            price_per_gpu_hour_microusd=800000)])
        self.publish("targon")
        chosen={**self.chosen,"node_count":2}
        result=self.project(chosen)
        self.assertEqual(result["reason_code"],"inventory_matches_found")
        self.assertEqual([row["qualification"] for row in result["offers"]],["qualified","qualified"])
        self.assertEqual([row["available_count"] for row in result["offers"]],[1,1])
        self.assertEqual([row["hourly_cost_microusd"] for row in result["offers"]],[750000,800000])
        self.assertTrue(all(row["selection"]["node_count"]==2 for row in result["offers"]))
        result=self.project({**chosen,"node_count":3})
        self.assertEqual(result["reason_code"],"inventory_no_matching_stock")
        self.assertTrue(all("inventory_insufficient_quantity" in row["blockers"] for row in result["offers"]))
        self.assertTrue(all(row["qualification"]=="unqualified" for row in result["offers"]))

    def test_incompatible_or_uncertain_hosts_do_not_complete_qualified_quantity(self):
        self.qualify()
        self.publish("targon",[offer()])
        for changes,uncertain in (({"ram_gib":64},False),({"disk_gib":64},False),
                ({"download_mbps":100},False),({"country":"RO"},False),
                ({"price_per_gpu_hour_microusd":900000},False),({"download_mbps":None},True),
                ({"available_count":0,"available_gpu_count":1,"min_gpu_count_for_rental":1,
                  "unverified_fields":["allocation"]},True)):
            with self.subTest(changes=changes):
                self.publish("lium",[single_offer("first"),single_offer("second",**changes)])
                result=self.project({**self.chosen,"node_count":2})
                self.assertTrue(all(row["qualification"]=="unqualified" for row in result["offers"]))
                self.assertIn("inventory_insufficient_quantity",result["offers"][0]["blockers"])
                self.assertEqual(result["reason_code"],"inventory_specs_unconfirmed" if uncertain
                    else "inventory_no_matching_stock")
                if uncertain:self.assertFalse(result["recommendations"])

    def test_advisory_quantity_groups_preserve_blockers_prices_and_gpu_editions(self):
        pro="RTX PRO 6000 Blackwell Workstation Edition"
        self.qualify(pro)
        first=single_offer("first",pro)
        second=single_offer("second",pro,hourly_cost_microusd=1200000,
            price_per_gpu_hour_microusd=1200000,download_mbps=None)
        self.publish("lium",[first,second])
        self.publish("targon")
        chosen={**self.chosen,"node_count":2}
        result=self.project(chosen)
        candidates=result["recommendations"]
        self.assertEqual(len(candidates),2)
        self.assertEqual([row["available_count"] for row in candidates],[1,1])
        self.assertEqual([row["hourly_cost_microusd"] for row in candidates],[750000,1200000])
        self.assertTrue(all(row["selection"]["node_count"]==2 for row in candidates))
        self.assertTrue(all(row["qualification"]=="unqualified" for row in candidates))
        self.assertIn("inventory_insufficient_quantity",candidates[0]["blockers"])
        self.assertIn("inventory_price_above_limit",candidates[1]["blockers"])
        self.assertIn("inventory_unknown_bandwidth",candidates[1]["blockers"])
        self.assertFalse(self.project({**chosen,"node_count":3})["recommendations"])
        for other in ({**second,"provider":"targon"},
                      {**second,"gpu_type":"RTX PRO 6000 Blackwell Server Edition"}):
            with self.subTest(provider=other["provider"],gpu=other["gpu_type"]):
                self.publish("lium",[first]+([other] if other["provider"]=="lium" else []))
                self.publish("targon",[other] if other["provider"]=="targon" else [])
                self.assertFalse(self.project(chosen)["recommendations"])

    def test_empty_5090_recommends_single_pro_without_changing_recipe_or_cost_limit(self):
        self.publish("lium")
        self.publish("targon",[offer(),offer(gpu="H100",offer_id="h100"),offer(gpu_count=8,offer_id="eight")])
        before=copy.deepcopy(self.chosen)
        result=self.project()
        self.assertEqual(result["reason_code"],"inventory_no_matching_stock")
        self.assertEqual(len(result["recommendations"]),1)
        candidate=result["recommendations"][0]
        self.assertEqual(candidate["hourly_cost_microusd"],1690000)
        self.assertEqual(candidate["qualification"],"unqualified")
        self.assertIn("inventory_price_above_limit",candidate["blockers"])
        self.assertIn("inventory_unknown_bandwidth",candidate["blockers"])
        self.assertIn("operator_provider_start_unqualified",candidate["blockers"])
        for key,value in before.items():
            if key!="gpu_type": self.assertEqual(candidate["selection"][key],value)
        self.assertEqual(before,self.chosen)
        self.assertEqual(result["filters"]["max_price_per_gpu_hour_microusd"],850000)

    def test_matching_5090_or_unknown_spec_suppresses_upgrade(self):
        self.publish("targon",[offer()])
        row=offer("lium","RTX 5090",download_mbps=300,price_per_gpu_hour_microusd=750000,
            hourly_cost_microusd=750000,unverified_fields=[])
        self.publish("lium",[row])
        self.assertEqual(self.project()["reason_code"],"inventory_matches_found")
        self.assertFalse(self.project()["recommendations"])
        self.publish("lium",[{**row,"download_mbps":None}])
        self.assertEqual(self.project()["reason_code"],"inventory_specs_unconfirmed")
        self.assertFalse(self.project()["recommendations"])

    def test_failure_stale_and_future_do_not_prove_no_stock(self):
        self.publish("lium")
        self.publish("targon",[offer()])
        self.publish("lium",status="error",reason_code="RAW SECRET error")
        result=self.project()
        self.assertFalse(result["recommendations"])
        self.assertNotIn("RAW SECRET",json.dumps(result))
        self.publish("lium")
        self.now+=121
        self.assertFalse(self.project()["recommendations"])
        self.now-=125
        self.assertFalse(self.project()["recommendations"])
        with self.assertRaisesRegex(ValueError,"observation_invalid"):
            self.publish("lium",observed_at=self.now+10)

    def test_possibly_splittable_host_is_not_proof_of_single_gpu_absence(self):
        self.publish("targon",[offer()])
        for minimum, free, count in ((1,8,1),(None,8,1),(1,4,0)):
            self.publish("lium",[offer("lium","RTX 5090",gpu_count=8,available_count=count,
                available_gpu_count=free,min_gpu_count_for_rental=minimum)])
            self.assertEqual(self.project()["reason_code"],"inventory_specs_unconfirmed")
            self.assertFalse(self.project()["recommendations"])
        self.publish("lium",[offer("lium","RTX 5090",gpu_count=8,available_count=1,
            available_gpu_count=8,min_gpu_count_for_rental=4)])
        self.assertEqual(len(self.project()["recommendations"]),1)

    def test_out_of_order_response_cannot_replace_failure_or_restore_old_stock(self):
        self.publish("lium",status="error")
        self.assertFalse(self.publish("lium",observed_at=self.now-1))
        self.assertEqual(self.project()["providers"][0]["status"],"error")

    def test_recommendation_respects_count_country_and_memory(self):
        self.publish("lium")
        self.publish("targon",[offer(ram_gib=64),offer(offer_id="pro2",gpu_count=2),
            offer(offer_id="lowquantity",available_count=1),offer(offer_id="wrongcountry",country="RO")])
        chosen={**self.chosen,"node_count":2,"filters":{"min_ram_gib":96,"allowed_countries":["SG"]}}
        self.assertFalse(self.project(chosen)["recommendations"])

    def test_provider_is_part_of_new_identity_but_absent_legacy_stays_identical(self):
        original={key:value for key,value in self.chosen.items() if key!="filters"}
        self.assertNotIn("provider",selection(original))
        self.assertEqual(request_hash(selection(original)),request_hash({**original,"filters":{}}))
        binding=DeploymentBinding(binding_id="fixture",runtime_profile_id=PROFILE_IDS[0],gpu_type="RTX 5090",
            gpu_count=1,pool="test",configuration_id="fixture",model_id="test",recipe_ids=("h3-base-ref2va-v1",),
            engine_manifest_digest="a"*64,launch=LaunchSpec("lium","fixture","test"),scope=self.scope,
            budget_account_ids=("owner-budget",),hourly_cost_microusd=750000,reservation_per_node_microusd=1500000,
            expires_at=10000,max_ttl_seconds=7200,enabled=True)
        legacy=asdict(binding);legacy.pop("enabled")
        self.assertEqual(binding.fingerprint,request_hash(legacy))
        self.assertTrue(binding.matches(self.chosen))
        self.assertFalse(binding.matches({**self.chosen,"provider":"targon"}))
        resolver=Mock(return_value=binding)
        registry=OperatorRegistry([binding],resolver=resolver)
        with self.assertRaisesRegex(OperatorError,"operator_provider_start_unqualified"):
            registry.resolve({**self.chosen,"provider":"targon"})
        resolver.assert_not_called()
        for bad in (None,"other",[],{}):
            with self.assertRaises(OperatorError): selection({**self.chosen,"provider":bad})

    def test_targon_preview_and_start_cannot_reserve_or_rent(self):
        before=self.repo.get_budget("owner-budget")
        value=self.service.preview(self.actor,{**self.chosen,"provider":"targon"})
        self.assertFalse(value["can_start"])
        self.assertIn({"code":"operator_provider_start_unqualified"},value["blockers"])
        with self.assertRaises(OperatorError):
            self.service.start(self.actor,{"preview_id":value["preview_id"]},"must-not-start")
        self.assertEqual(before,self.repo.get_budget("owner-budget"))
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.scalar(select(func.count()).select_from(operator_commands)),0)

    def test_http_preserves_full_selection_and_reads_only_cache(self):
        self.publish("targon",[offer()])
        app=FastAPI()
        actor=[self.actor]
        @app.middleware("http")
        async def principal(request:Request,call_next):
            request.state.principal=actor[0]
            return await call_next(request)
        register_routes(app,service=self.service)
        query={**self.chosen,"provider":"targon","node_count":2,"filters":json.dumps({"min_ram_gib":96})}
        with patch("studio_platform.lium_provider._central_loader",side_effect=AssertionError("credentials")), \
             patch("httpx.HTTPTransport.handle_request",side_effect=AssertionError("network")), TestClient(app) as client:
            result=client.get("/v1/operator/capacity/offers",params=query)
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.headers["cache-control"],"no-store")
            chosen=result.json()["market"]["selection"]
            self.assertEqual(chosen["node_count"],2)
            self.assertEqual(chosen["ttl_seconds"],7200)
            self.assertEqual(chosen["provider"],"targon")
            self.assertEqual(chosen["filters"],{"min_ram_gib":96})
            actor[0]=Principal("supervan","browser")
            self.assertEqual(client.get("/v1/operator/capacity/offers",params=query).status_code,403)

    def test_scanner_is_inert_until_explicitly_run_and_sanitizes_error(self):
        readers={"lium":Mock(side_effect=RuntimeError("secret-bearing-detail")),
            "targon":Mock(return_value={"provider":"targon","status":"ok","observed_at":self.now,"offers":[offer()]})}
        scanner=MarketScanner(self.repo,readers=readers)
        for reader in readers.values():reader.assert_not_called()
        before=self.repo.get_budget("owner-budget")
        scanner.once()
        self.assertEqual(before,self.repo.get_budget("owner-budget"))
        self.assertEqual(self.project()["providers"][0]["status"],"error")
        self.assertNotIn("secret-bearing-detail",json.dumps(self.project()))

    def test_stock_cache_works_without_enabling_capacity(self):
        with self.repo.transaction() as connection:
            connection.execute(delete(capacity_gate))
        self.publish("targon",[offer()])
        self.assertEqual(self.project()["providers"][1]["status"],"ok")
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.scalar(select(func.count()).select_from(capacity_gate)),0)
