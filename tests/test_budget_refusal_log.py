"""A refusal the API answers as plain "capacity_unavailable" keeps its real reason in the log (#1246)."""

from types import SimpleNamespace
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from studio_platform.api_errors import register_handlers
from studio_platform.repository import BudgetExceeded
from studio_platform.storage_asset_journal import AssetQuotaExceeded


class BudgetRefusalLogTests(unittest.TestCase):
    def client(self, error):
        app = FastAPI()
        repo = SimpleNamespace(engine=None, clock=lambda: 0.0)
        register_handlers(app, repository=repo, settings=SimpleNamespace(tenant_id="t"))

        @app.post("/v1/projects/p/storyboard/preflights")
        def fail():
            raise error

        return TestClient(app)

    def test_the_reason_is_logged_and_the_reply_stays_generic(self):
        cases = (
            (BudgetExceeded("pool_disabled"), "kind=BudgetExceeded code=pool_disabled"),
            (
                AssetQuotaExceeded("素材容量或处理并发已达到上限"),
                "kind=AssetQuotaExceeded code=other",
            ),
        )
        for error, expected in cases:
            with self.assertLogs("studio_platform.api_errors", "WARNING") as logs:
                reply = self.client(error).post("/v1/projects/p/storyboard/preflights")
            self.assertEqual(reply.status_code, 429)
            self.assertEqual(reply.json()["code"], "capacity_unavailable")
            self.assertIn(expected, logs.output[0])
            self.assertIn("path_kind=preflights", logs.output[0])
            # The message text itself never reaches the log.
            self.assertNotIn("素材", logs.output[0])


if __name__ == "__main__":
    unittest.main()
