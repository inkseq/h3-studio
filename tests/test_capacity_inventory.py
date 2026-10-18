"""Offline stock parsing, unit, identity, completeness and secret-boundary tests."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from studio_platform import capacity_inventory as subject


def targon():
    return {"name": "rtx6000b-small", "type": "vm", "gpu": True,
        "spec": {"gpu_model": "RTX-PRO-6000B", "gpu_count": 1, "cpu_millicores": 32000,
                 "memory_mib": 128000, "disk_size_mib": 327680},
        "cost_per_hour": 1.69, "available": 9, "private_provider_detail": "must-not-escape"}


def lium():
    return {"id": "11111111-1111-1111-1111-111111111111", "gpu_count": 2,
        "available_gpu_count": 2, "price_per_gpu": "0.75", "min_gpu_count_for_rental": 1,
        "is_whole_host_free": True, "has_no_pending_rental": True,
        "specs": {"gpu": {"details": [{"name": "NVIDIA GeForce RTX 5090"}] * 2},
                  "ram": {"total": "134217728.0"}, "hard_disk": {"free": 167772160},
                  "cpu": {"count": 24}, "network": {"download_speed": 99999}},
        "effective_download_speed_mbps": "200.5", "location": {"country_code": "AU"},
        "executor_ip_address": "private-address-must-not-escape"}


def loader(service, *, profile):
    assert (service, profile) == ("lium", "lium--rig-root")
    return SimpleNamespace(service=service, profile=profile, base_url="https://lium.io/api",
        primary_key_variable="LIUM_API_KEY", api_key="test-secret-must-not-escape")


class InventoryTests(unittest.TestCase):
    def scan(self, rows, *, provider="targon", status=200, headers=None):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(status, json=rows, headers=headers)
        kwargs = {"transport": httpx.MockTransport(respond), "clock": lambda: 12345}
        result = (subject.scan_targon(**kwargs) if provider == "targon"
                  else subject.scan_lium(loader, **kwargs))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(str(requests[0].url), subject.TARGON_URL if provider == "targon" else subject.LIUM_URL)
        self.assertEqual(result["observed_at"], 12345)
        return result, requests[0]

    def test_targon_vm_units_exact_price_and_unknowns_are_public(self):
        result, request = self.scan([targon()])
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["reason_code"])
        self.assertNotIn("X-API-Key", request.headers)
        offer = result["offers"][0]
        self.assertEqual((offer["gpu_type"], offer["gpu_count"], offer["available_count"]),
                         ("RTX PRO 6000 Blackwell", 1, 9))
        self.assertEqual((offer["ram_gib"], offer["disk_gib"], offer["cpu_cores"]), (125, 320, 32))
        self.assertEqual(offer["hourly_cost_microusd"], 1690000)
        self.assertEqual(offer["unverified_fields"], ["country", "download_mbps", "gpu_edition"])
        self.assertNotIn("must-not-escape", json.dumps(result))

    def test_lium_whole_node_prices_kib_resources_and_effective_bandwidth(self):
        result, request = self.scan([lium()], provider="lium")
        self.assertEqual(request.headers["X-API-Key"], "test-secret-must-not-escape")
        self.assertEqual(result["status"], "ok")
        offer = result["offers"][0]
        self.assertEqual((offer["ram_gib"], offer["disk_gib"], offer["cpu_cores"]), (128, 160, 24))
        self.assertEqual((offer["hourly_cost_microusd"], offer["price_per_gpu_hour_microusd"]), (1500000, 750000))
        self.assertEqual((offer["gpu_count"], offer["available_count"], offer["download_mbps"]), (2, 1, 200.5))
        self.assertEqual((offer["available_gpu_count"], offer["min_gpu_count_for_rental"]), (2, 1))
        self.assertEqual(offer["unverified_fields"], [])
        self.assertNotIn("must-not-escape", json.dumps(result))

    def test_missing_optional_measurements_are_not_fabricated(self):
        row = lium()
        row.pop("effective_download_speed_mbps")
        row.pop("min_gpu_count_for_rental")
        row["specs"].pop("ram")
        row["location"] = {}
        result, _ = self.scan([row], provider="lium")
        self.assertEqual(result["status"], "ok")
        offer = result["offers"][0]
        self.assertIsNone(offer["download_mbps"])
        self.assertIsNone(offer["ram_gib"])
        self.assertIsNone(offer["min_gpu_count_for_rental"])
        self.assertEqual(offer["unverified_fields"], ["country", "download_mbps", "ram_gib"])

    def test_complete_single_gpu_host_needs_no_minimum_split_metadata(self):
        row = lium()
        row.update(gpu_count=1, available_gpu_count=1, min_gpu_count_for_rental=None)
        row["specs"]["gpu"]["details"] = [{"name": "NVIDIA GeForce RTX 5090"}]
        result, _ = self.scan([row], provider="lium")
        self.assertEqual(result["status"], "ok")
        offer = result["offers"][0]
        self.assertEqual((offer["gpu_count"], offer["available_gpu_count"], offer["available_count"]), (1, 1, 1))
        self.assertIsNone(offer["min_gpu_count_for_rental"])
        self.assertNotIn("allocation", offer["unverified_fields"])

    def test_verified_server_edition_is_preserved(self):
        row = lium()
        row["specs"]["gpu"]["details"] = [{"name": "NVIDIA RTX PRO 6000 Blackwell Server Edition"}] * 2
        result, _ = self.scan([row], provider="lium")
        self.assertEqual(result["offers"][0]["gpu_type"], "RTX PRO 6000 Blackwell Server Edition")

    def test_decimal_node_price_is_rounded_only_after_gpu_multiplication(self):
        row = lium()
        row["price_per_gpu"] = "0.0000001"
        result, _ = self.scan([row], provider="lium")
        self.assertEqual(result["offers"][0]["hourly_cost_microusd"], 1)

    def test_empty_and_confirmed_unavailable_are_successful(self):
        for rows in ([], [dict(targon(), available=0)]):
            result, _ = self.scan(rows)
            self.assertEqual((result["status"], result["offers"]), ("ok", []))
        result, _ = self.scan([dict(lium(), available_gpu_count=0)], provider="lium")
        self.assertEqual((result["status"], result["offers"]), ("ok", []))

    def test_partial_hosts_preserve_free_and_minimum_counts_without_slice_quotes(self):
        for minimum in (1, 4, None):
            with self.subTest(minimum=minimum):
                row = lium()
                row.update(gpu_count=8, available_gpu_count=4,
                           min_gpu_count_for_rental=minimum, is_whole_host_free=False)
                row["specs"]["gpu"]["details"] = [{"name": "NVIDIA GeForce RTX 5090"}] * 8
                result, _ = self.scan([row], provider="lium")
                self.assertEqual(result["status"], "ok")
                offer = result["offers"][0]
                self.assertEqual((offer["gpu_count"], offer["available_gpu_count"], offer["available_count"]), (8, 4, 0))
                # The projection can distinguish min=4 (cannot satisfy one GPU)
                # from min=1 or unknown without inventing a rental allocation.
                self.assertEqual(offer["min_gpu_count_for_rental"], minimum)
                self.assertEqual(offer["unverified_fields"], ["allocation"])
                self.assertEqual(offer["hourly_cost_microusd"], 6000000)
                self.assertEqual((offer["ram_gib"], offer["disk_gib"]), (128, 160))

    def test_contradictory_whole_host_flag_retains_unknown_allocation(self):
        result, _ = self.scan([dict(lium(), is_whole_host_free=False)], provider="lium")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["offers"][0]["available_count"], 0)
        self.assertIn("allocation", result["offers"][0]["unverified_fields"])

    def test_pending_rental_retains_unknown_allocation_instead_of_false_empty(self):
        result, _ = self.scan([dict(lium(), has_no_pending_rental=False)], provider="lium")
        self.assertEqual(result["status"], "ok")
        offer = result["offers"][0]
        self.assertEqual((offer["available_count"], offer["available_gpu_count"]), (0, 2))
        self.assertIn("allocation", offer["unverified_fields"])

    def test_ambiguous_availability_and_negative_or_nonfinite_values_fail(self):
        for value in (None, -1, True, "9", 1.5):
            result, _ = self.scan([dict(targon(), available=value)])
            self.assertEqual((result["status"], result["offers"]), ("error", []))
        for field, value in (("available_gpu_count", None), ("available_gpu_count", 3),
                             ("price_per_gpu", "NaN"), ("price_per_gpu", "-1"),
                             ("min_gpu_count_for_rental", 3)):
            result, _ = self.scan([dict(lium(), **{field: value})], provider="lium")
            self.assertEqual((result["status"], result["offers"]), ("error", []))
        row = targon()
        row["spec"]["memory_mib"] = -1
        self.assertEqual(self.scan([row])[0]["status"], "error")

    def test_invalid_or_partial_payload_never_looks_like_no_stock(self):
        for payload in ({"error": "private"}, {"data": []}, [None], [targon(), targon()]):
            result, _ = self.scan(payload)
            self.assertEqual((result["status"], result["offers"]), ("error", []))
        for headers in ({"Link": '<https://other.invalid>; rel="next"'}, {"X-Next-Cursor": "opaque"},
                        {"Content-Range": "items 0-1/9"}, {"X-Total-Count": "10"}):
            self.assertEqual(self.scan([targon()], headers=headers)[0]["status"], "error")
        self.assertEqual(self.scan([], status=206)[0]["status"], "error")

    def test_http_errors_and_timeouts_never_expose_exception_or_body(self):
        for status in (302, 401, 429, 500):
            result, _ = self.scan({"secret": "must-not-escape"}, status=status)
            self.assertEqual(result["reason_code"], "inventory_http_error")
            self.assertNotIn("must-not-escape", json.dumps(result))
        def fail(request):
            raise httpx.ReadTimeout("test-secret-must-not-escape")
        result = subject.scan_targon(transport=httpx.MockTransport(fail))
        self.assertEqual(result["reason_code"], "inventory_timeout")
        self.assertNotIn("must-not-escape", json.dumps(result))

    def test_body_and_row_limits_are_enforced(self):
        with patch.object(subject, "MAX_RESPONSE_BYTES", 10):
            self.assertEqual(self.scan([targon()])[0]["reason_code"], "inventory_response_too_large")
        with patch.object(subject, "MAX_ROWS", 0):
            self.assertEqual(self.scan([targon()])[0]["status"], "error")

    def test_duplicate_json_fields_and_json_nonfinite_numbers_fail(self):
        for raw in (b'[{"available":9,"available":0}]', b'[NaN]'):
            with self.subTest(raw=raw):
                transport = httpx.MockTransport(lambda _: httpx.Response(200, content=raw))
                result = subject.scan_targon(transport=transport)
                self.assertEqual((result["status"], result["reason_code"]),
                                 ("error", "inventory_invalid_response"))

    def test_slow_stream_checks_each_chunk_and_timestamp_is_request_start(self):
        class SlowStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b"["
                yield b"]"
                raise AssertionError("must stop before reading another chunk")
        transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowStream()))
        with patch.object(subject.time, "monotonic", side_effect=[0, 10, 31]):
            result = subject.scan_targon(transport=transport, clock=lambda: 123)
        self.assertEqual(result["reason_code"], "inventory_timeout")
        self.assertEqual(result["observed_at"], 123)

    def test_unrequested_compression_is_rejected_before_reading(self):
        result, request = self.scan([], headers={"Content-Encoding": "identity"})
        self.assertEqual(request.headers["Accept-Encoding"], "identity")
        self.assertEqual(result["status"], "ok")
        class Unreadable(httpx.SyncByteStream):
            def __iter__(self):
                raise AssertionError("compressed bytes must not be consumed")
        transport = httpx.MockTransport(lambda _: httpx.Response(200,
            headers={"Content-Encoding": "gzip"}, stream=Unreadable()))
        result = subject.scan_targon(transport=transport)
        self.assertEqual(result["reason_code"], "inventory_response_encoding")

    def test_mismatched_profile_fails_before_http(self):
        calls = []
        def bad_loader(*args, **kwargs):
            value = loader(*args, **kwargs)
            value.base_url = "https://unapproved.invalid"
            return value
        def transport(request):
            calls.append(request)
            raise AssertionError("no network expected")
        result = subject.scan_lium(bad_loader, transport=httpx.MockTransport(transport))
        self.assertEqual(result["reason_code"], "inventory_profile_unavailable")
        self.assertEqual(calls, [])
        self.assertNotIn("must-not-escape", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
