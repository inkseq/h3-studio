"""Finite pilot guard tests. Fake credentials, HTTP and provider time only."""
import base64
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import httpx
from studio_platform.lium_provider import LiumError
from tools.h3_5090_pilot_cloud import (
    ExactPilotProvider, Pilot, PilotError, POLICY_5090, POLICY_PRO6000, number, qualify, public_rows,
)

EXECUTOR = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SECOND = "abababab-abab-4bab-8bab-abababababab"
POD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
TEMPLATE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PUBLIC_KEY = "ssh-ed25519 " + base64.b64encode(
    (11).to_bytes(4, "big") + b"ssh-ed25519" + (32).to_bytes(4, "big") + b"x"*32).decode()


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def node(executor=EXECUTOR, **changes):
    return {"id": executor, "gpu_model": "RTX 5090", "gpu_count": 1,
        "available_gpu_count": 1, "min_rentable_gpu_count": 1,
        "gpu_memory_gb": 32, "cpu_count": 18, "ram_gb": 110,
        "disk_free_gb": 500, "network_download_mbps": 900,
        "price_per_gpu_hour": "0.75", **changes}


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.now = 1000.0
        self.nodes = [node()]
        self.pods = []
        self.calls = []
        self.removed = False
        self.rent_timeout = False
        self.delete_timeout = False
        self.reset_ttl = False
        self.auth_gpu = "NVIDIA GeForce RTX 5090"
        self.auth_capacity = 32607
        self.expected_gpu_count = 1
        self.auth_price = "0.75"
        self.folder = Path(self.temp.name).resolve() / "pilot"
        self.pilot = Pilot(self.folder, fetch=self.feed, factory=self.factory, clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def feed(self):
        return {"generated_at": stamp(self.now), "nodes": self.nodes}

    def factory(self, **kwargs):
        def loader(service, *, profile):
            return SimpleNamespace(service="lium", profile="lium--rig-root",
                base_url="https://lium.io/api", primary_key_variable="LIUM_API_KEY",
                api_key="fake-key-for-offline-tests")
        return ExactPilotProvider(**kwargs, loader=loader, transport=httpx.MockTransport(self.http))

    def http(self, request):
        body = json.loads(request.content) if request.content else None
        route = request.url.path.removeprefix("/api/")
        self.calls.append((request.method, route, body))
        if request.method == "GET":
            if route == "pods":
                return httpx.Response(200, json=[] if self.removed else self.pods)
            if route == "executors":
                return httpx.Response(200, json=[{"id": n["id"], "gpu_count": n["gpu_count"],
                    "available_gpu_count": n["available_gpu_count"], "price_per_gpu": self.auth_price,
                    "specs": {"gpu": {"details": [{"name": self.auth_gpu, "capacity": self.auth_capacity}
                                                   for _ in range(n["gpu_count"])]}}}
                    for n in self.nodes])
            if route == "templates":
                return httpx.Response(200, json=[{"id": TEMPLATE, "name": "offline-template"}])
            if route == "pods/" + POD:
                return httpx.Response(200, json=self.pods[0])
            if route == "pods/" + POD + "/statement":
                if not self.removed:
                    return httpx.Response(404)
                return httpx.Response(200, json={"pod_id": POD, "pod_name": self.pods[0]["name"],
                    "created_at": stamp(1000), "removed_at": stamp(self.now),
                    "removed": True, "total": "0.012345", "billed_seconds": self.now-1000})
        if request.method == "POST" and route.endswith("/rent"):
            # Both durable records must predate the sole mutating rent request.
            state = json.loads((self.folder / "pilot.json").read_text())
            record = state["records"][-1]
            marker = json.loads((self.folder / "rent-journal" / (record["tag"] + ".json")).read_text())
            self.assertEqual(marker["phase"], "post_started")
            self.assertEqual(body["termination_hours"], 2)
            self.assertEqual(body["gpu_count"], self.expected_gpu_count)
            self.pods = [{"id": POD, "name": body["pod_name"], "status": "PENDING",
                "created_at": stamp(self.now), "removal_scheduled_at": stamp(self.now+7300),
                "termination_hours": 2, "executor": {"id": EXECUTOR, "executor_ip_address": "8.8.8.8"},
                "ports_mapping": {"22": 12345}}]
            if self.rent_timeout:
                raise httpx.ReadTimeout("fake upstream secret must not escape")
            return httpx.Response(200, json={"success": True, "pod_id": POD})
        if request.method == "POST" and route.endswith("/schedule-removal"):
            self.pods[0]["removal_scheduled_at"] = body["removal_scheduled_at"]
            return httpx.Response(200, json={"success": True})
        if request.method == "DELETE":
            state = json.loads((self.folder / "pilot.json").read_text())
            self.assertIn("destroy_started_at", state["records"][0])
            if self.delete_timeout:
                raise httpx.ReadTimeout("fake delete timeout")
            self.removed = True
            return httpx.Response(200, json={"success": True})
        raise AssertionError((request.method, route))

    def rent(self):
        return self.pilot.rent(EXECUTOR, TEMPLATE, PUBLIC_KEY)

    def count(self, method, suffix):
        return sum(m == method and route.endswith(suffix) for m, route, _ in self.calls)

    def test_rent_records_original_two_hour_ttl_and_waits_for_running_to_verify(self):
        result = self.rent()
        self.assertFalse(result["ttl_verified"])
        self.assertEqual(result["deadline"], 8200)
        self.assertEqual(result["collection_deadline"], 7600)
        self.assertEqual(self.count("POST", "/rent"), 1)
        self.assertEqual(self.count("POST", "/schedule-removal"), 0)
        self.now += 120
        self.pods[0].update(status="RUNNING", removal_scheduled_at=stamp(self.now+7200))
        verified = self.pilot.ensure_ttl(result["tag"])
        self.assertTrue(verified["ttl_verified"])
        self.assertEqual(verified["deadline"], 8200)
        self.assertEqual(verified["collection_deadline"], 7600)
        self.assertEqual(self.count("POST", "/schedule-removal"), 1)
        self.assertTrue(self.rent()["replay_refused"])
        self.assertEqual(self.count("POST", "/rent"), 1)
        self.assertNotIn("fake-key", (self.folder / "pilot.json").read_text())

    def test_exact_candidate_failure_never_falls_back_to_cheap_other_node(self):
        self.nodes = [node(ram_gb=94), node(SECOND)]
        with self.assertRaisesRegex(PilotError, "outside_limits"):
            self.rent()
        self.assertEqual(self.count("POST", "/rent"), 0)
        self.assertFalse((self.folder / "pilot.json").exists())

    def test_authenticated_gpu_or_price_mismatch_blocks_before_post(self):
        for gpu, price in (("NVIDIA RTX 4090", "0.75"), ("NVIDIA GeForce RTX 5090", "0.86")):
            self.auth_gpu, self.auth_price = gpu, price
            with self.assertRaises(PilotError):
                self.rent()
        self.assertEqual(self.count("POST", "/rent"), 0)

    def test_numeric_json_price_is_provider_decimal_and_selects_exact_node(self):
        self.auth_price = 0.75  # provider JSON decoder uses parse_float=Decimal
        provider = self.factory(enabled=True, fetch=self.feed, clock=lambda: self.now)
        try:
            value = provider._rows("executors?available=true")[0]["price_per_gpu"]
            self.assertIsInstance(value, Decimal)
            self.assertEqual(number(value), Decimal("0.75"))
        finally:
            provider.close()
        result = self.rent()
        self.assertFalse(result["ttl_verified"])
        self.pods[0]["status"] = "RUNNING"
        self.assertTrue(self.pilot.ensure_ttl(result["tag"])["ttl_verified"])
        self.assertEqual(self.count("POST", "/rent"), 1)
        for invalid in (Decimal("NaN"), Decimal("Infinity"), Decimal("-0.01"), True):
            with self.assertRaises(PilotError):
                number(invalid)

    def test_candidate_change_after_preflight_is_proven_not_submitted(self):
        original = self.feed
        count = 0
        def changing():
            nonlocal count
            count += 1
            if count > 1:
                self.nodes[0]["ram_gb"] = 64
            return original()
        self.pilot.fetch = changing
        result = self.rent()
        self.assertEqual(result["phase"], "not_created")
        self.assertEqual(result["actual_cost_microusd"], 0)
        self.assertEqual(self.count("POST", "/rent"), 0)

    def test_rent_timeout_reconciles_original_identity_without_replay(self):
        self.rent_timeout = True
        result = self.rent()
        self.assertEqual(result["pod_id"], POD)
        self.assertFalse(result["ttl_verified"])
        before = len(self.calls)
        self.pilot.reconcile(result["tag"])
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls[before:]))
        self.assertTrue(self.rent()["replay_refused"])
        self.assertEqual(self.count("POST", "/rent"), 1)
        with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
            self.pilot.ensure_ttl(result["tag"])
        self.assertEqual(self.count("POST", "/schedule-removal"), 0)
        self.pods[0]["status"] = "RUNNING"
        self.pilot.ensure_ttl(result["tag"])
        self.assertTrue(self.pilot.read()["records"][0]["ttl_verified"])

    def test_unknown_rent_not_found_stays_held_even_with_empty_inventory(self):
        self.rent_timeout = True
        result = self.rent()
        self.pods = []
        value = self.pilot.reconcile(result["tag"])["records"][0]
        self.assertEqual(value["phase"], "unknown")
        self.assertIsNone(value["actual_cost_microusd"])
        self.nodes.append(node(SECOND))
        with self.assertRaisesRegex(PilotError, "unknown_outcome_hold"):
            self.pilot.rent(SECOND, TEMPLATE, PUBLIC_KEY)

    def test_connection_is_get_only_and_requires_verified_ttl(self):
        result = self.rent()
        self.pods[0]["status"] = "RUNNING"
        before = len(self.calls)
        with self.assertRaisesRegex(PilotError, "not_ready"):
            self.pilot.reconcile(result["tag"], connection=True)
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls[before:]))
        self.assertEqual(self.count("POST", "/schedule-removal"), 0)
        self.pilot.ensure_ttl(result["tag"])
        before = len(self.calls)
        value = self.pilot.reconcile(result["tag"], connection=True)
        self.assertEqual(value["port"], 12345)
        self.assertFalse(value["allocation_verified"])
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls[before:]))
        self.pods[0]["removal_scheduled_at"] = stamp(8400)
        with self.assertRaisesRegex(PilotError, "not_ready"):
            self.pilot.reconcile(result["tag"], connection=True)
        with self.assertRaisesRegex(LiumError, "absolute_ttl_unconfirmed"):
            self.pilot.ensure_ttl(result["tag"])
        self.assertFalse(self.pilot.read()["records"][0]["ttl_verified"])
        self.assertEqual(self.count("POST", "/schedule-removal"), 1)

    def test_destroy_once_and_only_statement_settles(self):
        result = self.rent()
        self.now = 1050
        with self.assertRaises(PilotError):
            self.pilot.destroy(result["tag"], idle_confirmed=False)
        value = self.pilot.destroy(result["tag"], idle_confirmed=True)
        self.assertEqual(value["phase"], "destroyed")
        self.assertEqual(value["actual_cost_microusd"], 12345)
        self.pilot.destroy(result["tag"], idle_confirmed=True)
        self.assertEqual(self.count("DELETE", POD), 1)

    def test_delete_timeout_never_replays_after_restart_or_frees_new_rental(self):
        result = self.rent()
        self.delete_timeout = True
        self.pilot.destroy(result["tag"], idle_confirmed=True)
        restarted = Pilot(self.folder, fetch=self.feed, factory=self.factory, clock=lambda: self.now)
        self.assertTrue(restarted.destroy(result["tag"], idle_confirmed=True)["replay_refused"])
        self.assertEqual(self.count("DELETE", POD), 1)
        self.nodes.append(node(SECOND))
        with self.assertRaisesRegex(PilotError, "unknown_outcome_hold"):
            restarted.rent(SECOND, TEMPLATE, PUBLIC_KEY)

    def test_other_sixnine_pod_blocks_new_pilot(self):
        self.pods = [{"id": POD, "name": "sixnine-existing-production", "status": "RUNNING"}]
        with self.assertRaisesRegex(PilotError, "existing_sixnine"):
            self.rent()
        self.assertEqual(self.count("POST", "/rent"), 0)

    def test_previous_actual_cost_keeps_five_dollar_cap(self):
        result = self.rent()
        self.now += 10
        self.pilot.destroy(result["tag"], idle_confirmed=True)
        state = self.pilot.read()
        state["records"][0]["actual_cost_microusd"] = 3_400_000
        self.pilot.save(state)
        self.nodes.append(node(SECOND))
        before = len(self.calls)
        with self.assertRaisesRegex(PilotError, "budget"):
            self.pilot.rent(SECOND, TEMPLATE, PUBLIC_KEY)
        self.assertEqual(len(self.calls), before)

    def test_known_running_pod_with_unverified_ttl_blocks_second_rental(self):
        self.rent_timeout = True
        self.rent()
        self.nodes.append(node(SECOND))
        with self.assertRaisesRegex(PilotError, "unknown_outcome_hold"):
            self.pilot.rent(SECOND, TEMPLATE, PUBLIC_KEY)
        self.assertEqual(self.count("POST", "/rent"), 1)

    def test_prior_verified_ttl_drift_is_refreshed_before_second_rental(self):
        original = self.rent()
        self.assertFalse(original["ttl_verified"])
        self.pods[0]["status"] = "RUNNING"
        original = self.pilot.ensure_ttl(original["tag"])
        self.assertTrue(original["ttl_verified"])
        self.assertEqual(original["phase"], "running")
        self.now += 60
        self.pods[0].update(status="RUNNING", removal_scheduled_at=stamp(self.now + 7200))
        self.nodes.append(node(SECOND))
        before = len(self.calls)
        restarted = Pilot(self.folder, fetch=self.feed, factory=self.factory, clock=lambda: self.now)
        with self.assertRaisesRegex(PilotError, "unknown_outcome_hold"):
            restarted.rent(SECOND, TEMPLATE, PUBLIC_KEY)
        self.assertTrue(self.calls[before:])
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls[before:]))
        self.assertEqual(self.count("POST", "/rent"), 1)
        self.assertEqual(self.count("POST", "/schedule-removal"), 1)
        records = restarted.read()["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tag"], original["tag"])
        self.assertEqual(records[0]["deadline"], original["deadline"])
        self.assertEqual(records[0]["collection_deadline"], original["collection_deadline"])
        self.assertEqual(records[0]["phase"], "running")
        self.assertFalse(records[0]["ttl_verified"])

    def test_inventory_units_missing_fields_and_staleness_fail_closed(self):
        self.assertIn("ram_below_96_gib", qualify(node(ram_gb=96)))
        self.assertIn("free_disk_below_250_gib", qualify(node(disk_free_gb=250)))
        self.assertIn("unverified_ram_gb", qualify(node(ram_gb=None)))
        self.assertIn("not_single_gpu_host", qualify(node(gpu_count=2)))
        with self.assertRaisesRegex(PilotError, "stale"):
            public_rows({"generated_at": stamp(1), "nodes": []}, 1000)
        self.assertEqual(self.pilot.inventory()["candidates"][0]["rejections"], [])
        self.assertEqual(self.calls, [])


class Pro6000PolicyTests(unittest.TestCase):
    tearDown = PilotTests.tearDown
    feed = PilotTests.feed
    factory = PilotTests.factory
    http = PilotTests.http
    rent = PilotTests.rent
    count = PilotTests.count

    def setUp(self):
        PilotTests.setUp(self)
        self.nodes = [node(gpu_model="RTX PRO 6000 Blackwell Server Edition", gpu_count=2,
            available_gpu_count=2, min_rentable_gpu_count=2, gpu_memory_gb=96,
            ram_gb=283, disk_free_gb=1269, network_download_mbps=2300, price_per_gpu_hour=1.19)]
        self.auth_gpu = "NVIDIA RTX PRO 6000 Blackwell Server Edition"
        self.auth_capacity = 97887
        self.auth_price = 1.19
        self.expected_gpu_count = 2
        self.pilot = self.restart()

    def restart(self, policy="pro6000-comparison"):
        return Pilot(self.folder, policy=policy, fetch=self.feed, factory=self.factory, clock=lambda: self.now)

    def test_dual_card_manifest_reserves_eight_dollars_and_original_two_hour_ttl(self):
        result = self.rent()
        self.assertEqual(result["gpu_count"], 2)
        self.assertEqual(result["reservation_microusd"], 8_000_000)
        self.assertEqual(result["deadline"], 8200)
        self.assertFalse(result["ttl_verified"])
        self.assertEqual(self.count("POST", "/schedule-removal"), 0)
        self.pods[0]["status"] = "RUNNING"
        verified = self.pilot.ensure_ttl(result["tag"])
        self.assertTrue(verified["ttl_verified"])
        self.assertEqual((verified["deadline"], verified["reservation_microusd"]), (8200, 8_000_000))
        state = self.pilot.read()
        self.assertEqual(state["policy"], POLICY_PRO6000.snapshot())
        provider = self.pilot.provider(state["records"][0], PUBLIC_KEY)
        try:
            manifest = next(iter(provider._manifests.values()))
            self.assertEqual(manifest.max_price_per_gpu_hour_microusd, 2_000_000)
            self.assertEqual((manifest.gpu_count, manifest.execution_slots, manifest.termination_hours), (2, 2, 2))
            self.assertEqual(manifest.max_price_per_gpu_hour_microusd * manifest.gpu_count
                             * manifest.termination_hours, result["reservation_microusd"])
            with self.assertRaisesRegex(PilotError, "manifest_policy_mismatch"):
                provider._select_offer(replace(manifest, gpu_count=1, execution_slots=1))
        finally:
            provider.close()
        self.assertTrue(self.restart().rent(EXECUTOR, TEMPLATE, PUBLIC_KEY)["replay_refused"])
        with self.assertRaisesRegex(PilotError, "rental_count_limit"):
            self.restart().rent(SECOND, TEMPLATE, PUBLIC_KEY)
        self.assertEqual(self.count("POST", "/rent"), 1)

    def test_pro_filters_exact_model_topology_capacity_units_and_per_card_price(self):
        good = self.nodes[0]
        self.assertEqual(qualify(good, POLICY_PRO6000), [])
        for name in POLICY_PRO6000.gpu_names:
            self.assertEqual(qualify({**good, "gpu_model": name}, POLICY_PRO6000), [])
        for changes in ({"gpu_model": "RTX 6000 Ada"}, {"gpu_model": "RTX PRO 6000 Blackwell"},
                        {"gpu_count": 1}, {"gpu_count": 4}, {"min_rentable_gpu_count": 1},
                        {"available_gpu_count": 1}, {"gpu_memory_gb": 80}, {"ram_gb": 256},
                        {"disk_free_gb": 350}, {"cpu_count": 11}, {"network_download_mbps": 499},
                        {"price_per_gpu_hour": "2.000001"}):
            with self.subTest(changes=changes):
                self.assertTrue(qualify({**good, **changes}, POLICY_PRO6000))
        self.assertEqual(qualify({**good, "price_per_gpu_hour": 2}, POLICY_PRO6000), [])
        self.assertTrue(qualify(good, POLICY_5090))
        self.assertEqual(self.pilot.inventory()["candidates"][0]["rejections"], [])
        self.assertEqual(self.calls, [])

    def test_authenticated_card_identity_capacity_and_price_fail_before_rent(self):
        for gpu, capacity, price in (("NVIDIA RTX 6000 Ada Generation", 97887, 1.19),
                                    (self.auth_gpu, 81920, 1.19), (self.auth_gpu, 97887, 2.01)):
            self.auth_gpu, self.auth_capacity, self.auth_price = gpu, capacity, price
            with self.subTest(gpu=gpu, capacity=capacity, price=price), self.assertRaises(PilotError):
                self.rent()
        self.assertEqual(self.count("POST", "/rent"), 0)
        self.assertFalse((self.folder / "pilot.json").exists())

    def test_policy_is_bound_to_directory_and_snapshot_cannot_change(self):
        self.rent()
        before = len(self.calls)
        with self.assertRaisesRegex(PilotError, "policy_mismatch"):
            self.restart("5090").rent(SECOND, TEMPLATE, PUBLIC_KEY)
        state = self.pilot.read()
        state["policy"]["budget_microusd"] += 1
        with self.assertRaisesRegex(PilotError, "policy_mismatch"):
            self.pilot.save(state)
        state = self.pilot.read()
        self.assertEqual(state["policy"], POLICY_PRO6000.snapshot())
        with self.assertRaisesRegex(PilotError, "policy_mismatch"):
            self.restart("5090").save({"version": 2, "purpose": POLICY_5090.purpose,
                                      "policy": POLICY_5090.snapshot(), "records": []})
        self.assertEqual(len(self.calls), before)
        self.assertEqual(len(self.pilot.read()["records"]), 1)

    def test_legacy_5090_journal_remains_readable_but_cannot_be_reused_for_pro(self):
        legacy = {"version": 1, "purpose": POLICY_5090.purpose, "records": []}
        original = self.restart("5090")
        original.save(legacy)
        self.assertEqual(original.read(), legacy)
        with self.assertRaisesRegex(PilotError, "policy_mismatch"):
            self.pilot.rent(EXECUTOR, TEMPLATE, PUBLIC_KEY)
        self.assertEqual(self.calls, [])

    def test_pro_ttl_drift_requires_explicit_shortening_without_renewal_or_new_rent(self):
        result = self.rent()
        self.now += 60
        self.pods[0].update(status="RUNNING", removal_scheduled_at=stamp(self.now + 7200))
        before = len(self.calls)
        record = self.restart().reconcile(result["tag"])["records"][0]
        self.assertFalse(record["ttl_verified"])
        self.assertEqual(record["deadline"], result["deadline"])
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls[before:]))
        fixed = self.restart().ensure_ttl(result["tag"])
        self.assertTrue(fixed["ttl_verified"])
        self.assertEqual(fixed["deadline"], result["deadline"])
        self.assertLessEqual(self.pilot.read()["records"][0]["verified_removal_at"], result["deadline"])
        self.assertEqual(self.count("POST", "/rent"), 1)

    def test_unknown_dual_card_post_keeps_full_reservation_and_cannot_replay(self):
        self.rent_timeout = True
        result = self.rent()
        self.assertIsNone(result["actual_cost_microusd"])
        self.assertEqual(result["reservation_microusd"], 8_000_000)
        self.assertFalse(result["ttl_verified"])
        before = len(self.calls)
        self.assertTrue(self.restart().rent(EXECUTOR, TEMPLATE, PUBLIC_KEY)["replay_refused"])
        with self.assertRaisesRegex(PilotError, "rental_count_limit"):
            self.restart().rent(SECOND, TEMPLATE, PUBLIC_KEY)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.count("POST", "/rent"), 1)
        self.assertEqual(len(self.pilot.read()["records"]), 1)


if __name__ == "__main__":
    unittest.main()
