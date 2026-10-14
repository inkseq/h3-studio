"""HTTP identity, pagination and CSRF fences for human host policy access."""
from pathlib import Path
import secrets
import unittest

from fastapi.testclient import TestClient

from studio_platform.api import create_app
from studio_platform.settings import Settings
from test_platform_repository import LedgerCase


class OperatorHostRouteTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(Settings(Path(self.temp.name), auth_mode="local-test",
            tenant_id=self.scope.tenant_id, database_url=self.url,
            operator_capacity_owners=("superdan", "supervan"), operator_admin_owners=("superdan",)), repository=self.repo)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def login(self, owner):
        self.client.post("/api/auth/login", json={"username": owner}).raise_for_status()

    def test_cookie_scope_admin_and_machine_separation(self):
        for path in ("/v1/operator/hosts", "/v1/operator/admin/hosts", "/v1/operator/admin/hosts/audit"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.login("supervan")
        own = self.client.get("/v1/operator/hosts")
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(own.json()["account"], "supervan")
        self.assertFalse(own.json()["admin"])
        self.assertEqual(own.json()["hosts"], [])
        self.assertEqual(own.headers["cache-control"], "no-store")
        for path in ("/v1/operator/admin/hosts", "/v1/operator/admin/hosts/audit"):
            self.assertEqual(self.client.get(path).status_code, 403)
        token = secrets.token_urlsafe(32)
        self.app.state.auth.register_client("host-route-pat", token, "superdan", ["host-route-test-project"], ["jobs:read"])
        for path in ("/v1/operator/hosts", "/v1/operator/admin/hosts", "/v1/operator/admin/hosts/audit"):
            self.assertEqual(self.client.get(path, headers={"Authorization": "Bearer " + token}).status_code, 403)
        self.login("superdan")
        admin = self.client.get("/v1/operator/admin/hosts")
        self.assertEqual(admin.status_code, 200, admin.text)
        self.assertTrue(admin.json()["admin"])
        self.assertEqual(self.client.get("/v1/operator/admin/hosts/audit").status_code, 200)

    def test_bounded_pages_expected_account_and_cross_site_writes(self):
        self.login("superdan")
        for path in ("/v1/operator/hosts", "/v1/operator/admin/hosts", "/v1/operator/admin/hosts/audit"):
            for suffix in ("?limit=0", "?limit=101", "?offset=-1", "?offset=100001"):
                self.assertEqual(self.client.get(path + suffix).status_code, 422)
            self.assertEqual(self.client.get(path, headers={"X-Expected-Account": "supervan"}).status_code, 409)
        path = "/v1/operator/admin/hosts/policy"
        for headers, status in (({"Origin": "https://untrusted.example"}, 403),
                ({"Sec-Fetch-Site": "cross-site"}, 403),
                ({"X-Expected-Account": "supervan"}, 409)):
            self.assertEqual(self.client.post(path, json={}, headers={"Idempotency-Key": "blocked-host-policy", **headers}).status_code, status)
        self.assertEqual(self.client.post(path, json={}).status_code, 422)
        self.assertEqual(self.client.post(path, json={"provider": "http://localhost/secrets"},
            headers={"Idempotency-Key": "invalid-host-policy"}).status_code, 422)

    def test_numeric_admin_lookup_does_not_fabricate_observation(self):
        self.login("superdan")
        response = self.client.get("/v1/operator/admin/hosts?machine_id=19492")
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()["hosts"][0]
        self.assertEqual(row["machine_id"], "19492")
        self.assertFalse(row["identity_confirmed"])
        self.assertEqual(row["physical_id_source"], "operator_supplied_numeric")
        self.assertEqual(row["runs"], 0)
        self.assertIsNone(row["last_observed_at"])
        for machine in ("0", "-1", "01", "a", "19492%22", "9999999999999999999"):
            self.assertEqual(self.client.get("/v1/operator/admin/hosts?machine_id=" + machine).status_code, 422)


if __name__ == "__main__":
    unittest.main()
