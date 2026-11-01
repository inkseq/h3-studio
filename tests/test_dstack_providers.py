"""Lium/CloudRift inventory reads and per-profile fallback backends."""

import copy
import json
import unittest

import httpx
from sqlalchemy import select

from studio_platform.dstack_capacity import (
    CLOUDRIFT_INSTANCES_URL,
    LIUM_PODS_URL,
    DstackClient,
    DstackError,
)
from studio_platform.dstack_operator import DstackOperator, OperatorError
from studio_platform.repository import instance_intents
import test_dstack_operator
from test_platform_repository import LedgerCase

KEY = "provider-secret-token-123456"


def provider_client(test, backend, handler):
    config = {"type": backend, "creds": {"type": "api_key", "api_key": KEY}}

    def dstack_read(req):
        test.assertEqual(
            req.url.path, f"/api/project/sixnine/backends/{backend}/config_info"
        )
        return httpx.Response(200, json=config)

    def provider_read(req):
        # Lium and CloudRift take the key in X-API-Key, never as a bearer token.
        test.assertEqual(req.headers["X-API-Key"], KEY)
        test.assertNotIn("Authorization", req.headers)
        return handler(req)

    return DstackClient(
        "http://127.0.0.1:3000",
        "dstack-secret-token-123456",
        "sixnine",
        session=httpx.Client(transport=httpx.MockTransport(dstack_read)),
        provider_session=httpx.Client(transport=httpx.MockTransport(provider_read)),
    )


class ProviderInventoryTests(unittest.TestCase):
    def test_lium_lists_pod_ids_and_pending_pod_names(self):
        def read(req):
            self.assertEqual((req.method, str(req.url)), ("GET", LIUM_PODS_URL))
            return httpx.Response(
                200,
                json=[
                    {"id": "11111111-2222-3333-4444-555555555555", "pod_name": "p-1"},
                    {"id": "66666666-7777-8888-9999-000000000000"},
                ],
            )

        result = provider_client(self, "lium", read).provider_inventory("lium")
        # One allocation per pod; a name is only an alias for a pending binding.
        self.assertEqual(
            result["instance_ids"],
            [
                "11111111-2222-3333-4444-555555555555",
                "66666666-7777-8888-9999-000000000000",
            ],
        )
        self.assertEqual(
            result["aliases"], {"p-1": "11111111-2222-3333-4444-555555555555"}
        )
        self.assertTrue(result["complete"])

    def test_cloudrift_lists_live_instances_with_pinned_api_version(self):
        def read(req):
            self.assertEqual(
                (req.method, str(req.url)), ("POST", CLOUDRIFT_INSTANCES_URL)
            )
            body = json.loads(req.content)
            self.assertEqual(body["version"], "2026-10-12")
            self.assertIn("Active", body["data"]["selector"]["ByStatus"]["statuses"])
            return httpx.Response(
                200,
                json={"version": "2026-10-12", "data": {"instances": [{"id": "e9d5"}]}},
            )

        result = provider_client(self, "cloudrift", read).provider_inventory(
            "cloudrift"
        )
        self.assertEqual(result["instance_ids"], ["e9d5"])

    def test_malformed_inventories_are_never_read_as_empty(self):
        for backend, bad in (
            ("lium", {"pods": []}),
            ("lium", [{"name": "no-id"}]),
            ("cloudrift", {"data": {}}),
            ("cloudrift", {"data": {"instances": [{"id": "a"}, {"id": "a"}]}}),
        ):
            with (
                self.subTest(backend=backend, bad=bad),
                self.assertRaisesRegex(
                    DstackError, "^dstack_provider_inventory_unconfirmed$"
                ),
            ):
                provider_client(
                    self, backend, lambda _, bad=bad: httpx.Response(200, json=bad)
                ).provider_inventory(backend)


def offer(backend, name):
    return {
        "backend": backend,
        "region": "test-region",
        "price": 0.7,
        "availability": "available",
        "instance": {
            "name": name,
            "resources": {
                "cpus": 12,
                "memory_mib": 96 * 1024,
                "disk": {"size_mib": 128 * 1024},
                "gpus": [{"name": "RTX5090", "memory_mib": 32 * 1024}],
            },
        },
    }


class FallbackBackendTests(LedgerCase):
    def setUp(self):
        super().setUp()
        test_dstack_operator.DstackOperatorTests._setup_dstack(self)
        self.stock = {"vastai": [], "lium": [offer("lium", "pod-a")]}
        self.planned = []

        def plan(spec):
            backend = spec["configuration"]["backends"][0]
            self.planned.append(backend)
            return {
                "run_spec": spec,
                "job_plans": [{"offers": self.stock.get(backend, [])}],
            }

        self.client.plan = plan

    def operator(self, **changes):
        config = copy.deepcopy(self.config)
        config["profiles"][0].update(changes)
        return DstackOperator(self.repo, self.settings, self.capacity, config)

    def test_fallback_list_is_validated(self):
        for value in (["lium", "lium"], ["vastai"], ["aws"], "lium"):
            with self.subTest(value=value), self.assertRaises(OperatorError):
                self.operator(fallback_backends=value)

    def test_without_fallback_an_empty_primary_still_refuses(self):
        service = self.operator()
        with self.assertRaisesRegex(OperatorError, "dstack_no_compatible_offers"):
            service.preview(self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900})
        self.assertEqual(self.planned, ["vastai"])

    def test_empty_primary_binds_the_first_fallback_with_offers_end_to_end(self):
        service = self.operator(fallback_backends=["runpod", "lium", "cloudrift"])
        preview = service.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )
        self.assertEqual(self.planned, ["vastai", "runpod", "lium"])
        self.assertEqual({row["backend"] for row in preview["offers"]}, {"lium"})
        service.start(self.owner, {"preview_id": preview["preview_id"]}, "start-1")
        (applied,) = self.client.applied
        binding = self.store.load(applied)
        self.assertEqual(binding["backend"], "lium")
        self.assertEqual(binding["run_spec"]["configuration"]["backends"], ["lium"])
        self.assertNotIn("backend_options", binding["run_spec"]["configuration"])
        with self.repo.engine.connect() as connection:
            intent = connection.execute(select(instance_intents)).mappings().one()
        self.assertEqual(intent["provider"], "lium")

    def test_a_cooling_primary_lane_is_skipped_for_its_fallback(self):
        from unittest.mock import patch

        self.stock["runpod"] = self.stock.get("lium", [])
        service = self.operator(fallback_backends=["lium"])
        with patch(
            "studio_platform.scheduling.health.cooling_backends",
            return_value={"vastai": 10**12},
        ):
            preview = service.preview(
                self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
            )
        self.assertEqual(self.planned, ["lium"])
        self.assertEqual({row["backend"] for row in preview["offers"]}, {"lium"})

    def test_every_lane_cooling_refuses_without_planning(self):
        from unittest.mock import patch

        service = self.operator(fallback_backends=["lium"])
        with (
            patch(
                "studio_platform.scheduling.health.cooling_backends",
                return_value={"vastai": 10**12, "lium": 10**12},
            ),
            self.assertRaisesRegex(OperatorError, "dstack_lane_backoff"),
        ):
            service.preview(self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900})
        self.assertEqual(self.planned, [])

    def test_vast_offers_rejected_by_host_policy_fall_through_to_lium(self):
        vast = self.client.__class__.plan(self.client, {})["job_plans"][0]["offers"]
        self.stock["vastai"] = vast
        slow = self.client.vast_offer_mapping

        def mapping(request, *, clock):
            value = slow(request, clock=clock)
            for row in value["offers"]:
                row["advertised_download_mbps"] = 100
            return value

        self.client.vast_offer_mapping = mapping
        service = self.operator(fallback_backends=["lium"])
        preview = service.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )
        self.assertEqual(self.planned, ["vastai", "lium"])
        self.assertEqual({row["backend"] for row in preview["offers"]}, {"lium"})

    def test_vast_host_rejection_without_fallback_keeps_its_reason(self):
        self.stock["vastai"] = self.client.__class__.plan(self.client, {})["job_plans"][
            0
        ]["offers"]
        slow = self.client.vast_offer_mapping
        self.client.vast_offer_mapping = lambda request, *, clock: {
            **slow(request, clock=clock),
            "offers": [],
        }
        with self.assertRaisesRegex(OperatorError, "dstack_no_compatible_host_offers"):
            self.operator().preview(
                self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
            )

    def test_primary_with_offers_never_asks_a_fallback(self):
        self.stock["vastai"] = self.client.__class__.plan(self.client, {})["job_plans"][
            0
        ]["offers"]
        service = self.operator(fallback_backends=["lium"])
        service.preview(self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900})
        self.assertEqual(self.planned, ["vastai"])


if __name__ == "__main__":
    unittest.main()
