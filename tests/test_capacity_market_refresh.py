"""Dual-provider wake-ups and lifecycle isolation with offline suppliers."""
from concurrent.futures import Future, ThreadPoolExecutor
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from studio_platform.auth import Principal
from studio_platform.capacity_candidates import candidates_projection
from studio_platform.capacity_market import (market_inventory, publish_observation,
    refresh_projection, request_refresh)
from studio_platform.capacity_scan import ProviderMarketRefresh
from studio_platform.operator_capacity import OperatorCapacity, OperatorRegistry, operator_commands
from studio_platform.operator_routes import register_routes
from studio_platform.runtime_catalog import public_catalog
from test_platform_repository import LedgerCase


class DeferredExecutor:
    def __init__(self, **options):
        self.work, self.shutdown_calls = [], []

    def submit(self, function, *args):
        future = Future()
        self.work.append((function, args, future))
        return future

    def complete(self, index):
        function, args, future = self.work[index]
        future.set_result(function(*args))

    def shutdown(self, **options):
        self.shutdown_calls.append(options)


class RefreshTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.registry = OperatorRegistry(catalog=public_catalog)

    def records(self):
        with self.repo.engine.connect() as connection:
            return {row["provider"]: dict(row) for row in connection.execute(select(market_inventory)).mappings()}

    def observation(self, provider, *, observed=None, status="ok", **extra):
        return {"provider": provider, "status": status, "observed_at": self.now if observed is None else observed,
                "offers": [], **extra}

    def project(self):
        with self.repo.engine.connect() as connection:
            return candidates_projection(connection, self.registry, "MiniMax-H3-Pruned-Rank8-INT8", "fl", 3600, self.now)

    def test_request_preserves_cache_times_and_money_and_coalesces_concurrently(self):
        for provider in ("lium", "targon"):
            publish_observation(self.repo, self.observation(provider, observed=self.now-121))
        before = copy.deepcopy(self.records())
        budget = self.repo.get_budget("owner-budget")
        with ThreadPoolExecutor(max_workers=4) as executor:
            receipts = list(executor.map(lambda _: request_refresh(self.repo), range(4)))
        self.assertEqual(len({receipt["request_id"] for receipt in receipts}), 1)
        self.assertEqual(sum(not receipt["coalesced"] for receipt in receipts), 1)
        after = self.records()
        for provider in ("lium", "targon"):
            self.assertEqual(after[provider]["observed_at"], before[provider]["observed_at"])
            self.assertEqual(after[provider]["payload"]["offers"], before[provider]["payload"]["offers"])
            self.assertEqual(after[provider]["payload"]["refresh_request_id"], receipts[0]["request_id"])
            self.assertEqual(refresh_projection(after[provider], self.now)["refresh_status"], "pending")
        self.assertEqual([provider["status"] for provider in self.project()["providers"]], ["stale", "stale"])
        self.assertEqual(self.repo.get_budget("owner-budget"), budget)
        self.assertEqual(self.repo.list_instance_intents(), [])
        with self.repo.engine.connect() as connection:
            self.assertEqual(connection.scalar(select(func.count()).select_from(operator_commands)), 0)

    def test_same_clock_cached_success_is_not_a_completed_new_read(self):
        publish_observation(self.repo, self.observation("lium"))
        receipt = request_refresh(self.repo)
        self.assertEqual(refresh_projection(self.records()["lium"], self.now)["refresh_status"], "pending")
        self.now += 1
        publish_observation(self.repo, self.observation("lium"))
        provider = self.project()["providers"][0]
        self.assertEqual(provider["refresh_request_id"], receipt["request_id"])
        self.assertEqual(provider["refresh_status"], "complete")
        self.now += 121
        provider = self.project()["providers"][0]
        self.assertEqual(provider["status"], "stale")
        self.assertEqual(provider["refresh_status"], "complete")

    def test_post_request_observations_are_independent_redacted_and_out_of_order_safe(self):
        receipt = request_refresh(self.repo)
        self.now += 1
        publish_observation(self.repo, self.observation("targon", status="error", raw="secret"))
        result = {item["provider"]: item for item in self.project()["providers"]}
        self.assertEqual(result["targon"]["refresh_status"], "failed")
        self.assertEqual(result["targon"]["refresh_reason_code"], "inventory_scan_failed")
        self.assertEqual(result["lium"]["refresh_status"], "pending")
        self.assertFalse(publish_observation(self.repo, self.observation("targon", observed=self.now-2)))
        self.assertNotIn("secret", json.dumps(self.project()))
        self.now += 60
        result = {item["provider"]: item for item in self.project()["providers"]}
        self.assertEqual(result["lium"]["refresh_status"], "timeout")
        self.assertEqual(result["lium"]["refresh_reason_code"], "inventory_refresh_timeout")
        replacement = request_refresh(self.repo)
        self.assertNotEqual(replacement["request_id"], receipt["request_id"])
        self.assertEqual({value["payload"]["refresh_request_id"] for value in self.records().values()},
                         {replacement["request_id"]})

    def test_inflight_old_read_preserves_request_marker_without_completing_it(self):
        started = self.now
        self.now += 1
        receipt = request_refresh(self.repo)
        publish_observation(self.repo, self.observation("lium", observed=started))
        row = self.project()["providers"][0]
        self.assertEqual(row["refresh_request_id"], receipt["request_id"])
        self.assertEqual(row["refresh_status"], "pending")

    def test_scanner_publishes_fast_supplier_and_services_request_without_blocking_other_ticks(self):
        readers = {provider: Mock(side_effect=lambda provider=provider: self.observation(provider))
                   for provider in ("lium", "targon")}
        executor = DeferredExecutor()
        refresh = ProviderMarketRefresh(self.repo, readers=readers)
        with patch("studio_platform.capacity_scan.ThreadPoolExecutor", return_value=executor):
            refresh()
            self.assertEqual(len(executor.work), 2)
            self.assertEqual(refresh.pending.keys(), {"lium", "targon"})
            for reader in readers.values(): reader.assert_not_called()
            self.now += 1
            executor.complete(1)
            refresh()
            self.assertEqual(self.records().keys(), {"targon"})
            self.assertIn("lium", refresh.pending)
            receipt = request_refresh(self.repo)
            self.now += 1
            executor.complete(0)
            refresh()
            # This Lium read actually began after the request, so no duplicate
            # scan is scheduled; old Targon cache still needs a new observation.
            self.assertEqual(self.project()["providers"][0]["refresh_status"], "complete")
            self.assertEqual(len(executor.work), 3)
            self.assertEqual(executor.work[2][1][0], "targon")
            executor.complete(2)
            refresh()
            self.assertTrue(all(item["refresh_status"] == "complete" for item in self.project()["providers"]))
            self.assertTrue(all(item["refresh_request_id"] == receipt["request_id"] for item in self.project()["providers"]))
            self.assertEqual(len(executor.work), 3)

    def test_old_inflight_read_triggers_new_requested_read_and_stop_discards_late_result(self):
        readers = {provider: Mock(side_effect=lambda provider=provider: self.observation(provider, observed=1000))
                   for provider in ("lium", "targon")}
        executor = DeferredExecutor()
        refresh = ProviderMarketRefresh(self.repo, readers=readers)
        with patch("studio_platform.capacity_scan.ThreadPoolExecutor", return_value=executor):
            refresh()
            self.now += 1
            request_refresh(self.repo)
            executor.complete(0)
            refresh()
            self.assertEqual(len(executor.work), 3)
            self.assertEqual(executor.work[2][1][0], "lium")
            self.assertEqual(self.project()["providers"][0]["refresh_status"], "pending")
            refresh(stopping=True)
            self.assertEqual(executor.shutdown_calls, [{"wait": False, "cancel_futures": True}])
            executor.complete(1)
            executor.complete(2)
            refresh()
            self.assertEqual(self.records()["targon"]["observed_at"], 0)

    def test_same_clock_request_wakes_idle_suppliers_once_without_waiting_for_cadence(self):
        readers = {provider: Mock(side_effect=lambda provider=provider: self.observation(provider))
                   for provider in ("lium", "targon")}
        executor = DeferredExecutor()
        refresh = ProviderMarketRefresh(self.repo, readers=readers)
        with patch("studio_platform.capacity_scan.ThreadPoolExecutor", return_value=executor):
            refresh()
            executor.complete(0)
            executor.complete(1)
            refresh()
            self.assertEqual(len(executor.work), 2)
            request_refresh(self.repo)
            refresh()
            self.assertEqual(len(executor.work), 4)
            refresh()
            self.assertEqual(len(executor.work), 4)
            self.now += 0.1
            executor.complete(2)
            executor.complete(3)
            refresh()
            self.assertEqual(len(executor.work), 4)
            self.assertTrue(all(item["refresh_status"] == "complete" for item in self.project()["providers"]))

    def test_missing_lium_identity_never_falls_back_but_targon_remains_usable(self):
        with patch("studio_platform.capacity_inventory.scan_lium", side_effect=AssertionError("identity fallback")), \
             patch("studio_platform.capacity_inventory.scan_targon", return_value=self.observation("targon")) as targon:
            refresh = ProviderMarketRefresh(self.repo)
            self.assertEqual(refresh._read("lium", self.now)["status"], "error")
            self.assertEqual(refresh._read("targon", self.now)["status"], "ok")
            targon.assert_called_once()
        self.assertIsNone(refresh.executor)

    def test_route_is_operator_only_accepts_empty_body_and_has_no_network_or_paid_effects(self):
        actor = [Principal("superdan", "browser", auth_mode="password")]
        service = OperatorCapacity(self.repo, SimpleNamespace(operator_capacity_owners=("superdan",)), self.registry)
        app = FastAPI()
        @app.middleware("http")
        async def principal(request: Request, call_next):
            request.state.principal = actor[0]
            return await call_next(request)
        register_routes(app, service=service)
        with patch("studio_platform.lium_provider._central_loader", side_effect=AssertionError("credentials")), \
             patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("network")), TestClient(app) as client:
            result = client.post("/v1/operator/capacity/market-refreshes", json={})
            self.assertEqual(result.status_code, 202)
            self.assertEqual(result.headers["cache-control"], "no-store")
            self.assertEqual(result.json()["providers"], ["lium", "targon"])
            self.assertEqual(client.post("/v1/operator/capacity/market-refreshes", json={"provider":"lium"}).status_code, 422)
            for principal, status in ((None, 401), (Principal("supervan", "browser"), 403),
                    (Principal("superdan", "service", machine=True), 403)):
                actor[0] = principal
                self.assertEqual(client.post("/v1/operator/capacity/market-refreshes", json={}).status_code, status)

    def test_real_app_origin_and_expected_account_guard_reject_before_cache_write(self):
        from studio_platform.api import create_app
        from studio_platform.settings import Settings
        settings = Settings(Path(self.temp.name)/"http", database_url=self.url,
                            auth_mode="local-test", operator_capacity_owners=("superdan",))
        with patch("studio_platform.lium_provider._central_loader", side_effect=AssertionError("credentials")), \
             patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("network")), \
             TestClient(create_app(settings)) as client:
            client.post("/api/auth/login", json={"username":"superdan"}).raise_for_status()
            for headers, status in (({"origin":"https://attacker.example"}, 403),
                    ({"sec-fetch-site":"cross-site"}, 403), ({"x-expected-account":"supervan"}, 409)):
                self.assertEqual(client.post("/v1/operator/capacity/market-refreshes", json={}, headers=headers).status_code,
                                 status)
                self.assertEqual(self.records(), {})
            response = client.post("/v1/operator/capacity/market-refreshes", json={},
                headers={"origin":"http://testserver", "x-expected-account":"superdan"})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(set(self.records()), {"lium", "targon"})

