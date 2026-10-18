"""In-process HTTP contract tests with temporary state and no sockets."""
import json
from pathlib import Path
import secrets
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from studio_platform.agent_connect import AgentConnect, PROFILE_ID, PROFILE_VERSION
from studio_platform.agent_connect_routes import register_routes
from studio_platform.auth import Auth, digest
from studio_platform.repository import Repository
from studio_platform.settings import Settings


class AgentConnectRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = Repository("sqlite:///" + (root / "test.sqlite").as_posix())
        self.addCleanup(self.repo.close)
        self.auth = Auth(self.repo.engine, mode="local-test")
        self.origin = "http://127.0.0.1:8850"
        self.session = self.auth.login("superdan", "", source="dan")
        app = FastAPI()
        app.state.auth = self.auth
        app.state.settings = Settings(root, auth_mode="local-test")
        @app.middleware("http")
        async def identity(request, call_next):
            bearer = request.headers.get("authorization", "")
            request.state.principal = (self.auth.bearer(bearer[7:]) if bearer.startswith("Bearer ")
                else self.auth.session(request.cookies.get("sixnine_session")))
            return await call_next(request)
        register_routes(app)
        self.app = app
        self.client = TestClient(app, base_url=self.origin)
        self.addCleanup(self.client.close)
        self.client.cookies.set("sixnine_session", self.session)

    def issue(self, *, headers=None):
        return self.client.post("/v1/account/agent-connections", json={"name": "Codex",
            "authorization_profile_id": PROFILE_ID, "authorization_profile_version": PROFILE_VERSION},
            headers=headers or {"Origin": self.origin, "Idempotency-Key": secrets.token_hex(12)})

    def material(self, grant):
        token, verifier = "sxp_" + secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        return token, verifier, {"code": grant["code"], "client_challenge": digest(verifier),
            "token_hash": digest(token), "key_prefix": token[:12],
            "expected_authorization_fingerprint": grant["connection"]["authorization_fingerprint"]}

    def test_browser_issue_list_anonymous_exchange_and_revoke(self):
        issued = self.issue()
        self.assertEqual(issued.status_code, 201)
        grant = issued.json()
        listing = self.client.get("/v1/account/agent-connections")
        self.assertEqual(listing.status_code, 200)
        self.assertNotIn(grant["code"], listing.text)
        self.assertEqual(listing.json()["authorization_profile"]["id"], PROFILE_ID)
        token, _, body = self.material(grant)
        anon = TestClient(self.app, base_url=self.origin)
        self.addCleanup(anon.close)
        result = anon.post("/v1/agent-connect/exchange", json=body)
        self.assertEqual(result.status_code, 200)
        self.assertNotIn(token, result.text)
        self.assertNotIn("api_key", result.json())
        response = self.client.delete("/v1/account/agent-connections/" + grant["connection"]["id"],
            headers={"Origin": self.origin})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.auth.bearer(token))

    def test_source_is_strict_for_issue_revoke_and_exchange(self):
        for headers in ({"Origin": "https://evil.example", "Idempotency-Key": "origin"},
                {"Idempotency-Key": "no-origin"},
                {"Origin": self.origin, "Sec-Fetch-Site": "cross-site", "Idempotency-Key": "crosssite"}):
            self.assertEqual(self.issue(headers=headers).status_code, 403)
        grant = self.issue().json()
        _, _, body = self.material(grant)
        self.assertEqual(self.client.post("/v1/agent-connect/exchange", json=body,
            headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.delete("/v1/account/agent-connections/" + grant["connection"]["id"],
            headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/v1/account/agent-connections/" + grant["connection"]["id"]).json()["status"], "pending")

    def test_exchange_strict_json_extras_and_errors_do_not_reflect_secrets(self):
        grant = self.issue().json()
        token, verifier, body = self.material(grant)
        response = self.client.post("/v1/agent-connect/exchange", json=body | {"owner": "supervan", "api_key": token})
        self.assertEqual(response.status_code, 422)
        for secret in (token, verifier, grant["code"]):
            self.assertNotIn(secret, response.text)
        response = self.client.post("/v1/agent-connect/exchange", content=json.dumps(body),
            headers={"Content-Type": "text/plain"})
        self.assertEqual(response.status_code, 415)
        response = self.client.post("/v1/agent-connect/exchange", content="{",
            headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/v1/agent-connect/exchange", content='{"code":"' + "x" * 9000 + '"}',
            headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)

    def test_machine_cannot_manage_connections_and_pending_owner_isolated(self):
        grant = self.issue().json()
        token, _, body = self.material(grant)
        self.client.post("/v1/agent-connect/exchange", json=body)
        machine = TestClient(self.app, base_url=self.origin, headers={"Authorization": "Bearer " + token})
        self.addCleanup(machine.close)
        self.assertEqual(machine.get("/v1/account/agent-connections").status_code, 403)
        self.assertEqual(machine.post("/v1/account/agent-connections", json={}, headers={"Origin": self.origin}).status_code, 403)
        van_token = self.auth.login("supervan", "", source="van")
        self.client.cookies.set("sixnine_session", van_token)
        self.assertEqual(self.client.get("/v1/account/agent-connections/" + grant["connection"]["id"]).status_code, 404)

    def test_lost_issuance_response_does_not_return_original_code(self):
        headers = {"Origin": self.origin, "Idempotency-Key": "stable-issue"}
        first = self.issue(headers=headers).json()
        second = self.issue(headers=headers).json()
        self.assertEqual(first["connection"]["id"], second["connection"]["id"])
        self.assertIsNone(second["code"])
        self.assertFalse(second["code_available"])


if __name__ == "__main__":
    unittest.main()
