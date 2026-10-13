"""Model-first discovery and exact cached-offer validation; no provider calls."""
from dataclasses import replace
import copy
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from studio_platform.auth import Principal
from studio_platform.capacity_candidates import candidates_projection, resolve_selected_offer
from studio_platform.capacity_inventory import allocation_resources
from studio_platform.capacity_market import publish_observation
from studio_platform.operator_capacity import DeploymentBinding, OperatorCapacity, OperatorError, OperatorRegistry
from studio_platform.operator_routes import register_routes
from studio_platform.runtime_catalog import PROFILE_IDS, get_profile, public_catalog
from studio_platform.scaler import LaunchSpec
from test_platform_repository import LedgerCase


PRUNED = "MiniMax-H3-Pruned-Rank8-INT8"
BASE = "MiniMax-H3-Base-INT8"
PRO = "RTX PRO 6000 Blackwell Workstation Edition"


def historical_catalog():
    """Keep original-allocation fixtures tied to their four immutable recipes."""
    catalog = public_catalog()
    catalog['profiles'] = [profile for profile in catalog['profiles'] if profile['id'] in PROFILE_IDS[:4]]
    return catalog


def offer(identity="executor-1", provider="lium", gpu="RTX 5090", **changes):
    return {"provider": provider, "offer_id": identity, "gpu_type": gpu, "gpu_count": 1,
            "available_count": 1, "ram_gib": 125, "disk_gib": 320, "download_mbps": 300,
            "country": "SG", "cpu_cores": 32, "hourly_cost_microusd": 750000,
            "price_per_gpu_hour_microusd": 750000, "kind": "executor" if provider == "lium" else "vm",
            "unverified_fields": [], **changes}


class CandidateTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.registry = OperatorRegistry(catalog=historical_catalog, qualified_providers=("lium", "targon"))

    def publish(self, provider, rows=(), status="ok", observed=None):
        publish_observation(self.repo, {"provider": provider, "status": status,
            "observed_at": self.now if observed is None else observed, "offers": list(rows)})

    def project(self, model=PRUNED, ttl=7200):
        with self.repo.engine.connect() as connection:
            return candidates_projection(connection, self.registry, model, "ref", ttl, self.now)

    def resolve(self, choice):
        with self.repo.engine.connect() as connection:
            return resolve_selected_offer(connection, self.registry, choice, self.now)

    def qualify(self, profile_id=PROFILE_IDS[0], gpu="RTX 5090", provider="lium", **changes):
        profile = get_profile(profile_id)
        binding = DeploymentBinding(binding_id="candidate-" + provider, runtime_profile_id=profile_id,
            gpu_type=gpu, gpu_count=1, execution_slots=1, pool="test", configuration_id="candidate",
            model_id=profile["model_id"], recipe_ids=("h3-base-ref2va-v1",), engine_manifest_digest="a"*64,
            launch=LaunchSpec(provider, "candidate", profile["model_id"]), scope=self.scope,
            budget_account_ids=("owner-budget",), hourly_cost_microusd=2000000,
            reservation_per_node_microusd=4000000, expires_at=self.now+10000, max_ttl_seconds=7200,
            min_ttl_seconds=120, enabled=True, filters={"min_ram_gib":96, "min_disk_gib":128})
        binding = replace(binding, **changes)
        existing = list(self.registry.bindings.values())
        self.registry = OperatorRegistry(existing+[binding], catalog=historical_catalog,
                                         qualified_providers=("lium", "targon"))
        return binding

    def test_groups_only_exact_model_across_profiles_and_explicit_gpu_names(self):
        self.publish("lium", [offer(), offer("pro", gpu=PRO), offer("h100", gpu="H100")])
        self.publish("targon", [offer("rtx6000b-small", "targon", "RTX PRO 6000 Blackwell")])
        result = self.project()
        self.assertEqual({row["offer_id"] for row in result["candidates"]}, {"executor-1", "pro", "rtx6000b-small"})
        self.assertEqual({row["selection"]["runtime_profile_id"] for row in result["candidates"]},
                         {PROFILE_IDS[0], PROFILE_IDS[3]})
        for row in result["candidates"]:
            self.assertEqual(get_profile(row["selection"]["runtime_profile_id"])["model_id"], PRUNED)
            self.assertEqual(row["selection"]["offer_id"], row["offer_id"])
            self.assertEqual(row["selection"]["node_count"], 1)
        self.assertEqual(self.project(BASE)["candidates"], [])

    def test_base_never_infers_gpu_edition_or_changes_precision(self):
        self.publish("lium", [offer("base", gpu=PRO, gpu_count=2, ram_gib=279, disk_gib=400)])
        self.publish("targon", [offer("generic", "targon", "RTX PRO 6000 Blackwell", ram_gib=512, disk_gib=400)])
        rows = self.project(BASE)["candidates"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["selection"]["runtime_profile_id"], PROFILE_IDS[1])
        self.assertEqual(rows[0]["selection"]["gpu_count"], 2)

    def test_29gib_discovery_does_not_requalify_historical_96gib_binding(self):
        historical = self.qualify()  # Existing 96 GiB deployment remains bound to its identity.
        self.registry = OperatorRegistry(list(self.registry.bindings.values()), catalog=public_catalog,
                                         qualified_providers=('lium', 'targon'))
        self.publish('lium', [offer('smaller', ram_gib=35)])
        result = self.project()
        self.assertEqual(len(result['candidates']), 3)
        self.assertEqual({row['selection']['runtime_profile_id'] for row in result['candidates']},
                         {PROFILE_IDS[4], PROFILE_IDS[5], PROFILE_IDS[6]})
        self.assertEqual(self.registry.bindings[historical.binding_id], historical)
        for candidate in result['candidates']:
            with self.subTest(profile=candidate['selection']['runtime_profile_id']):
                self.assertFalse(candidate['deployment_qualified'])
                self.assertEqual(candidate['qualification'], 'unqualified')
                with self.assertRaisesRegex(OperatorError, 'operator_deployment_not_configured'):
                    self.resolve(candidate['selection'])
        floors = {row['runtime_profile_id']: row['hard_requirements']['min_ram_gib']
                  for row in result['filters']}
        self.assertEqual(floors[PROFILE_IDS[0]], 96)
        self.assertEqual(floors[PROFILE_IDS[4]], 29)
        self.assertEqual(floors[PROFILE_IDS[5]], 29)
        self.assertEqual(floors[PROFILE_IDS[6]], 29)
        self.assertTrue(any(row['runtime_profile_id'] == PROFILE_IDS[0] and
                            'inventory_ram_below_minimum' in row['blockers'] for row in result['excluded']))
        self.assertEqual(self.repo.list_instance_intents(), [])

    def test_ram_disk_exclude_unknown_specs_remain_and_preferences_do_not_hide_stock(self):
        self.publish("lium", [offer("ram-low", ram_gib=64), offer("disk-low", disk_gib=100),
            offer("unknown", ram_gib=None, disk_gib=None, cpu_cores=None, hourly_cost_microusd=None),
            offer("costly-slow", hourly_cost_microusd=900000, price_per_gpu_hour_microusd=900000,
                  download_mbps=100)])
        self.publish("targon")
        rows = {row["offer_id"]:row for row in self.project()["candidates"]}
        self.assertEqual(set(rows), {"unknown", "costly-slow"})
        self.assertFalse(rows["unknown"]["specs_confirmed"])
        self.assertTrue({"inventory_unknown_ram", "inventory_unknown_disk", "inventory_unknown_cpu",
                         "inventory_unknown_price"} <= set(rows["unknown"]["blockers"]))
        self.assertTrue({"inventory_price_above_guidance", "inventory_bandwidth_below_guidance"}
                        <= set(rows["costly-slow"]["preference_hints"]))
        self.assertNotIn("inventory_price_above_limit", rows["costly-slow"]["blockers"])

    def test_lium_uses_allocated_ram_floor_without_changing_the_displayed_quote(self):
        self.qualify()
        self.publish("lium", [offer("too-small", ram_gib=98), offer("fits", ram_gib=100)])
        rows = self.project()["candidates"]
        self.assertEqual([row["offer_id"] for row in rows], ["fits"])
        self.assertEqual(rows[0]["ram_gib"], 100)
        self.assertEqual(rows[0]["allocation_ram_gib"], 96)
        excluded = self.project()["excluded"]
        self.assertEqual(excluded[0]["ram_gib"], 98)
        self.assertEqual(excluded[0]["allocation_ram_gib"], 94)
        self.assertIn("inventory_ram_below_minimum", excluded[0]["blockers"])
        self.assertEqual(self.resolve(rows[0]["selection"])["ram_gib"], 100)
        with self.assertRaisesRegex(OperatorError, "inventory_ram_below_minimum"):
            self.resolve({**rows[0]["selection"], "offer_id":"too-small"})
        raw = offer(ram_gib=1000)
        self.assertEqual(allocation_resources(raw)["ram_gib"], 990)
        self.assertEqual(raw["ram_gib"], 1000)
        targon = offer("sku", "targon", "RTX PRO 6000 Blackwell", ram_gib=98)
        self.assertEqual(allocation_resources(targon), targon)
        self.assertIsNone(allocation_resources(offer(ram_gib=None))["ram_gib"])

    def test_effective_filters_distinguish_profile_floors_guidance_and_protected_policy(self):
        binding = self.qualify(filters={"min_ram_gib":96, "min_disk_gib":250,
            "min_download_mbps":500, "max_price_per_gpu_hour_microusd":850000},
            boot={"path":"PRIVATE-CONTROLLER-PATH"})
        result = self.project()
        profile = next(item for item in result["filters"] if item["runtime_profile_id"] == binding.runtime_profile_id)
        self.assertEqual(profile["source"], "runtime_profile")
        self.assertEqual(profile["hard_requirements"]["min_ram_gib"], 96)
        self.assertEqual(profile["hard_requirements"]["min_disk_gib"], 128)
        self.assertEqual(profile["guidance"]["min_download_mbps"], 200)
        policy = profile["deployments"][0]
        self.assertEqual(policy["filters"]["min_disk_gib"], 250)
        self.assertEqual(policy["filters"]["min_download_mbps"], 500)
        self.assertEqual(policy["source"], "protected_operator_registry")
        self.assertTrue(policy["enabled"])
        self.assertEqual(policy["hourly_cost_ceiling_microusd"], binding.hourly_cost_microusd)
        self.assertNotIn("PRIVATE-CONTROLLER-PATH", str(result))
        self.assertNotIn("owner-budget", str(result))
        self.assertNotIn("budget_account_ids", str(result))
        other = next(item for item in result["filters"] if item["runtime_profile_id"] != binding.runtime_profile_id)
        self.assertEqual(other["deployments"], [])

    def test_excluded_stock_is_bounded_and_model_edition_mismatch_is_explained(self):
        self.publish("lium", [offer(str(index), ram_gib=62) for index in range(105)])
        self.publish("targon", [offer("generic-pro", "targon", "RTX PRO 6000 Blackwell")])
        pruned = self.project()
        self.assertEqual(pruned["excluded_count"], 105)
        self.assertEqual(len(pruned["excluded"]), 100)
        self.assertTrue(all("inventory_ram_below_minimum" in row["blockers"] for row in pruned["excluded"]))
        base = self.project(BASE)
        self.assertEqual(base["candidates"], [])
        self.assertEqual(base["excluded_count"], 106)
        # A generic supplier edition cannot borrow pruned qualification for Base.
        self.publish("lium")
        row = self.project(BASE)["excluded"][0]
        self.assertEqual(row["offer_id"], "generic-pro")
        self.assertIn("operator_gpu_not_catalogued", row["blockers"])
        self.assertIn("inventory_ram_below_minimum", row["blockers"])
        self.assertEqual(self.project(BASE)["filters"][0]["deployments"], [])

    def test_topology_is_pending_and_full_host_price_is_preserved(self):
        self.qualify()
        rows = [offer(str(count), gpu_count=count, hourly_cost_microusd=count*750000,
                      available_gpu_count=count) for count in (1, 2, 4)]
        self.publish("lium", rows)
        self.publish("targon")
        result = {row["gpu_count"]:row for row in self.project()["candidates"]}
        self.assertEqual(set(result), {1, 2, 4})
        self.assertEqual(result[1]["execution_slots"], 1)
        self.assertEqual(result[1]["qualification"], "qualified")
        for count in (2, 4):
            self.assertIsNone(result[count]["execution_slots"])
            self.assertIn("operator_topology_not_qualified", result[count]["blockers"])
            self.assertEqual(result[count]["hourly_cost_microusd"], count*750000)
            self.assertEqual(result[count]["selection"]["gpu_count"], count)

    def test_rank_deployable_then_whole_price_then_bandwidth_without_provider_priority(self):
        self.qualify()
        self.publish("lium", [offer("qualified", hourly_cost_microusd=900000),
            offer("slow", gpu=PRO, hourly_cost_microusd=800000, download_mbps=100),
            offer("unknown", gpu=PRO, hourly_cost_microusd=800000, download_mbps=None)])
        self.publish("targon", [offer("cheaper", "targon", "RTX PRO 6000 Blackwell",
            hourly_cost_microusd=700000), offer("fast", "targon", "RTX PRO 6000 Blackwell",
            hourly_cost_microusd=800000, download_mbps=1000)])
        rows = self.project()["candidates"]
        self.assertEqual([row["offer_id"] for row in rows], ["qualified", "cheaper", "fast", "slow", "unknown"])
        self.assertEqual(rows[1]["offer_kind"], "resource_sku")
        self.assertEqual(rows[0]["offer_kind"], "executor")

    def test_explicit_pool_pause_blocks_only_matching_candidate_and_preserves_qualification(self):
        self.qualify(pool="lium-active")
        self.qualify(PROFILE_IDS[3], "RTX PRO 6000 Blackwell", "targon", pool="targon-paused",
            launch=LaunchSpec("targon", "candidate", PRUNED, offer_id="rtx6000b-small"))
        self.publish("lium", [offer()])
        self.publish("targon", [offer("rtx6000b-small", "targon", "RTX PRO 6000 Blackwell")])
        actor = Principal("superdan", "browser", auth_mode="password")
        service = OperatorCapacity(self.repo, SimpleNamespace(operator_capacity_owners=("superdan",)), self.registry)
        service.update_policy(actor, {"expected_version":0, "enabled":True, "max_instances":4,
            "max_physical_gpus":4, "max_hourly_cost_microusd":4000000,
            "idle_shutdown_seconds":600, "max_ttl_seconds":7200})
        budget = copy.deepcopy(self.repo.get_budget("owner-budget"))
        for instances, gpus in ((0, 1), (1, 0), (0, 0)):
            with self.subTest(instances=instances, gpus=gpus):
                self.repo.configure_pool("targon-paused", max_instances=instances, max_physical_gpus=gpus)
                rows = self.project()["candidates"]
                self.assertEqual([row["provider"] for row in rows], ["lium", "targon"])
                self.assertEqual(rows[0]["blockers"], [])
                paused = rows[1]
                self.assertTrue(paused["deployment_qualified"])
                self.assertTrue(paused["specs_confirmed"])
                self.assertEqual(paused["execution_slots"], 1)
                self.assertEqual(paused["qualification"], "unqualified")
                self.assertEqual(paused["rank_reasons"][0], "deployment_qualified")
                self.assertEqual(paused["blockers"], ["operator_pool_paused"])
                with self.assertRaisesRegex(OperatorError, "operator_pool_paused"):
                    self.resolve(paused["selection"])
                preview = service.preview(actor, paused["selection"])
                self.assertFalse(preview["can_start"])
                self.assertEqual(preview["blockers"], [{"code":"operator_pool_paused"}])
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        self.assertEqual(self.repo.list_instance_intents(), [])
        self.repo.configure_pool("targon-paused", max_instances=1, max_physical_gpus=1)
        resumed = next(row for row in self.project()["candidates"] if row["provider"] == "targon")
        self.assertEqual(resumed["blockers"], [])
        self.assertEqual(self.resolve(resumed["selection"])["offer_id"], "rtx6000b-small")

    def test_missing_or_occupied_pool_is_not_projected_as_an_explicit_pause(self):
        self.qualify(pool="test")
        self.publish("lium", [offer()])
        self.assertEqual(self.project()["candidates"][0]["blockers"], [])
        self.repo.configure_pool("test", max_instances=1, max_physical_gpus=1)
        self.repo.reserve_instance_intent(self.scope, "test", "occupied", physical_gpus=1, slots=1,
            reserved_cost_microusd=100000, hard_deadline=self.now+3600,
            budget_account_ids=("owner-budget",), dry_run=False, provider="lium")
        row = self.project()["candidates"][0]
        self.assertEqual(row["blockers"], [])
        self.assertTrue(row["deployment_qualified"])
        self.assertEqual(self.resolve(row["selection"])["offer_id"], "executor-1")

    def test_binding_filters_and_full_worker_count_are_required_for_qualification(self):
        self.qualify(filters={"min_ram_gib":96, "min_disk_gib":128, "min_download_mbps":500,
                              "max_price_per_gpu_hour_microusd":800000, "allowed_countries":["US"]})
        self.publish("lium", [offer("blocked", price_per_gpu_hour_microusd=900000, download_mbps=None)])
        candidate = self.project()["candidates"][0]
        self.assertFalse(candidate["deployment_qualified"])
        self.assertFalse(candidate["specs_confirmed"])
        self.assertIsNone(candidate["execution_slots"])
        self.assertTrue({"inventory_unknown_bandwidth", "inventory_price_above_limit", "inventory_country_mismatch"}
                        <= set(candidate["blockers"]))
        with self.assertRaisesRegex(OperatorError, "inventory_unknown_bandwidth"):
            self.resolve(candidate["selection"])

    def test_every_rented_gpu_requires_an_approved_execution_slot(self):
        self.qualify(PROFILE_IDS[1], PRO, gpu_count=2, execution_slots=1)
        self.publish("lium", [offer("two", gpu=PRO, gpu_count=2, ram_gib=279, disk_gib=400)])
        candidate = self.project(BASE)["candidates"][0]
        self.assertEqual(candidate["qualification"], "unqualified")
        self.assertIsNone(candidate["execution_slots"])
        self.assertIn("operator_execution_slots_unqualified", candidate["blockers"])
        with self.assertRaisesRegex(OperatorError, "operator_execution_slots_unqualified"):
            self.resolve(candidate["selection"])

    def test_targon_sku_and_fixed_lium_executor_must_match_the_approved_launch(self):
        for provider, profile_id, gpu, identity in (("lium", PROFILE_IDS[0], "RTX 5090", "executor-1"),
                ("targon", PROFILE_IDS[3], "RTX PRO 6000 Blackwell", "rtx6000b-small")):
            with self.subTest(provider=provider):
                self.registry = OperatorRegistry(catalog=historical_catalog, qualified_providers=("lium", "targon"))
                self.qualify(profile_id, gpu, provider,
                    launch=LaunchSpec(provider, "candidate", PRUNED, offer_id=identity))
                self.publish("lium")
                self.publish("targon")
                self.publish(provider, [offer(identity, provider, gpu), offer("other", provider, gpu)])
                rows = {row["offer_id"]:row for row in self.project()["candidates"]}
                self.assertEqual(rows[identity]["qualification"], "qualified")
                self.assertEqual(self.resolve(rows[identity]["selection"])["offer_id"], identity)
                self.assertIn("operator_offer_selection_mismatch", rows["other"]["blockers"])
                self.assertFalse(rows["other"]["deployment_qualified"])
                with self.assertRaisesRegex(OperatorError, "operator_offer_selection_mismatch"):
                    self.resolve(rows["other"]["selection"])
                self.publish(provider, [offer(identity, provider, gpu, kind="bm")])
                wrong_kind = self.project()["candidates"][0]
                self.assertEqual(wrong_kind["qualification"], "unqualified")
                self.assertIn("operator_offer_selection_mismatch", wrong_kind["blockers"])
                with self.assertRaisesRegex(OperatorError, "operator_offer_selection_mismatch"):
                    self.resolve(wrong_kind["selection"])

    def test_fresh_failure_stale_missing_and_successful_empty_are_distinct(self):
        self.assertEqual(self.project()["reason_code"], "inventory_scan_unconfirmed")
        self.publish("lium", [offer()], observed=self.now-121)
        self.publish("targon", status="error")
        result = self.project()
        self.assertEqual([row["status"] for row in result["providers"]], ["stale", "error"])
        self.assertEqual(result["candidates"], [])
        self.publish("lium")
        self.publish("targon")
        self.assertEqual(self.project()["reason_code"], "inventory_no_matching_stock")
        self.assertEqual(self.project()["status"], "ok")

    def test_partial_executor_is_not_a_fake_available_slice_or_proof_of_absence(self):
        self.publish("lium", [offer("partial", gpu_count=8, available_count=0, available_gpu_count=4,
            min_gpu_count_for_rental=1, unverified_fields=["allocation"], hourly_cost_microusd=6000000)])
        self.publish("targon")
        result = self.project()
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["reason_code"], "inventory_specs_unconfirmed")

    def test_resolver_returns_exact_detached_row_and_rejects_changed_or_stale_stock(self):
        self.qualify()
        original = offer()
        self.publish("lium", [original])
        chosen = self.project()["candidates"][0]["selection"]
        self.assertEqual(self.resolve(chosen), original)
        for change, code in (({"offer_id":"other"}, "operator_offer_unavailable"),
                ({"gpu_count":2}, "operator_offer_selection_mismatch"),
                ({"gpu_type":PRO}, "operator_offer_selection_mismatch"),
                ({"node_count":2}, "operator_offer_quantity_invalid")):
            with self.subTest(change=change), self.assertRaisesRegex(OperatorError, code):
                self.resolve({**chosen, **change})
        self.now += 121
        with self.assertRaisesRegex(OperatorError, "operator_offer_observation_unavailable"):
            self.resolve(chosen)

    def test_resolver_rechecks_specs_quantity_approval_and_ttl(self):
        self.qualify(min_ttl_seconds=3780)
        self.publish("lium", [offer()])
        choice = self.project()["candidates"][0]["selection"]
        for changes, code in (({"available_count":0}, "operator_offer_unavailable"),
                ({"ram_gib":64}, "inventory_ram_below_minimum"),
                ({"ram_gib":None}, "inventory_unknown_ram"),
                ({"unverified_fields":["ram_gib"]}, "inventory_unknown_ram"),
                ({"hourly_cost_microusd":3000000}, "inventory_price_above_limit"),
                ({"unverified_fields":["allocation"]}, "inventory_unknown_allocation")):
            self.publish("lium", [offer(**changes)])
            with self.subTest(changes=changes), self.assertRaisesRegex(OperatorError, code):
                self.resolve(choice)
        self.publish("lium", [offer()])
        with self.assertRaisesRegex(OperatorError, "operator_ttl_below_provider_minimum"):
            self.resolve({**choice, "ttl_seconds":120})
        binding = next(iter(self.registry.bindings.values()))
        self.registry.bindings[binding.binding_id] = replace(binding, enabled=False)
        with self.assertRaisesRegex(OperatorError, "operator_deployment_not_qualified"):
            self.resolve(choice)

    def test_ambiguous_offer_ids_never_select_first_row(self):
        self.qualify()
        self.publish("lium", [offer()])
        choice = self.project()["candidates"][0]["selection"]
        self.publish("lium", [offer(), offer(hourly_cost_microusd=800000)])
        result = self.project()
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["providers"][0]["reason_code"], "inventory_offer_ambiguous")
        with self.assertRaisesRegex(OperatorError, "operator_offer_observation_unavailable"):
            self.resolve(choice)

    def test_http_is_authenticated_bounded_no_store_and_read_only(self):
        self.publish("lium", [offer()])
        service = OperatorCapacity(self.repo, SimpleNamespace(operator_capacity_owners=("superdan",)), self.registry)
        app, actors = FastAPI(), [Principal("superdan", "browser", auth_mode="password")]
        @app.middleware("http")
        async def principal(request: Request, call_next):
            request.state.principal = actors[0]
            return await call_next(request)
        register_routes(app, service=service)
        budget = copy.deepcopy(self.repo.get_budget("owner-budget"))
        query = {"model_id":PRUNED, "mode":"ref", "ttl_seconds":7200}
        with patch("studio_platform.lium_provider._central_loader", side_effect=AssertionError("credential load")), \
             patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("network")), TestClient(app) as client:
            response = client.get("/v1/operator/capacity/candidates", params=query)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.json()["candidates"][0]["selection"]["offer_id"], "executor-1")
            for invalid in ({"model_id":"unknown"}, {"mode":"other"}, {"ttl_seconds":119}, {"ttl_seconds":14401}):
                self.assertEqual(client.get("/v1/operator/capacity/candidates", params={**query, **invalid}).status_code, 422)
            actors[0] = Principal("supervan", "browser", auth_mode="password")
            self.assertEqual(client.get("/v1/operator/capacity/candidates", params=query).status_code, 403)
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
