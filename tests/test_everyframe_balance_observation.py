"""Preserve the scheduler/admin projections of an EveryFrame balance answer."""

import io
import json
import math
import tempfile
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from studio_platform.providers import balances, everyframe
from studio_platform.scheduling import rebind
from studio_platform import dstack_demand_start


class BalanceObservationTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.settings = SimpleNamespace(data_dir=work.name, everyframe_enabled=True)
        self.source = next(s for s in balances.SOURCES if s.provider == "everyframe")
        self.requests = []
        self.status, self.value, self.malformed = 200, 20, False
        self.error = None
        for module in (balances, everyframe):
            stub = patch.object(
                module,
                "load_config",
                return_value=SimpleNamespace(api_key="private-test-key"),
            )
            stub.start()
            self.addCleanup(stub.stop)
        stub = patch.object(balances, "SOURCES", (self.source,))
        stub.start()
        self.addCleanup(stub.stop)
        stub = patch.object(httpx.Client, "send", self.send)
        stub.start()
        self.addCleanup(stub.stop)
        rebind._balance.update(usd=None, read_at=None)
        self.addCleanup(rebind._balance.update, usd=None, read_at=None)

    def send(self, request, **_):
        self.requests.append(request)
        if request.url.path.endswith("/render/models"):
            return httpx.Response(200, json=[], request=request)
        if self.error:
            raise self.error
        return httpx.Response(
            self.status,
            content=b"invalid"
            if self.malformed
            else json.dumps({"balanceUsd": self.value}).encode(),
            request=request,
        )

    def admin(self, now):
        balances.read_all(self.settings, None, now=now)
        return balances.read_stored(self.settings)["rows"][0]

    def test_distinct_status_boolean_and_nonfinite_behavior_is_unchanged(self):
        # Existing discrepancies are characterized, not corrected by extraction.
        for status, value, scheduler, admin in (
            (200, 0.5, 0.5, 0.5),
            (201, 0.5, None, 0.5),
            (200, True, True, None),
            (200, "bad", None, None),
            (200, float("inf"), float("inf"), float("inf")),
        ):
            with self.subTest(status=status, value_type=type(value).__name__):
                self.status, self.value = status, value
                rebind._balance.update(usd=None, read_at=None)
                with redirect_stdout(io.StringIO()):
                    actual = rebind.refresh_balance(self.settings, 1000)
                    row = self.admin(1000)
                self.assertEqual(actual, scheduler)
                if admin is None:
                    self.assertTrue(row["last_read_failed"])
                else:
                    self.assertEqual(row["amount"], admin)
                    self.assertFalse(row["last_read_failed"])
        rebind._balance.update(usd=None, read_at=None)
        self.status, self.value = 200, float("nan")
        with redirect_stdout(io.StringIO()):
            self.assertTrue(math.isnan(rebind.refresh_balance(self.settings, 1000)))
            self.assertTrue(math.isnan(self.admin(1000)["amount"]))
        self.assertFalse(rebind.balance_allows_hosted())

    def test_failed_refresh_is_throttled_scheduler_unknown_admin_keeps_last_good(self):
        self.value = 0.5
        with redirect_stdout(io.StringIO()):
            self.assertEqual(rebind.refresh_balance(self.settings, 1000), 0.5)
            self.assertEqual(self.admin(1000)["amount"], 0.5)
            self.error = httpx.ReadTimeout("private-test-key sentinel-answer")
            now = 1000 + rebind.BALANCE_READ_SECONDS
            self.assertIsNone(rebind.refresh_balance(self.settings, now))
            self.assertTrue(rebind.balance_allows_hosted())
            row = self.admin(now)
            count = len(self.requests)
            self.assertIsNone(rebind.refresh_balance(self.settings, now + 1))
        self.assertEqual(len(self.requests), count)
        self.assertEqual(
            (row["amount"], row["read_at"], row["last_read_failed"]), (0.5, 1000, True)
        )

    def test_malformed_answer_does_not_leak_private_context(self):
        self.malformed = True
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertIsNone(rebind.refresh_balance(self.settings, 1000))
            row = self.admin(1000)
        self.assertTrue(row["last_read_failed"])
        self.assertNotIn("private-test-key", out.getvalue())
        self.assertNotIn("invalid", out.getvalue())

    def tick(self, now, *, native=True):
        service = SimpleNamespace(
            settings=self.settings,
            repo=SimpleNamespace(clock=lambda: now),
            capacity=SimpleNamespace(
                client=SimpleNamespace(backend_api_key=lambda _: "unused")
            )
            if native
            else None,
        )

        class ImmediateThread:
            def __init__(self, target, **_):
                self.target = target

            def start(self):
                self.target()

        def claim(_):
            # All vendor reads must happen before entering the claim transaction.
            count = len(self.requests)
            self.assertTrue(count > 0 or not self.settings.everyframe_enabled)
            return {"state": "idle"}, None

        with (
            patch.object(balances.threading, "Thread", ImmediateThread),
            patch.object(dstack_demand_start, "_claim", side_effect=claim),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(dstack_demand_start.start_once(service), {"state": "idle"})

    def paths(self, suffix):
        return [r for r in self.requests if r.url.path.endswith(suffix)]

    def test_controller_composes_one_key_read_with_one_separate_catalog_read(self):
        self.tick(1000)
        self.assertEqual(len(self.paths("/key")), 1)
        self.assertEqual(len(self.paths("/render/models")), 1)
        self.assertEqual(self.paths("/key")[0].extensions["timeout"]["read"], 5)
        self.assertEqual(
            self.paths("/render/models")[0].extensions["timeout"]["read"], 15
        )
        self.tick(1001)
        self.assertEqual(len(self.paths("/key")), 1)
        self.tick(1000 + rebind.BALANCE_READ_SECONDS)
        self.assertEqual(len(self.paths("/key")), 2)
        self.assertEqual(len(self.paths("/render/models")), 2)

    def test_supplied_failures_never_trigger_a_second_key_request(self):
        for status, malformed, error in (
            (500, False, None),
            (200, True, None),
            (200, False, httpx.ReadTimeout("private-test-key")),
        ):
            with self.subTest(
                status=status, malformed=malformed, transport_error=bool(error)
            ):
                self.status, self.malformed, self.error = status, malformed, error
                self.requests.clear()
                rebind._balance.update(usd=None, read_at=None)
                # Make the admin refresh due without changing the scheduler cache.
                from pathlib import Path

                path = Path(self.settings.data_dir) / balances.FILENAME
                if path.exists():
                    path.unlink()
                self.tick(1000)
                self.assertEqual(len(self.paths("/key")), 1)
                self.assertEqual(len(self.paths("/render/models")), 1)
                self.assertTrue(rebind.balance_allows_hosted())
                self.assertTrue(
                    balances.read_stored(self.settings)["rows"][0]["last_read_failed"]
                )

    def test_single_response_retains_different_scheduler_and_admin_projections(self):
        self.status, self.value = 201, 0.5
        self.tick(1000)
        self.assertTrue(rebind.balance_allows_hosted())
        row = balances.read_stored(self.settings)["rows"][0]
        self.assertEqual((row["amount"], row["read_at"]), (0.5, 1000))
        self.assertEqual(len(self.paths("/key")), 1)
        self.status, self.value = 200, False
        self.tick(1000 + rebind.BALANCE_READ_SECONDS)
        self.assertFalse(rebind.balance_allows_hosted())
        row = balances.read_stored(self.settings)["rows"][0]
        self.assertEqual(
            (row["amount"], row["read_at"], row["last_read_failed"]), (0.5, 1000, True)
        )

    def test_cached_success_keeps_actual_observation_time_and_failed_successor_does_not_replace_it(
        self,
    ):
        self.value = 0.5
        self.tick(1000, native=False)
        self.tick(2000)
        row = balances.read_stored(self.settings)["rows"][0]
        self.assertEqual((row["amount"], row["read_at"]), (0.5, 1000))
        self.assertEqual(len(self.paths("/key")), 1)
        self.error = httpx.ReadError("private-test-key")
        self.tick(2000 + rebind.BALANCE_READ_SECONDS)
        row = balances.read_stored(self.settings)["rows"][0]
        self.assertEqual(
            (row["amount"], row["read_at"], row["last_read_failed"]), (0.5, 1000, True)
        )
        self.assertTrue(rebind.balance_allows_hosted())
        self.assertEqual(len(self.paths("/key")), 2)

    def test_disabled_scheduler_keeps_standalone_admin_read(self):
        self.settings.everyframe_enabled = False
        self.tick(1000)
        self.assertEqual(len(self.paths("/key")), 1)
        self.assertEqual(self.paths("/key")[0].extensions["timeout"]["read"], 15)
        self.assertIsNone(rebind.balance_observation(self.settings))

    def test_pending_background_keeps_an_immutable_observation_and_no_balance_repr(
        self,
    ):
        from dataclasses import FrozenInstanceError

        targets = []

        class DeferredThread:
            def __init__(self, target, **_):
                self.target = target

            def start(self):
                targets.append(self.target)

        self.value = 23.45
        rebind.refresh_balance(self.settings, 1000)
        sample = rebind.balance_observation(self.settings)
        self.assertNotIn("23.45", repr(sample))
        with self.assertRaises(FrozenInstanceError):
            sample.amount = 10
        with patch.object(balances.threading, "Thread", DeferredThread):
            self.assertTrue(
                balances.refresh_in_background(
                    self.settings, None, 1001, everyframe_balance=sample
                )
            )
            self.assertFalse(
                balances.refresh_in_background(
                    self.settings, None, 1002, everyframe_balance=sample
                )
            )
        try:
            self.value = 42
            rebind.refresh_balance(self.settings, 1000 + rebind.BALANCE_READ_SECONDS)
            with redirect_stdout(io.StringIO()):
                targets[0]()
        finally:
            if balances._running.locked():
                balances._running.release()
        row = balances.read_stored(self.settings)["rows"][0]
        self.assertEqual((row["amount"], row["read_at"]), (23.45, 1000))
        self.assertEqual(len(self.paths("/key")), 2)


if __name__ == "__main__":
    unittest.main()
