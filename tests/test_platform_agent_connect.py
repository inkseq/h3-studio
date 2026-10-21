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

from studio_platform.agent_connect import (
    AgentConnect,
    ConnectError,
    PROFILE_ID,
    PROFILE_VERSION,
    CODE_TTL_SECONDS,
    RECOVERY_TTL_SECONDS,
    audit,
    connections,
    exchange_limits,
)
from studio_platform.auth import (
    Auth,
    AuthenticationError,
    Principal,
    accounts,
    digest,
    personal_keys,
)
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
        return self.service.issue(
            principal or self.principal,
            name="Codex",
            authorization_profile_id=PROFILE_ID,
            authorization_profile_version=PROFILE_VERSION,
            idempotency_key=idempotency_key or secrets.token_hex(16),
            origin=self.origin,
            **extra,
        )

    def material(self, grant):
        token, verifier = "sxp_" + secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        return (
            token,
            verifier,
            {
                "code": grant["code"],
                "client_challenge": digest(verifier),
                "token_hash": digest(token),
                "key_prefix": token[:12],
                "source": "agent-one",
                "expected_authorization_fingerprint": grant["connection"][
                    "authorization_fingerprint"
                ],
                "origin": self.origin,
            },
        )

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
            stored = [
                dict(row)
                for table in (connections, personal_keys, audit, exchange_limits)
                for row in conn.execute(select(table)).mappings()
            ]
        raw = json.dumps(stored)
        for secret in (grant["code"], token, verifier, self.session):
            self.assertNotIn(secret, raw)
        self.assertEqual(self.count(personal_keys), 1)
        self.assertEqual(
            result["key"]["expires_at"],
            grant["connection"]["authorization"]["key_expires_at"],
        )

    def test_issue_replay_same_record_no_code_and_changed_body_conflicts(self):
        first = self.issue(idempotency_key="issue-one")
        second = self.issue(idempotency_key="issue-one")
        self.assertEqual(first["connection"]["id"], second["connection"]["id"])
        self.assertIsNone(second["code"])
        self.assertFalse(second["code_available"])
        self.assertEqual(self.count(connections), 1)
        with self.assertRaisesRegex(ConnectError, "idempotency_conflict"):
            self.service.issue(
                self.principal,
                name="Changed",
                authorization_profile_id=PROFILE_ID,
                authorization_profile_version=PROFILE_VERSION,
                idempotency_key="issue-one",
                origin=self.origin,
            )

    def test_grant_freezes_scopes_and_key_expiry_across_policy_change(self):
        grant = self.issue()
        token, _, body = self.material(grant)
        with (
            patch("studio_platform.agent_connect.CONNECT_SCOPES", ("jobs:read",)),
            patch("studio_platform.agent_connect.KEY_LIFETIME_DAYS", 1),
        ):
            result = self.service.exchange(**body)
        self.assertEqual(
            result["key"]["scopes"], grant["connection"]["authorization"]["scopes"]
        )
        self.assertEqual(
            result["key"]["expires_at"],
            grant["connection"]["authorization"]["key_expires_at"],
        )
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
        other = AgentConnect(
            Auth(
                self.repo.engine,
                tenant="another",
                mode="local-test",
                clock=lambda: self.now,
            )
        )
        _, _, body = self.material(grant)
        with self.assertRaises(ConnectError):
            other.exchange(**body)
        machine = Principal("superdan", "key:fake", True, all_projects=True)
        with self.assertRaises(AuthenticationError):
            self.issue(principal=machine)

    def test_wrong_authorization_fingerprint_or_origin_preserves_grant(self):
        grant = self.issue()
        _, _, body = self.material(grant)
        for changes in (
            {"expected_authorization_fingerprint": "0" * 64},
            {"origin": "https://other.example"},
        ):
            with self.assertRaisesRegex(
                ConnectError, "connection_authorization_mismatch"
            ):
                self.service.exchange(**(body | changes))
        self.assertEqual(self.count(personal_keys), 0)
        self.assertEqual(
            self.service.exchange(**body)["connection"]["status"], "connected"
        )

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
        self.assertTrue(
            self.service.revoke(self.principal, grant["connection"]["id"])["revoked"]
        )
        self.assertEqual(self.count(personal_keys), 1)

    def test_legacy_pat_manager_revocation_blocks_recovery(self):
        grant = self.issue()
        token, verifier, body = self.material(grant)
        result = self.service.exchange(**body)
        self.auth.revoke_key("superdan", result["key"]["id"])
        self.assertEqual(
            self.service.get(self.principal, grant["connection"]["id"])["status"],
            "revoked",
        )
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
        grant = service.issue(
            browser,
            name="Codex",
            authorization_profile_id=PROFILE_ID,
            authorization_profile_version=PROFILE_VERSION,
            idempotency_key="password-issue",
            origin=self.origin,
        )
        token, verifier, body = self.material(grant)
        service.exchange(**body)
        auth.set_password("superdan", secrets.token_urlsafe(18))
        self.assertIsNone(auth.bearer(token))
        with self.assertRaisesRegex(ConnectError, "connection_authority_changed"):
            service.exchange(**body, recovery_verifier=verifier)

    def test_source_limiter_is_atomic_and_invalid_codes_have_no_per_code_rows(self):
        def attempt(_):
            try:
                self.service.reserve_exchange(
                    "same-source", code="sxc_" + secrets.token_urlsafe(32)
                )
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
            self.service.issue(
                self.principal,
                authorization_profile_id="creator-admin",
                authorization_profile_version=1,
                idempotency_key="wrong",
                origin=self.origin,
            )
        self.assertEqual(self.count(personal_keys), 0)

    def test_token_hash_collision_rolls_back_claim_and_does_not_mint_second_key(self):
        grant_one, grant_two = self.issue(), self.issue()
        token, verifier, body = self.material(grant_one)
        self.service.exchange(**body)
        second = body | {
            "code": grant_two["code"],
            "expected_authorization_fingerprint": grant_two["connection"][
                "authorization_fingerprint"
            ],
        }
        with self.assertRaisesRegex(ConnectError, "connection_key_conflict"):
            self.service.exchange(**second)
        self.assertEqual(
            self.service.get(self.principal, grant_two["connection"]["id"])["status"],
            "pending",
        )
        self.assertEqual(self.count(personal_keys), 1)

    def test_parallel_issue_same_operation_returns_one_code_and_one_record(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(lambda _: self.issue(idempotency_key="one-click"), range(8))
            )
        self.assertEqual(sum(row["code_available"] for row in results), 1)
        self.assertEqual(len({row["connection"]["id"] for row in results}), 1)
        self.assertEqual(self.count(connections), 1)

    def test_fifty_existing_keys_blocks_claim_without_consuming_code(self):
        for index in range(50):
            self.auth.create_key(
                "superdan",
                authenticated_session=self.principal,
                name="Existing " + str(index),
                scopes=["projects:read"],
                all_projects=True,
            )
        grant = self.issue()
        _, _, body = self.material(grant)
        with self.assertRaisesRegex(ConnectError, "too_many_active_keys"):
            self.service.exchange(**body)
        self.assertEqual(self.count(personal_keys), 50)
        self.assertEqual(
            self.service.get(self.principal, grant["connection"]["id"])["status"],
            "pending",
        )

    def test_connection_limiter_bounds_recovery_from_multiple_sources(self):
        grant = self.issue()
        for index in range(20):
            self.service.reserve_exchange(
                "distributed-" + str(index), code=grant["code"]
            )
        with self.assertRaisesRegex(ConnectError, "connection_exchange_limited"):
            self.service.reserve_exchange("distributed-new-source", code=grant["code"])
        self.now += 300
        self.service.reserve_exchange("distributed-after-window", code=grant["code"])

    def test_existing_keys_do_not_receive_assistant_scope(self):
        old = self.auth.create_key(
            "superdan",
            authenticated_session=self.principal,
            name="Existing",
            scopes=["projects:read"],
            all_projects=True,
        )
        self.issue()
        self.assertNotIn("assistant:run", self.auth.bearer(old["api_key"]).scopes)

    def telegram_issue(self, **extra):
        return self.issue(client="telegram", **extra)

    def telegram_material(self, grant, *, user_id="123456789", nonce=None):
        token, verifier, body = self.material(grant)
        body["telegram_user"] = {
            "id": user_id,
            "username": "film_maker",
            "display_name": "Film Maker",
        }
        if nonce is not None:
            body["telegram_nonce"] = nonce
        return token, verifier, body

    def test_legacy_authorization_bytes_and_fingerprint_stay_unchanged(self):
        with patch("studio_platform.agent_connect.uuid.uuid4") as ident:
            ident.return_value.hex = "a" * 32
            grant = self.issue(idempotency_key="legacy-frozen")
        expected = {
            "origin": self.origin,
            "owner": "superdan",
            "tenant": self.service.tenant,
            "connection_id": "connection-" + "a" * 32,
            "profile_id": "creator-full",
            "profile_version": 1,
            "scopes": [
                "assets:read",
                "assets:write",
                "assistant:run",
                "jobs:read",
                "jobs:write",
                "projects:create",
                "projects:read",
                "projects:write",
            ],
            "all_projects": True,
            "project_ids": [],
            "key_lifetime_days": 90,
            "key_expires_at": self.now + 90 * 86400,
        }
        encoded = json.dumps(expected, sort_keys=True, separators=(",", ":"))
        self.assertEqual(grant["connection"]["authorization"], expected)
        self.assertEqual(
            grant["connection"]["authorization_fingerprint"], digest(encoded)
        )
        with self.repo.engine.connect() as conn:
            stored = conn.execute(select(connections.c.authorization)).scalar_one()
        self.assertEqual(stored, encoded)
        _, _, body = self.material(grant)
        self.assertEqual(
            self.service.exchange(**body)["connection"]["status"], "connected"
        )

    def test_telegram_preview_has_only_frozen_grant_and_no_browser_identity(self):
        nonce = secrets.token_urlsafe(32)
        grant = self.telegram_issue(telegram_nonce=nonce)
        with self.repo.engine.connect() as conn:
            before = dict(conn.execute(select(connections)).mappings().one())
        result = self.service.preview(
            code=grant["code"], origin=self.origin, source="preview"
        )
        self.assertEqual(
            set(result), {"connection_id", "authorization", "authorization_fingerprint"}
        )
        self.assertEqual(result["connection_id"], grant["connection"]["id"])
        self.assertEqual(result["authorization"], grant["connection"]["authorization"])
        self.assertEqual(
            result["authorization_fingerprint"],
            digest(
                json.dumps(
                    result["authorization"], sort_keys=True, separators=(",", ":")
                )
            ),
        )
        self.assertRegex(result["authorization"]["owner"], r"^account-[0-9a-f]{32}$")
        self.assertEqual(result["authorization"]["telegram_nonce_hash"], digest(nonce))
        for secret in (nonce, grant["code"], "superdan", self.session):
            self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(self.count(personal_keys), 0)
        self.assertEqual(self.count(audit), 1)
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                dict(conn.execute(select(connections)).mappings().one()), before
            )
        for value in ("not-a-code", self.issue()["code"]):
            with self.assertRaises(ConnectError) as raised:
                self.service.preview(
                    code=value, origin=self.origin, source="preview-unknown"
                )
            self.assertEqual(raised.exception.status_code, 404)

    def test_telegram_preview_expiry_revoke_consumption_and_origin(self):
        grant = self.telegram_issue()
        with self.assertRaisesRegex(ConnectError, "connection_authorization_mismatch"):
            self.service.preview(
                code=grant["code"], origin="https://other.example", source="origin"
            )
        self.now += CODE_TTL_SECONDS
        with self.assertRaises(ConnectError) as raised:
            self.service.preview(
                code=grant["code"], origin=self.origin, source="expired"
            )
        self.assertEqual(raised.exception.status_code, 403)
        grant = self.telegram_issue()
        self.service.revoke(self.principal, grant["connection"]["id"])
        with self.assertRaises(ConnectError) as raised:
            self.service.preview(
                code=grant["code"], origin=self.origin, source="revoked"
            )
        self.assertEqual(raised.exception.status_code, 410)
        grant = self.telegram_issue()
        self.service.exchange(**self.telegram_material(grant)[2])
        with self.assertRaises(ConnectError) as raised:
            self.service.preview(
                code=grant["code"], origin=self.origin, source="consumed"
            )
        self.assertEqual(raised.exception.status_code, 410)

    def test_telegram_preview_logout_account_and_tenant_fail_closed(self):
        grant = self.telegram_issue()
        self.auth.logout(self.session)
        with self.assertRaisesRegex(ConnectError, "connection_authority_changed"):
            self.service.preview(
                code=grant["code"], origin=self.origin, source="logged-out"
            )
        other = AgentConnect(
            Auth(
                self.repo.engine,
                tenant="other",
                mode="local-test",
                clock=lambda: self.now,
            )
        )
        with self.assertRaises(ConnectError) as raised:
            other.preview(code=grant["code"], origin=self.origin, source="other-tenant")
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.count(personal_keys), 0)

    def test_telegram_preview_uses_original_source_and_connection_rate_limits(self):
        grant = self.telegram_issue()
        for _ in range(10):
            self.service.preview(
                code=grant["code"], origin=self.origin, source="same-source"
            )
        with self.assertRaises(ConnectError) as raised:
            self.service.preview(
                code=grant["code"], origin=self.origin, source="same-source"
            )
        self.assertEqual(raised.exception.status_code, 429)
        for index in range(10):
            self.service.preview(
                code=grant["code"], origin=self.origin, source="other-" + str(index)
            )
        with self.assertRaises(ConnectError) as raised:
            self.service.preview(
                code=grant["code"], origin=self.origin, source="new-source"
            )
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(self.count(personal_keys), 0)

    def test_telegram_nonce_is_hashed_bound_and_part_of_issue_idempotency(self):
        nonce = secrets.token_urlsafe(32)
        grant = self.telegram_issue(
            telegram_nonce=nonce, idempotency_key="telegram-bound"
        )
        replay = self.telegram_issue(
            telegram_nonce=nonce, idempotency_key="telegram-bound"
        )
        self.assertEqual(grant["connection"]["id"], replay["connection"]["id"])
        with self.assertRaisesRegex(ConnectError, "idempotency_conflict"):
            self.telegram_issue(
                telegram_nonce=secrets.token_urlsafe(32),
                idempotency_key="telegram-bound",
            )
        _, _, body = self.telegram_material(grant)
        for extra in ({}, {"telegram_nonce": secrets.token_urlsafe(32)}):
            with self.assertRaisesRegex(
                ConnectError, "connection_telegram_nonce_mismatch"
            ):
                self.service.exchange(**(body | extra))
        self.assertEqual(self.count(personal_keys), 0)
        body["telegram_nonce"] = nonce
        self.service.exchange(**body)
        with self.repo.engine.connect() as conn:
            persisted = [
                dict(row) for row in conn.execute(select(connections)).mappings()
            ]
        self.assertNotIn(nonce, json.dumps(persisted))
        self.assertEqual(
            persisted[0]["authorization_fingerprint"],
            grant["connection"]["authorization_fingerprint"],
        )

    def test_telegram_web_first_claim_keeps_canonical_pat_owner_and_metadata(self):
        grant = self.telegram_issue()
        token, _, body = self.telegram_material(grant)
        exchanged = self.service.exchange(**body)
        current = self.service.get(self.principal, grant["connection"]["id"])
        self.assertEqual(current["client"], "telegram")
        self.assertEqual(current["claimed_telegram_user"], body["telegram_user"])
        self.assertEqual(
            exchanged["connection"]["claimed_telegram_user"], body["telegram_user"]
        )
        self.assertEqual(current["authorization"], grant["connection"]["authorization"])
        self.assertEqual(self.auth.bearer(token).owner, "superdan")
        self.assertEqual(self.service.list(self.van)["connections"], [])
        self.assertEqual(
            self.service.list(self.principal)["connections"][0][
                "claimed_telegram_user"
            ],
            body["telegram_user"],
        )

    def test_telegram_recovery_requires_same_claim_nonce_and_saved_material(self):
        nonce = secrets.token_urlsafe(32)
        grant = self.telegram_issue(telegram_nonce=nonce)
        _, verifier, body = self.telegram_material(grant, nonce=nonce)
        first = self.service.exchange(**body)
        for changes, error in (
            ({"telegram_user": {"id": "999"}}, "connection_telegram_user_mismatch"),
            (
                {"telegram_nonce": secrets.token_urlsafe(32)},
                "connection_telegram_nonce_mismatch",
            ),
            ({"token_hash": "0" * 64}, "connection_consumed"),
        ):
            with self.assertRaisesRegex(ConnectError, error):
                self.service.exchange(**(body | changes), recovery_verifier=verifier)
        recovered = self.service.exchange(
            **(body | {"telegram_user": {"id": "123456789"}}),
            recovery_verifier=verifier,
        )
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["key"]["id"], first["key"]["id"])
        self.assertEqual(
            recovered["connection"]["claimed_telegram_user"], body["telegram_user"]
        )
        self.assertEqual(self.count(personal_keys), 1)

    def test_telegram_metadata_is_strict_and_legacy_grants_cannot_change_client(self):
        grant = self.telegram_issue()
        _, _, body = self.telegram_material(grant)
        invalid = (
            {"id": 123},
            {"id": "0"},
            {"id": "01"},
            {"id": "9223372036854775808"},
            {"id": "123", "username": "bad name"},
            {"id": "123", "display_name": "bad\nname"},
            {"id": "123", "extra": "field"},
        )
        for index, claim in enumerate(invalid):
            with self.assertRaisesRegex(
                ConnectError, "connection_telegram_user_invalid"
            ):
                self.service.exchange(
                    **(
                        body
                        | {"telegram_user": claim, "source": "invalid-" + str(index)}
                    )
                )
        self.assertEqual(self.count(personal_keys), 0)
        legacy = self.issue()
        with self.assertRaisesRegex(ConnectError, "connection_client_mismatch"):
            self.service.exchange(**self.telegram_material(legacy)[2])
        for fields in (
            {"client": "other"},
            {"telegram_nonce": secrets.token_urlsafe(32)},
        ):
            with self.assertRaises(ConnectError):
                self.issue(**fields)

    def test_parallel_telegram_claim_binds_only_winner_and_rolls_back_conflicts(self):
        grant = self.telegram_issue()
        materials = [
            self.telegram_material(grant, user_id=str(index + 1)) for index in range(8)
        ]

        def claim(entry):
            token, verifier, body = entry
            try:
                return token, verifier, body, self.service.exchange(**body)
            except ConnectError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            winners = [
                result for result in pool.map(claim, materials) if result is not None
            ]
        self.assertEqual(len(winners), 1)
        _, verifier, body, result = winners[0]
        self.assertEqual(
            result["connection"]["claimed_telegram_user"], body["telegram_user"]
        )
        recovered = self.service.exchange(
            **(body | {"source": "recovery"}), recovery_verifier=verifier
        )
        self.assertEqual(recovered["key"]["id"], result["key"]["id"])
        self.assertEqual(self.count(personal_keys), 1)

    def test_telegram_key_collision_preserves_unclaimed_grant_and_no_claim_metadata(
        self,
    ):
        first = self.telegram_issue()
        _, _, original_body = self.telegram_material(first)
        self.service.exchange(**original_body)
        second = self.telegram_issue()
        collision = original_body | {
            "code": second["code"],
            "expected_authorization_fingerprint": second["connection"][
                "authorization_fingerprint"
            ],
            "telegram_user": {"id": "222"},
        }
        with self.assertRaisesRegex(ConnectError, "connection_key_conflict"):
            self.service.exchange(**collision)
        current = self.service.get(self.principal, second["connection"]["id"])
        self.assertEqual(current["status"], "pending")
        self.assertIsNone(current["claimed_telegram_user"])
        self.assertEqual(self.count(personal_keys), 1)
        self.service.exchange(**self.telegram_material(second, user_id="333")[2])
        self.assertEqual(
            self.service.get(self.principal, second["connection"]["id"])[
                "claimed_telegram_user"
            ]["id"],
            "333",
        )

    def test_telegram_claim_survives_pause_cap_and_revocation_controls(self):
        grant = self.telegram_issue()
        _, _, body = self.telegram_material(grant)
        self.service.exchange(**body)
        ident = grant["connection"]["id"]
        before = self.service.get(self.principal, ident)
        paused = self.service.change_connection(
            self.principal,
            ident,
            {"expected_version": before["state_version"], "paused": True},
            "pause-telegram",
        )
        self.assertTrue(paused["paused"])
        self.assertEqual(paused["claimed_telegram_user"], body["telegram_user"])
        capped = self.service.change_connection(
            self.principal,
            ident,
            {"expected_version": paused["state_version"], "daily_limit_microusd": 10},
            "cap-telegram",
        )
        self.assertEqual(capped["claimed_telegram_user"], body["telegram_user"])
        self.service.revoke(self.principal, ident)
        ended = self.service.get(self.principal, ident)
        self.assertEqual(ended["status"], "revoked")
        self.assertEqual(ended["claimed_telegram_user"], body["telegram_user"])


if __name__ == "__main__":
    unittest.main()
