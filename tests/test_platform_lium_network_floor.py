"""Optional bandwidth selection uses fake HTTP; no cloud or speed benchmark."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import httpx

from studio_platform.capacity import CAPACITY_WAIT_CODES
from studio_platform.lium_provider import BASE_URL, LiumError, LiumManifest, LiumProvider, PROFILE
from studio_platform.repository import request_hash
from test_platform_lium_provider import MockAPI, EXECUTOR, OTHER, POD, TAG, TEMPLATE, launch, manifest
from test_platform_production_scaler import as_json, configuration


class NetworkFloorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = MockAPI()
        self.now = 1000
        self.providers = []
        self.addCleanup(lambda: [provider.close() for provider in self.providers])

    def provider(self, **fields):
        config = SimpleNamespace(service="lium", profile=PROFILE, base_url=BASE_URL,
            primary_key_variable="LIUM_API_KEY", api_key="offline-test-key-never-real")
        provider = LiumProvider(enabled=True, manifests=(manifest(**fields),),
            loader=lambda *a, **kw: config, transport=httpx.MockTransport(self.api), clock=lambda: self.now,
            journal_dir=Path(self.temp.name)/str(len(self.providers)))
        self.providers.append(provider)
        return provider

    def server_provider(self, **fields):
        return self.provider(executor_id="", compatible_gpu_names=("NVIDIA H200",), minimum_vram_mib=140000,
            server_side_selection=True, **fields)

    def spec_response(self, request):
        if request.url.path != "/api/executors/rent-by-spec":
            return None
        body = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "dry_run": body["dry_run"],
            "pod_id": None if body["dry_run"] else POD, "template_id": TEMPLATE,
            "price_per_hour": 1.5, "selected_executor": {"id": OTHER}})

    def test_only_positive_finite_numeric_floors_are_accepted(self):
        for value in (0, -1, True, False, "2000", [], {}, float("nan"), float("inf"), -float("inf"), 10**1000):
            with self.subTest(value=repr(value)[:50]), self.assertRaisesRegex(LiumError, "invalid_download_floor"):
                manifest(min_download_mbps=value)
        for value in (None, 1, 2000.5):
            self.assertEqual(manifest(min_download_mbps=value).min_download_mbps, value)
        self.assertEqual(self.api.calls, [])

    def test_listing_preserves_existing_filter_and_rechecks_at_actual_creation(self):
        queries = []
        def listing(request):
            if request.url.path == "/api/executors":
                queries.append(dict(request.url.params))
                return httpx.Response(200, json=[{"id": EXECUTOR, "gpu_count": 2, "price_per_gpu": "0.75",
                    "specs": {"gpu": {"details": [{"name": "NVIDIA H200", "capacity": 141000}]}}}])
        self.api.hook = listing
        provider = self.provider(compatible_gpu_names=("NVIDIA H200",), minimum_vram_mib=140000,
            min_download_mbps=2000.5)
        self.assertIsNone(provider.preflight_availability(launch()))
        self.assertEqual(provider.create(TAG, launch(), hard_deadline=8300).instance_id, POD)
        self.assertEqual(queries, [{"available": "true", "min_download_mbps": "2000.5"}]*2)
        self.assertEqual(self.api.count("POST"), 1)

    def test_exact_executor_filter_does_not_fallback_on_missing_telemetry(self):
        queries = []
        def listing(request):
            if request.url.path == "/api/executors":
                queries.append(dict(request.url.params))
                return httpx.Response(200, json=[])  # Provider excluded unknown/slow measurements.
        self.api.hook = listing
        provider = self.provider(min_download_mbps=2000)
        self.assertEqual(provider.preflight_availability(launch()), "provider_inventory_unavailable")
        with self.assertRaisesRegex(LiumError, "unavailable"):
            provider.create(TAG, launch(), hard_deadline=8300)
        self.assertEqual(queries, [{"min_download_mbps": "2000"}]*2)
        self.assertEqual(self.api.count("POST"), 0)

    def test_scientific_notation_is_encoded_as_the_same_finite_number(self):
        queries = []
        def listing(request):
            if request.url.path == "/api/executors":
                queries.append(request.url.params["min_download_mbps"])
                return httpx.Response(200, json=[])
        self.api.hook = listing
        self.assertEqual(self.provider(min_download_mbps=1e20).preflight_availability(launch()),
                         "provider_inventory_unavailable")
        self.assertEqual(queries, ["1e+20"])

    def test_spec_floor_is_identical_in_dry_run_and_single_paid_request(self):
        self.api.hook = self.spec_response
        provider = self.server_provider(min_download_mbps=2000.5)
        self.assertEqual(provider.create(TAG, launch(offer_id=""), hard_deadline=8300).instance_id, POD)
        payloads = [body for method, path, body in self.api.calls if method == "POST"]
        self.assertEqual([body["dry_run"] for body in payloads], [True, False])
        self.assertEqual([body["min_download_mbps"] for body in payloads], [2000.5, 2000.5])
        self.assertEqual(payloads[-1]["termination_hours"], 2)
        self.assertEqual(payloads[-1]["max_price_per_gpu_hour"], 1.0)

    def test_spec_inventory_failure_keeps_safe_wait_code_without_lowering_floor(self):
        self.api.hook = lambda req: httpx.Response(409, json={"success": False,
            "code": "no_executor_matches_spec", "constraint": "min_download_mbps",
            "message": "untrusted provider details"}) if req.method == "POST" else None
        provider = self.server_provider(min_download_mbps=2000)
        reason = provider.preflight_availability(launch(offer_id=""))
        self.assertEqual(reason, "provider_inventory_unavailable")
        self.assertEqual(CAPACITY_WAIT_CODES[reason], "capacity_no_matching_gpu")
        with self.assertRaisesRegex(LiumError, "unavailable"):
            provider.create(TAG, launch(offer_id=""), hard_deadline=8300)
        payloads = [body for method, path, body in self.api.calls if method == "POST"]
        self.assertTrue(all(body["dry_run"] and body["min_download_mbps"] == 2000 for body in payloads))
        self.assertEqual(len(payloads), 2)

    def test_unconfirmed_inventory_is_not_reported_as_no_stock(self):
        self.api.hook = lambda req: httpx.Response(503, json={"message": "secret upstream detail"})
        reason = self.server_provider(min_download_mbps=2000).preflight_availability(launch(offer_id=""))
        self.assertEqual(reason, "provider_inventory_unconfirmed")
        self.assertEqual(CAPACITY_WAIT_CODES[reason], "capacity_inventory_check_failed")

    def test_absent_floor_keeps_historical_http_shapes(self):
        queries = []
        def capture(request):
            if request.url.path == "/api/executors":
                queries.append(dict(request.url.params))
            return self.spec_response(request)
        self.api.hook = capture
        self.assertIsNone(self.provider().preflight_availability(launch()))
        self.assertIsNone(self.server_provider().preflight_availability(launch(offer_id="")))
        self.assertEqual(queries, [{}])
        self.assertTrue(all("min_download_mbps" not in body for method, path, body in self.api.calls if method == "POST"))

    def test_loading_absent_field_keeps_legacy_configuration_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            old = configuration(Path(directory))
            documents = [{k: v for k, v in row.items() if k != "min_download_mbps"} for row in old.manifests]
            old = replace(old, manifests=documents)
            expected = request_hash(as_json(old))
            reloaded = replace(old, manifests=json.loads(json.dumps(documents)))
            self.assertEqual(reloaded.fingerprint(), expected)
            self.assertTrue(all(LiumManifest(**row).min_download_mbps is None for row in reloaded.manifests))
            changed = replace(old, manifests=[dict(row, min_download_mbps=2000) for row in documents])
            self.assertNotEqual(changed.fingerprint(), expected)


if __name__ == "__main__":
    unittest.main()
