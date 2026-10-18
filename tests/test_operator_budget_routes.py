"""Authenticated owner/admin seams, exact-account and CSRF denial; no providers."""

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from studio_platform.api import create_app
from studio_platform.settings import Settings


class BudgetRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(
            Path(self.tmp.name),
            auth_mode="local-test",
            operator_capacity_owners=("superdan",),
            operator_admin_owners=("superdan",),
        )
        self.app = create_app(self.settings)
        self.repo = self.app.state.repository
        self.addCleanup(self.repo.close)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        for account, owner in (
            ("owner-dan", "superdan"),
            ("owner-van", "supervan"),
            ("global", None),
        ):
            self.repo.configure_budget(
                account, tenant_id="sixnine", owner_id=owner, limit_microusd=1000000
            )

    def login(self, owner):
        self.client.post("/api/auth/login", json={"username": owner}).raise_for_status()

    def test_owner_reads_only_own_even_without_machine_start_permission(self):
        self.assertEqual(self.client.get("/v1/operator/budgets").status_code, 401)
        self.login("supervan")
        response = self.client.get("/v1/operator/budgets")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [row["id"] for row in response.json()["budgets"]], ["owner-van"]
        )
        self.assertEqual(response.json()["account"], "supervan")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/v1/operator/admin/budgets").status_code, 403)
        self.assertEqual(
            self.client.put(
                "/v1/operator/admin/budgets/owner-dan", json={}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/v1/operator/budgets", headers={"X-Expected-Account": "superdan"}
            ).status_code,
            409,
        )

    def test_admin_cas_reads_existing_only_and_csrf_blocks_mutation(self):
        self.login("superdan")
        rows = self.client.get("/v1/operator/admin/budgets").json()["budgets"]
        self.assertEqual(
            {row["id"] for row in rows}, {"owner-dan", "owner-van", "global"}
        )
        old = next(row for row in rows if row["id"] == "owner-van")
        body = {"expected_snapshot": old["snapshot"], "limit_microusd": 1200000}
        denied = self.client.put(
            "/v1/operator/admin/budgets/owner-van",
            json=body,
            headers={"Origin": "https://untrusted.example"},
        )
        self.assertEqual(denied.status_code, 403)
        result = self.client.put("/v1/operator/admin/budgets/owner-van", json=body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["changed"])
        self.assertEqual(
            self.client.put(
                "/v1/operator/admin/budgets/owner-van", json=body
            ).status_code,
            409,
        )
        audit = self.client.get("/v1/operator/admin/budgets/audit").json()["events"]
        self.assertEqual(audit[0]["actor"]["owner_id"], "superdan")
        self.assertEqual(audit[0]["old"]["limit_microusd"], 1000000)
        self.assertEqual(audit[0]["new"]["limit_microusd"], 1200000)
        self.assertEqual(
            self.client.put(
                "/v1/operator/admin/budgets/missing", json=body
            ).status_code,
            404,
        )
