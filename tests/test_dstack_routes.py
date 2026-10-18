"""Cookie authorization, CSRF and idempotent hold API seam; fake service only."""
from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient
from studio_platform.api import create_app
from studio_platform.settings import Settings
from studio_platform.dstack_operator import DstackOperator

class DstackRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings=Settings(Path(self.tmp.name),auth_mode="local-test",operator_capacity_owners=("superdan",))
        self.app=create_app(settings)
        self.client=TestClient(self.app)
        self.client.__enter__(); self.addCleanup(self.client.__exit__,None,None,None)
    def login(self,who="superdan"):
        self.client.post("/api/auth/login",json={"username":who}).raise_for_status()
    def test_disabled_state_is_authenticated_and_authoritative(self):
        self.assertEqual(self.client.get("/v1/operator/dstack/state").status_code,401)
        self.login()
        response=self.client.get("/v1/operator/dstack/state")
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["operator"]["account"],"superdan")
        self.assertFalse(response.json()["enabled"])
        self.assertEqual(response.headers["cache-control"],"no-store")
    def test_other_account_and_cross_site_mutation_are_rejected(self):
        self.login("supervan")
        self.assertEqual(self.client.get("/v1/operator/dstack/catalog").status_code,403)
        self.login()
        self.assertEqual(self.client.post("/v1/operator/dstack/starts",json={"preview_id":"x"},
            headers={"Origin":"https://untrusted.example","Idempotency-Key":"only-key"}).status_code,403)
    def test_hold_requires_idempotency_header_before_service(self):
        self.login()
        self.assertEqual(self.client.post("/v1/operator/dstack/nodes/original-node/hold",json={"hold_seconds":60}).status_code,422)
