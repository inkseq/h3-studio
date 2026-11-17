"""Lium/CloudRift inventory reads and per-profile fallback backends."""

import copy
import math
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

    def test_a_serverless_lane_is_priced_by_its_own_rate_not_the_pod_ceiling(self):
        # Owner 2026-11-17 (#1243): serverless bills per second while it works, so
        # a rented machine's hourly ceiling (0.75 USD here) must not drop it.
        rate = 4_800_000
        lane = dict(offer("modal-dialout", "sandbox"), price=rate / 1_000_000)
        self.stock["modal-dialout"] = [lane]
        self.client.lane_price = lambda backend: (
            rate if backend == "modal-dialout" else None
        )
        service = self.operator(backend="modal-dialout", fallback_backends=["lium"])
        # The fleet's hourly total still applies (raised from the fixture's 3 USD).
        service.policy["max_hourly_cost_microusd"] = 10_000_000
        preview = service.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )
        self.assertEqual(self.planned, ["modal-dialout"])
        self.assertEqual(
            {row["backend"] for row in preview["offers"]}, {"modal-dialout"}
        )
        self.assertEqual(preview["max_price_microusd"], rate)
        self.assertEqual(preview["hourly_cost_microusd"], rate)
        self.assertEqual(
            preview["reservation_microusd"], math.ceil(rate * (900 + 300) / 3600)
        )
        # The start keeps the lane's rate as the run's hourly cost.
        service.start(self.owner, {"preview_id": preview["preview_id"]}, "lane-1")
        (applied,) = self.client.applied
        binding = self.store.load(applied)
        self.assertEqual(binding["backend"], "modal-dialout")
        self.assertEqual(
            binding["run_spec"]["configuration"]["backends"], ["modal-dialout"]
        )
        with self.repo.engine.connect() as connection:
            intent = connection.execute(select(instance_intents)).mappings().one()
        self.assertEqual(intent["provider"], "modal-dialout")

    def test_a_rate_that_rounds_up_as_a_float_still_gets_its_offer(self):
        rate = 2_007_000  # 2.007 * 1e6 rounds up to 2_007_001
        self.stock["modal-dialout"] = [
            dict(offer("modal-dialout", "sandbox"), price=rate / 1_000_000)
        ]
        self.client.lane_price = lambda backend: (
            rate if backend == "modal-dialout" else None
        )
        service = self.operator(backend="modal-dialout", fallback_backends=["lium"])
        preview = service.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )
        self.assertEqual(
            {row["backend"] for row in preview["offers"]}, {"modal-dialout"}
        )
        self.assertEqual(preview["max_price_microusd"], rate)

    def test_a_pod_fallback_keeps_the_pod_ceiling_after_a_serverless_lane(self):
        # The lane has no offer; the pod fallback is still bound by 0.75 USD/h.
        self.client.lane_price = lambda backend: (
            4_800_000 if backend == "modal-dialout" else None
        )
        expensive = dict(offer("lium", "pod-dear"), price=0.9)
        self.stock["lium"] = [expensive, offer("lium", "pod-a")]
        service = self.operator(backend="modal-dialout", fallback_backends=["lium"])
        preview = service.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )
        self.assertEqual(self.planned, ["modal-dialout", "lium"])
        self.assertEqual(preview["max_price_microusd"], 750000)
        self.assertEqual([row["offer_id"] for row in preview["offers"]], ["pod-a"])
        self.assertTrue(
            all(row["hourly_cost_microusd"] <= 750000 for row in preview["offers"])
        )

    def test_a_refused_serverless_start_ends_instead_of_staying_unknown(self):
        # #1268: RunPod answered /run with a 4xx, so no job exists. The start
        # must not become an unknown allocation that blocks the lane.
        import tempfile

        import httpx

        from studio_platform.dialout_capacity import RoutedClient
        from studio_platform.dstack_capacity import DstackCapacity
        from studio_platform.runpod_serverless import RunPodServerlessClient

        def runpod(request):
            if request.url.path.endswith("/run"):
                return httpx.Response(400)
            return httpx.Response(200, json={"jobs": {"inQueue": 0, "inProgress": 0}})

        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        lane = RunPodServerlessClient(
            {
                "api_key": "k" * 32,
                "endpoint_id": "abc123",
                "gpu_name": "RTX5090",
                "gpu_memory_gib": 32,
                "cpus": 12,
                "memory_gib": 96,
                "disk_gib": 128,
                "hourly_microusd": 700_000,
                "state_dir": state.name,
            },
            self.client.project,
            http=httpx.Client(transport=httpx.MockTransport(runpod)),
            clock=self.repo.clock,
        )
        capacity = DstackCapacity(
            RoutedClient(self.client, lane), self.store, clock=self.repo.clock
        )
        config = copy.deepcopy(self.config)
        config["profiles"][0].update(backend="runpod-serverless", transport="dialout")
        settings = copy.copy(self.settings)
        settings.public_origin = "https://staging.inkseq.com"
        service = DstackOperator(self.repo, settings, capacity, config)
        preview = service.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )
        accepted = service.start(
            self.owner, {"preview_id": preview["preview_id"]}, "refused-1"
        )
        observed = self.store.load(accepted["node_id"])
        # Ended, with its cost still to be settled (never assumed zero).
        self.assertEqual(
            (observed["state"], observed["dstack_status"], observed["billing_state"]),
            ("stopped", "failed", "unsettled"),
        )
        with self.repo.engine.connect() as connection:
            intent = connection.execute(select(instance_intents)).mappings().one()
        self.assertNotEqual(intent["state"], "creation_unknown")
        # It holds no slot or hourly cost, and the refusing lane cools, so the
        # next start goes to the fallback instead of the same refusal.
        usage = service.prelaunch_capacity_usage(self.repo.engine.connect())
        self.assertEqual(
            (usage["instances"], usage["logical_slots"], usage["hourly_cost_microusd"]),
            (0, 0, 0),
        )
        from studio_platform.scheduling.health import cooling_backends

        with self.repo.engine.connect() as connection:
            cooling = cooling_backends(
                connection, self.settings.tenant_id, self.repo.clock()
            )
        self.assertIn("runpod-serverless", cooling)
        # The watchdog stops the refused node at once (a failed start), so its
        # replacement starts while the lane still cools, not 20 minutes later.
        from studio_platform.capacity_watchdog import reclaim_node
        from studio_platform.operator_capacity import operator_nodes

        with self.repo.engine.connect() as connection:
            node = dict(connection.execute(select(operator_nodes)).mappings().one())
            intent = dict(connection.execute(select(instance_intents)).mappings().one())
        outcome = reclaim_node(service, node, intent)
        self.assertEqual(
            (outcome["stop_requested"], outcome["reason"]),
            (True, "capacity_startup_failed"),
        )
        # The controller's next observation records the run as stopped again.
        capacity.observe(intent["id"])
        with self.repo.engine.connect() as connection:
            node = connection.execute(select(operator_nodes)).mappings().one()
            cooling = cooling_backends(
                connection, self.settings.tenant_id, self.repo.clock()
            )
        self.assertEqual(
            (node["desired_state"], node["runtime_state"]), ("stopped", "stopped")
        )
        self.assertIn("runpod-serverless", cooling)
        self.planned.clear()
        config["profiles"][0].update(fallback_backends=["lium"])
        service = DstackOperator(self.repo, settings, capacity, config)
        retry = service.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )
        self.assertEqual({row["backend"] for row in retry["offers"]}, {"lium"})

    def test_a_stop_for_a_start_without_a_run_id_waits_then_ends_it(self):
        # #1268: a lost /run answer leaves no run id. A watchdog stop must not
        # fail every tick: it stays unknown until the journal expires, then
        # adopts the ended run and needs no provider call.
        import tempfile

        import httpx

        from studio_platform.dialout_capacity import RoutedClient
        from studio_platform.dstack_capacity import DstackCapacity
        from studio_platform.runpod_serverless import RunPodServerlessClient

        calls = []

        def runpod(request):
            calls.append(request.url.path)
            if request.url.path.endswith("/run"):
                return httpx.Response(503)
            return httpx.Response(200, json={"jobs": {"inQueue": 0, "inProgress": 0}})

        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        lane = RunPodServerlessClient(
            {
                "api_key": "k" * 32,
                "endpoint_id": "abc123",
                "gpu_name": "RTX5090",
                "gpu_memory_gib": 32,
                "cpus": 12,
                "memory_gib": 96,
                "disk_gib": 128,
                "hourly_microusd": 700_000,
                "state_dir": state.name,
            },
            self.client.project,
            http=httpx.Client(transport=httpx.MockTransport(runpod)),
            clock=self.repo.clock,
        )
        capacity = DstackCapacity(
            RoutedClient(self.client, lane), self.store, clock=self.repo.clock
        )
        config = copy.deepcopy(self.config)
        config["profiles"][0].update(backend="runpod-serverless", transport="dialout")
        settings = copy.copy(self.settings)
        settings.public_origin = "https://staging.inkseq.com"
        service = DstackOperator(self.repo, settings, capacity, config)
        preview = service.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )
        accepted = service.start(
            self.owner, {"preview_id": preview["preview_id"]}, "lost-1"
        )
        node_id = accepted["node_id"]
        self.assertIsNone(self.store.load(node_id).get("run_id"))
        waiting = capacity.stop(node_id)
        self.assertNotEqual(waiting["state"], "stopped")
        self.assertIsNone(self.store.load(node_id).get("run_id"))
        self.now += lane.UNBOUND_EXPIRY_SECONDS
        calls.clear()
        ended = capacity.stop(node_id)
        binding = self.store.load(node_id)
        self.assertEqual(
            (ended["state"], binding["state"], binding["dstack_status"]),
            ("stopped", "stopped", "failed"),
        )
        self.assertTrue(binding["run_id"])
        self.assertFalse([path for path in calls if "/cancel/" in path])

    def test_a_serverless_job_with_no_worker_cools_the_lane_and_stops_now(self):
        # #1272: RunPod queued the job with no GPU free; the same lane was
        # retried after each 20 min timeout and no shot was ever made.
        import tempfile

        import httpx

        from studio_platform.capacity_watchdog import reclaim_node
        from studio_platform.dialout_capacity import RoutedClient
        from studio_platform.dstack_capacity import DstackCapacity
        from studio_platform.operator_capacity import operator_nodes
        from studio_platform.runpod_serverless import RunPodServerlessClient
        from studio_platform.scheduling.health import cooling_backends

        jobs = {}

        def runpod(request):
            path = request.url.path
            if path.endswith("/run"):
                jobs["job-1"] = "IN_QUEUE"
                return httpx.Response(200, json={"id": "job-1", "status": "IN_QUEUE"})
            if "/status/" in path:
                return httpx.Response(
                    200, json={"id": "job-1", "status": jobs["job-1"]}
                )
            if "/cancel/" in path:
                jobs["job-1"] = "CANCELLED"
                return httpx.Response(200, json={"status": "CANCELLED"})
            live = sum(s in {"IN_QUEUE", "IN_PROGRESS"} for s in jobs.values())
            return httpx.Response(
                200,
                json={
                    "jobs": {"inQueue": live, "inProgress": 0},
                    "workers": {"initializing": 0, "idle": 0, "ready": 0, "running": 0},
                },
            )

        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        lane = RunPodServerlessClient(
            {
                "api_key": "k" * 32,
                "endpoint_id": "abc123",
                "gpu_name": "RTX5090",
                "gpu_memory_gib": 32,
                "cpus": 12,
                "memory_gib": 96,
                "disk_gib": 128,
                "hourly_microusd": 700_000,
                "state_dir": state.name,
            },
            self.client.project,
            http=httpx.Client(transport=httpx.MockTransport(runpod)),
            clock=self.repo.clock,
        )
        capacity = DstackCapacity(
            RoutedClient(self.client, lane), self.store, clock=self.repo.clock
        )
        config = copy.deepcopy(self.config)
        config["profiles"][0].update(backend="runpod-serverless", transport="dialout")
        settings = copy.copy(self.settings)
        settings.public_origin = "https://staging.inkseq.com"
        service = DstackOperator(self.repo, settings, capacity, config)
        preview = service.preview(
            self.owner, {"profile_id": "5090-fl", "ttl_seconds": 900}
        )
        node_id = service.start(
            self.owner, {"preview_id": preview["preview_id"]}, "no-worker-1"
        )["node_id"]
        self.assertFalse(self.store.load(node_id).get("lane_refused"))
        self.now += lane.NO_WORKER_SECONDS
        capacity.observe(node_id)
        self.assertTrue(self.store.load(node_id)["lane_refused"])
        self.assertEqual(jobs["job-1"], "IN_QUEUE")  # The read changed nothing.
        with self.repo.engine.connect() as connection:
            node = dict(connection.execute(select(operator_nodes)).mappings().one())
            intent = dict(connection.execute(select(instance_intents)).mappings().one())
        outcome = reclaim_node(service, node, intent)
        self.assertEqual(
            (outcome["stop_requested"], outcome["reason"]),
            (True, "capacity_startup_failed"),
        )
        capacity.stop(node_id)
        self.assertEqual(jobs["job-1"], "CANCELLED")
        observed = capacity.observe(node_id)
        self.assertEqual(
            (observed["state"], self.store.load(node_id)["lane_refused"]),
            ("stopped", True),
        )
        # Stopped as a failed start of this lane: it cools, so the next start
        # goes to the fallback instead of queueing on RunPod again.
        with self.repo.engine.connect() as connection:
            self.assertIn(
                "runpod-serverless",
                cooling_backends(
                    connection, self.settings.tenant_id, self.repo.clock()
                ),
            )

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

    def test_denied_lium_executor_is_excluded_and_the_rest_are_frozen(self):
        # #900: a Lium offer names its executor (the physical host).
        from studio_platform.host_policy import HostPolicyStore

        bad = "0453b369-1111-4222-8333-444455556666"
        good = "11111111-1111-4111-8111-111111111111"
        self.stock["lium"] = [offer("lium", bad), offer("lium", good)]
        service = self.operator(fallback_backends=["lium"])
        request = {"profile_id": "5090-fl", "ttl_seconds": 900}
        # Nothing denied: no selector, unchanged behaviour.
        preview = service.preview(self.owner, request)
        self.assertEqual({row["offer_id"] for row in preview["offers"]}, {bad, good})
        with self.repo.transaction() as connection:
            HostPolicyStore(self.repo, self.scope.tenant_id).deny_slow_download(
                connection, bad, node_id="node-bad", now=self.now, provider="lium"
            )
        preview = service.preview(self.owner, request)
        self.assertEqual([row["offer_id"] for row in preview["offers"]], [good])
        service.start(self.owner, {"preview_id": preview["preview_id"]}, "start-1")
        (applied,) = self.client.applied
        binding = self.store.load(applied)
        self.assertEqual(binding["run_spec"]["profile"]["instance_types"], [good])
        self.assertEqual(binding["host_selection"]["provider"], "lium")
        # Every executor denied: this lane has nothing to offer.
        with self.repo.transaction() as connection:
            HostPolicyStore(self.repo, self.scope.tenant_id).deny_slow_download(
                connection, good, node_id="node-good", now=self.now, provider="lium"
            )
        with self.assertRaisesRegex(OperatorError, "dstack_no_compatible_host_offers"):
            service.preview(self.owner, request)

    def test_lium_executor_that_just_failed_to_start_is_left_out_if_another_is(
        self,
    ):
        # #1198: Lium rents the cheapest executor in the selector, so the one
        # whose last start failed is left out while another is on offer.
        from unittest.mock import patch

        bad = "0453b369-1111-4222-8333-444455556666"
        good = "11111111-1111-4111-8111-111111111111"
        self.stock["lium"] = [offer("lium", bad), offer("lium", good)]
        service = self.operator(fallback_backends=["lium"])
        request = {"profile_id": "5090-fl", "ttl_seconds": 900}
        with patch(
            "studio_platform.host_policy.recent_start_failures", return_value={bad}
        ) as recent:
            preview = service.preview(self.owner, request)
            self.assertEqual([row["offer_id"] for row in preview["offers"]], [good])
            self.assertEqual(recent.call_args.kwargs["provider"], "lium")
            service.start(self.owner, {"preview_id": preview["preview_id"]}, "start-1")
            binding = self.store.load(self.client.applied[0])
            self.assertEqual(binding["run_spec"]["profile"]["instance_types"], [good])
        with patch(
            "studio_platform.host_policy.recent_start_failures", return_value={bad}
        ):
            # The only executor on offer is still offered.
            self.stock["lium"] = [offer("lium", bad)]
            preview = self.operator(fallback_backends=["lium"]).preview(
                self.owner, request
            )
            self.assertEqual([row["offer_id"] for row in preview["offers"]], [bad])

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
