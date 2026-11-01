"""Provider balance reads: real reply shapes through a fake transport."""

import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx

from studio_platform.providers import balances

REPLIES = {
    "console.vast.ai": {"credit": 42.5},
    "api.runpod.io": {"data": {"myself": {"clientBalance": 7.25}}},
    "lium.io": {"balance": 101.5},
    "api.cloudrift.ai": {"data": {"balance": 965}},
    "api.everyframe.studio": {"balanceUsd": 2.93},
}


class Client:
    def __init__(self, broken=()):
        self.broken = broken

    def backend_api_key(self, backend):
        if backend in self.broken:
            raise ValueError("not configured")
        return "backend-key-" + backend


class BalanceTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.settings = SimpleNamespace(data_dir=work.name)
        patch = mock.patch.object(
            balances,
            "load_config",
            return_value=SimpleNamespace(api_key="ef-test-key-0123456789"),
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.seen = []

    def transport(self, refuse=()):
        def handler(request):
            self.seen.append(request)
            if request.url.host in refuse:
                return httpx.Response(500, json={"error": "nope"})
            return httpx.Response(200, json=REPLIES[request.url.host])

        return httpx.MockTransport(handler)

    def rows(self):
        return {
            row["provider"]: row for row in balances.read_stored(self.settings)["rows"]
        }

    def read(self, *, now=1000, client=None, refuse=()):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            balances.read_all(
                self.settings,
                client or Client(),
                now=now,
                transport=self.transport(refuse),
            )
        return out.getvalue()

    def test_reads_every_provider_with_an_api_and_marks_low_ones(self):
        self.read()
        rows = self.rows()
        self.assertEqual(rows["vast"]["amount"], 42.5)
        self.assertFalse(rows["vast"]["low"])
        self.assertEqual(rows["runpod"]["amount"], 7.25)
        self.assertTrue(rows["runpod"]["low"])
        self.assertEqual(rows["lium"]["amount"], 101.5)
        self.assertEqual(rows["everyframe"]["amount"], 2.93)
        self.assertTrue(rows["everyframe"]["low"])
        self.assertEqual(rows["cloudrift"]["amount"], 965)
        self.assertEqual(rows["cloudrift"]["unit"], "raw")
        self.assertFalse(rows["cloudrift"]["low"])

    def test_a_provider_without_an_api_shows_a_link_never_a_number(self):
        self.read()
        for name in ("modal", "openrouter", "aws", "google"):
            row = self.rows()[name]
            self.assertFalse(row["readable"])
            self.assertIsNone(row["amount"])
            self.assertTrue(row["console_url"].startswith("https://"))

    def test_a_failed_read_keeps_the_last_value_and_says_so(self):
        self.read(now=1000)
        self.read(now=2000, refuse=("lium.io",), client=Client(broken=("vastai",)))
        rows = self.rows()
        for name in ("lium", "vast"):
            self.assertTrue(rows[name]["last_read_failed"])
            self.assertEqual(rows[name]["read_at"], 1000)
        self.assertEqual(rows["lium"]["amount"], 101.5)
        self.assertEqual(rows["runpod"]["read_at"], 2000)
        self.assertFalse(rows["runpod"]["last_read_failed"])

    def test_on_call_lines_name_the_provider_but_never_an_amount_or_key(self):
        out = self.read()
        self.assertIn("provider_balance_low runpod", out)
        self.assertNotIn("7.25", out)
        self.assertNotIn("key", out)
        stored = (Path(self.settings.data_dir) / balances.FILENAME).read_text()
        self.assertNotIn("backend-key", stored)
        self.assertNotIn("ef-test-key", stored)

    def test_each_key_goes_only_to_its_own_vendor(self):
        self.read()
        sent = {}
        for request in self.seen:
            sent[request.url.host] = (
                request.headers.get("authorization") or request.headers["x-api-key"]
            )
        self.assertIn("vastai", sent["console.vast.ai"])
        self.assertIn("runpod", sent["api.runpod.io"])
        self.assertIn("lium", sent["lium.io"])
        self.assertIn("cloudrift", sent["api.cloudrift.ai"])
        self.assertIn("ef-test-key", sent["api.everyframe.studio"])

    def test_reads_only_when_due(self):
        started = []

        class Thread:
            def __init__(self, target, **_):
                pass

            def start(self):
                started.append(1)
                balances._running.release()

        with mock.patch.object(balances.threading, "Thread", Thread):
            self.assertTrue(
                balances.refresh_in_background(self.settings, Client(), 1000)
            )
            self.read(now=1000)
            self.assertFalse(
                balances.refresh_in_background(
                    self.settings, Client(), 1000 + balances.READ_SECONDS - 1
                )
            )
            self.assertTrue(
                balances.refresh_in_background(
                    self.settings, Client(), 1000 + balances.READ_SECONDS
                )
            )
        self.assertEqual(len(started), 2)

    def test_a_read_in_progress_blocks_a_second_one(self):
        self.assertTrue(balances._running.acquire(blocking=False))
        try:
            self.assertFalse(
                balances.refresh_in_background(self.settings, Client(), 1000)
            )
        finally:
            balances._running.release()


if __name__ == "__main__":
    unittest.main()
