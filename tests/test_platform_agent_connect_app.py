"""Actual app middleware integration, entirely in-process and disposable."""
from pathlib import Path
import secrets
import tempfile
import unittest
import time

from fastapi.testclient import TestClient

from studio_platform.api import create_app
from studio_platform.auth import digest
from studio_platform.settings import Settings
from studio_platform.repository import jobs
from sqlalchemy import select
from test_platform_repository import LedgerCase


class AgentConnectAppTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.origin = "http://127.0.0.1:8850"
        self.app = create_app(Settings(Path(self.temp.name), auth_mode="local-test", database_url=self.url), repository=self.repo)
        self.client = TestClient(self.app, base_url=self.origin)
        self.addCleanup(self.client.close)
        response = self.client.post("/api/auth/login", json={"username": "superdan"}, headers={"Origin": self.origin})
        self.assertEqual(response.status_code, 200)

    def grant(self):
        profile = self.client.get("/v1/account/agent-connections").json()["authorization_profile"]
        response = self.client.post("/v1/account/agent-connections", json={"name": "Codex",
            "authorization_profile_id": profile["id"], "authorization_profile_version": profile["version"]},
            headers={"Origin": self.origin, "Idempotency-Key": secrets.token_hex(12)})
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_public_discovery_helper_then_anonymous_exchange_uses_real_auth(self):
        anon = TestClient(self.app, base_url=self.origin)
        self.addCleanup(anon.close)
        manifest = anon.get("/for-agents/connect-manifest.json")
        self.assertEqual(manifest.status_code, 200)
        helper = anon.get("/for-agents/connect.py")
        self.assertEqual(helper.status_code, 200)
        self.assertTrue(len(helper.content) > 1000)
        grant = self.grant()
        token, verifier = "sxp_" + secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        body = {"code": grant["code"], "client_challenge": digest(verifier), "token_hash": digest(token),
            "key_prefix": token[:12], "expected_authorization_fingerprint": grant["connection"]["authorization_fingerprint"]}
        exchanged = anon.post("/v1/agent-connect/exchange", json=body)
        self.assertEqual(exchanged.status_code, 200)
        machine = TestClient(self.app, base_url=self.origin, headers={"Authorization": "Bearer " + token})
        self.addCleanup(machine.close)
        response = machine.get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["username"], "superdan")
        self.assertTrue(response.json()["machine"])
        self.assertIn("assistant:run", response.json()["scopes"])
        self.assertEqual(machine.get("/v1/account/agent-connections").status_code, 403)
        self.assertEqual(anon.get("/v1/projects").status_code, 401)

    def test_cross_site_guard_rejects_code_and_private_apis_stay_private(self):
        grant = self.grant()
        token, verifier = "sxp_" + secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        anon = TestClient(self.app, base_url=self.origin)
        self.addCleanup(anon.close)
        response = anon.post("/v1/agent-connect/exchange", json={"code": grant["code"],
            "client_challenge": digest(verifier), "token_hash": digest(token), "key_prefix": token[:12],
            "expected_authorization_fingerprint": grant["connection"]["authorization_fingerprint"]},
            headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.app.state.auth.bearer(token))
        self.assertEqual(anon.post("/v1/account/agent-connections", json={}, headers={"Origin": self.origin}).status_code, 401)
        self.assertEqual(anon.get("/openapi.json").status_code, 401)
        self.assertFalse(self.client.get("/healthz").json()["generation_enabled"])

    def machine(self, grant):
        token, verifier = "sxp_" + secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        anon = TestClient(self.app, base_url=self.origin)
        self.addCleanup(anon.close)
        body = {"code": grant["code"], "client_challenge": digest(verifier), "token_hash": digest(token),
            "key_prefix": token[:12], "expected_authorization_fingerprint": grant["connection"]["authorization_fingerprint"]}
        response = anon.post("/v1/agent-connect/exchange", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        client = TestClient(self.app, base_url=self.origin, headers={"Authorization": "Bearer " + token})
        self.addCleanup(client.close)
        return client

    def test_connected_agent_shares_real_session_and_card_with_owner_and_revocation_preserves_them(self):
        grant = self.grant()
        machine = self.machine(grant)
        created = machine.post("/v1/quick-chat/sessions", json={"title": "Connected author"},
            headers={"Idempotency-Key": "connected-session"})
        self.assertEqual(created.status_code, 201, created.text)
        session = created.json()["session"]
        base = "/v1/quick-chat/sessions/" + session["id"]
        response = machine.post(base + "/turns", json={"expected_version": session["version"],
            "model_id": session["model_id"], "assistant_mode": "none", "create_card": True,
            "text": "A blue paper boat moves slowly on a pond."}, headers={"Idempotency-Key": "connected-card"})
        self.assertEqual(response.status_code, 201, response.text)
        card_id = response.json()["card_id"]
        card = base + "/cards/" + card_id
        self.assertEqual(machine.get(card).json(), self.client.get(card).json())
        van = TestClient(self.app, base_url=self.origin)
        self.addCleanup(van.close)
        van.post("/api/auth/login", json={"username": "supervan"}, headers={"Origin": self.origin}).raise_for_status()
        self.assertEqual(van.get(base).status_code, 404)
        self.assertEqual(van.get(card).status_code, 404)
        before = self.client.get(card).json()
        revoked = self.client.delete("/v1/account/agent-connections/" + grant["connection"]["id"],
            headers={"Origin": self.origin})
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(machine.get(card).status_code, 401)
        self.assertEqual(self.client.get(card).json(), before)
        with self.repo.engine.connect() as conn:
            self.assertEqual(conn.execute(select(jobs.c.id)).all(), [])

    def test_chunked_oversized_exchange_is_rejected_before_consuming_grant(self):
        grant = self.grant()
        anon = TestClient(self.app, base_url=self.origin)
        self.addCleanup(anon.close)
        response = anon.post("/v1/agent-connect/exchange", content=iter([b'{"code":"', b'x'*8192, b'"}']),
            headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.client.get("/v1/account/agent-connections/" + grant["connection"]["id"]).json()["status"], "pending")
        self.assertEqual(anon.get("/v1/agent-connect/exchange").status_code, 401)
        self.assertEqual(self.machine(grant).get("/api/auth/me").status_code, 200)


if __name__ == "__main__":
    unittest.main()
