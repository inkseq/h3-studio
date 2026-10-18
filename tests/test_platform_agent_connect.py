"""Disposable SQL authorization tests. No network, GPU or real credentials."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import insert, select, update

from studio_platform.agent_connect import (AgentConnect, ConnectError, PROFILE_ID, PROFILE_VERSION,
    CODE_TTL_SECONDS, RECOVERY_TTL_SECONDS, audit, connections, exchange_limits)
from studio_platform.auth import Auth, AuthenticationError, Principal, accounts, digest, personal_keys
from studio_platform.repository import Repository
from test_platform_repository import LedgerCase


class AgentConnectTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.auth = Auth(self.repo.engine, mode="local-test", clock=lambda: self.now)
        self.service = AgentConnect(self.auth)
        self.session = self.auth.login("superdan", "", source="dan")
        self.principal = self.auth.session(self.session)
        self.van_session = self.auth.login("supervan", "", source="van")
        self.van = self.auth.session(self.van_session)
        self.origin = "http://127.0.0.1:8850"

    def issue(self, *, principal=None, idempotency_key=None, **extra):
        return self.service.issue(principal or self.principal, name="Codex",
            authorization_profile_id=PROFILE_ID, authorization_profile_version=PROFILE_VERSION,
            idempotency_key=idempotency_key or secrets.token_hex(16), origin=self.origin, **extra)

    def material(self, grant):
        token, verifier = "sxp_" + secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        return token, verifier, {"code": grant["code"], "client_challenge": digest(verifier),
            "token_hash": digest(token), "key_prefix": token[:12], "source": "agent-one",
            "expected_authorization_fingerprint": grant["connection"]["authorization_fingerprint"], "origin": self.origin}

    def count(self, table):
        with self.repo.engine.connect() as conn:
            return len(conn.execute(select(table)).all())

    def test_uses_existing_pat_auth_and_only_hashes_persist(self):
        grant = self.issue()
        token, verifier, body = self.material(grant)
        result = self.service.exchange(**body)
        self.assertEqual(result["connection"]["status"], "connected")
        self.assertNotIn("api_key", result)
        principal = self.auth.bearer(token)
        self.assertEqual(principal.owner, "superdan")
        self.assertTrue(principal.all_projects)
        self.assertIn("assistant:run", principal.scopes)
        with self.repo.engine.connect() as conn:
            stored = [dict(row) for table in (connections, personal_keys, audit, exchange_limits)
                for row in conn.execute(select(table)).mappings()]
        raw = json.dumps(stored)
        for secret in (grant["code"], token, verifier, self.session):
            self.assertNotIn(secret, raw)
        self.assertEqual(self.count(personal_keys), 1)
        self.assertEqual(result["key"]["expires_at"], grant["connection"]["authorization"]["key_expires_at"])

    def test_issue_replay_same_record_no_code_and_changed_body_conflicts(self):
        first = self.issue(idempotency_key="issue-one")
        second = self.issue(idempotency_key="issue-one")
        self.assertEqual(first["connection"]["id"], second["connection"]["id"])
        self.assertIsNone(second["code"])
        self.assertFalse(second["code_available"])
        self.assertEqual(self.count(connections), 1)
        with self.assertRaisesRegex(ConnectError, "idempotency_conflict"):
            self.service.issue(self.principal, name="Changed", authorization_profile_id=PROFILE_ID,
                authorization_profile_version=PROFILE_VERSION, idempotency_key="issue-one", origin=self.origin)

    def test_grant_freezes_scopes_and_key_expiry_across_policy_change(self):
        grant = self.issue()
        token, _, body = self.material(grant)
        with patch("studio_platform.agent_connect.CONNECT_SCOPES", ("jobs:read",)), patch("studio_platform.agent_connect.KEY_LIFETIME_DAYS", 1):
            result = self.service.exchange(**body)
        self.assertEqual(result["key"]["scopes"], grant["connection"]["authorization"]["scopes"])
        self.assertEqual(result["key"]["expires_at"], grant["connection"]["authorization"]["key_expires_at"])
        self.assertIn("assistant:run", self.auth.bearer(token).scopes)

    def test_consumed_grant_without_verifier_cannot_mint_or_recover(self):
        grant = self.issue()
        _, _, body = self.material(grant)
        self.service.exchange(**body)
        with self.assertRaisesRegex(ConnectError, "connection_consumed"):
            self.service.exchange(**body)
        self.assertEqual(self.count(personal_keys), 1)

    def test_lost_response_recovery_returns_exact_key_and_fails_wrong_verifier(self):
        grant = self.issue()
        _, verifier, body = self.material(grant)
        first = self.service.exchange(**body)
        self.now += 20
        recovered = self.service.exchange(**body, recovery_verifier=verifier)
        self.assertTrue(recovered["recovered"])
        self.assertEqual(first["key"]["id"], recovered["key"]["id"])
        self.assertEqual(first["key"]["expires_at"], recovered["key"]["expires_at"])
        with self.assertRaises(ConnectError):
            self.service.exchange(**body, recovery_verifier=secrets.token_urlsafe(32))
        self.assertEqual(self.count(personal_keys), 1)

    def test_recovery_never_creates_an_unclaimed_key(self):
        grant = self.issue()
        _, verifier, body = self.material(grant)
        with self.assertRaisesRegex(ConnectError, "connection_not_exchanged"):
            self.service.exchange(**body, recovery_verifier=verifier)
        self.assertEqual(self.count(personal_keys), 0)

    def test_expiry_and_recovery_expiry_do_not_extend_validity(self):
        grant = self.issue()
        _, _, body = self.material(grant)
        self.now += CODE_TTL_SECONDS
        with self.assertRaisesRegex(ConnectError, "connection_expired"):
            self.service.exchange(**body)
        self.assertEqual(self.count(personal_keys), 0)
        grant = self.issue()
        _, verifier, body = self.material(grant)
        first = self.service.exchange(**body)
        self.now += RECOVERY_TTL_SECONDS
        with self.assertRaisesRegex(ConnectError, "connection_consumed"):
            self.service.exchange(**body, recovery_verifier=verifier)
        self.assertEqual(self.count(personal_keys), 1)

    def test_parallel_agents_exactly_one_key_and_proof_recovers_winner(self):
        grant = self.issue()
        materials = [self.material(grant) for _ in range(8)]
        def run(entry):
            token, verifier, body = entry
            try:
                return (True, token, verifier, body, self.service.exchange(**body))
            except ConnectError:
                return (False, None, None, None, None)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, materials))
        winners = [result for result in results if result[0]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.count(personal_keys), 1)
        _, token, verifier, body, result = winners[0]
        body["source"] = "recovery-new-source"
        recovered = self.service.exchange(**body, recovery_verifier=verifier)
        self.assertEqual(result["key"]["id"], recovered["key"]["id"])
        self.assertEqual(self.auth.bearer(token).owner, "superdan")

    def test_wrong_account_tenant_and_machine_cannot_manage(self):
        grant = self.issue()
        connection_id = grant["connection"]["id"]
        for method in (self.service.get, self.service.revoke):
            with self.assertRaisesRegex(ConnectError, "connection_not_found"):
                method(self.van, connection_id)
        self.assertEqual(self.service.list(self.van)["connections"], [])
        other = AgentConnect(Auth(self.repo.engine, tenant="another", mode="local-test", clock=lambda: self.now))
        _, _, body = self.material(grant)
        with self.assertRaises(ConnectError):
            other.exchange(**body)
        machine = Principal("superdan", "key:fake", True, all_projects=True)
        with self.assertRaises(AuthenticationError):
            self.issue(principal=machine)

    def test_wrong_authorization_fingerprint_or_origin_preserves_grant(self):
        grant = self.issue()
        _, _, body = self.material(grant)
        for changes in ({"expected_authorization_fingerprint": "0" * 64}, {"origin": "https://other.example"}):
            with self.assertRaisesRegex(ConnectError, "connection_authorization_mismatch"):
                self.service.exchange(**(body | changes))
        self.assertEqual(self.count(personal_keys), 0)
        self.assertEqual(self.service.exchange(**body)["connection"]["status"], "connected")

    def test_logout_before_exchange_and_revocation_fail_closed(self):
        grant = self.issue()
        token, verifier, body = self.material(grant)
        self.auth.logout(self.session)
        with self.assertRaisesRegex(ConnectError, "connection_authority_changed"):
            self.service.exchange(**body)
        self.session = self.auth.login("superdan", "", source="newlogin")
        self.principal = self.auth.session(self.session)
        grant = self.issue()
        token, verifier, body = self.material(grant)
        result = self.service.exchange(**body)
        self.service.revoke(self.principal, grant["connection"]["id"])
        self.assertIsNone(self.auth.bearer(token))
        with self.assertRaises(ConnectError):
            self.service.exchange(**body, recovery_verifier=verifier)
        self.assertTrue(self.service.revoke(self.principal, grant["connection"]["id"])["revoked"])
        self.assertEqual(self.count(personal_keys), 1)

    def test_legacy_pat_manager_revocation_blocks_recovery(self):
        grant = self.issue()
        token, verifier, body = self.material(grant)
        result = self.service.exchange(**body)
        self.auth.revoke_key("superdan", result["key"]["id"])
        self.assertEqual(self.service.get(self.principal, grant["connection"]["id"])["status"], "revoked")
        self.assertIsNone(self.auth.bearer(token))
        with self.assertRaises(ConnectError):
            self.service.exchange(**body, recovery_verifier=verifier)

    def test_disabled_account_and_password_rotation_invalidate_code_and_key(self):
        auth = Auth(self.repo.engine, mode="password", clock=lambda: self.now)
        password = secrets.token_urlsafe(18)
        for owner in ("superdan", "supervan"):
            auth.set_password(owner, password)
        browser = auth.session(auth.login("superdan", password, source="password"))
        service = AgentConnect(auth)
        grant = service.issue(browser, name="Codex", authorization_profile_id=PROFILE_ID,
            authorization_profile_version=PROFILE_VERSION, idempotency_key="password-issue", origin=self.origin)
        token, verifier, body = self.material(grant)
        service.exchange(**body)
        auth.set_password("superdan", secrets.token_urlsafe(18))
        self.assertIsNone(auth.bearer(token))
        with self.assertRaisesRegex(ConnectError, "connection_authority_changed"):
            service.exchange(**body, recovery_verifier=verifier)

    def test_source_limiter_is_atomic_and_invalid_codes_have_no_per_code_rows(self):
        def attempt(_):
            try:
                self.service.reserve_exchange("same-source", code="sxc_" + secrets.token_urlsafe(32))
                return True
            except ConnectError:
                return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(attempt, range(20)))
        self.assertEqual(sum(outcomes), 10)
        self.assertEqual(self.count(exchange_limits), 1)
        self.now += 300
        self.service.reserve_exchange("same-source")

    def test_pending_capacity_and_profile_validation_do_not_change_auth(self):
        for _ in range(5):
            self.issue()
        with self.assertRaisesRegex(ConnectError, "too_many_pending_connections"):
            self.issue()
        with self.assertRaisesRegex(ConnectError, "connection_profile_changed"):
            self.service.issue(self.principal, authorization_profile_id="creator-admin", authorization_profile_version=1,
                idempotency_key="wrong", origin=self.origin)
        self.assertEqual(self.count(personal_keys), 0)

    def test_token_hash_collision_rolls_back_claim_and_does_not_mint_second_key(self):
        grant_one, grant_two = self.issue(), self.issue()
        token, verifier, body = self.material(grant_one)
        self.service.exchange(**body)
        second = body | {"code": grant_two["code"], "expected_authorization_fingerprint": grant_two["connection"]["authorization_fingerprint"]}
        with self.assertRaisesRegex(ConnectError, "connection_key_conflict"):
            self.service.exchange(**second)
        self.assertEqual(self.service.get(self.principal, grant_two["connection"]["id"])["status"], "pending")
        self.assertEqual(self.count(personal_keys), 1)

    def test_parallel_issue_same_operation_returns_one_code_and_one_record(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.issue(idempotency_key="one-click"), range(8)))
        self.assertEqual(sum(row["code_available"] for row in results), 1)
        self.assertEqual(len({row["connection"]["id"] for row in results}), 1)
        self.assertEqual(self.count(connections), 1)

    def test_fifty_existing_keys_blocks_claim_without_consuming_code(self):
        for index in range(50):
            self.auth.create_key("superdan", authenticated_session=self.principal, name="Existing " + str(index),
                scopes=["projects:read"], all_projects=True)
        grant = self.issue()
        _, _, body = self.material(grant)
        with self.assertRaisesRegex(ConnectError, "too_many_active_keys"):
            self.service.exchange(**body)
        self.assertEqual(self.count(personal_keys), 50)
        self.assertEqual(self.service.get(self.principal, grant["connection"]["id"])["status"], "pending")

    def test_connection_limiter_bounds_recovery_from_multiple_sources(self):
        grant = self.issue()
        for index in range(20):
            self.service.reserve_exchange("distributed-" + str(index), code=grant["code"])
        with self.assertRaisesRegex(ConnectError, "connection_exchange_limited"):
            self.service.reserve_exchange("distributed-new-source", code=grant["code"])
        self.now += 300
        self.service.reserve_exchange("distributed-after-window", code=grant["code"])

    def test_existing_keys_do_not_receive_assistant_scope(self):
        old = self.auth.create_key("superdan", authenticated_session=self.principal,
            name="Existing", scopes=["projects:read"], all_projects=True)
        self.issue()
        self.assertNotIn("assistant:run", self.auth.bearer(old["api_key"]).scopes)


if __name__ == "__main__":
    unittest.main()
