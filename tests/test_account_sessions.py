"""Offline session authority, provider outcome recovery and account isolation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import delete, func, insert, select

from studio_platform.account_sessions import AccountSessions, SessionError
from studio_platform.auth import (
    Auth,
    AuthenticationError,
    Principal,
    SessionUnavailable,
    digest,
    sessions,
)
from studio_platform.identity_schema import metadata, users
from studio_platform.repository import documents
from studio_platform.session_authority import advance, read
from studio_platform.workos_auth import WorkOSAuth
from test_platform_repository import LedgerCase


class Provider:
    def __init__(self, clock):
        self.clock = clock
        self.active, self.calls, self.revokes = {}, [], []
        self.failure, self.after_revoke, self.page_size = None, None, 100
        self.client_id = "client_" + "A" * 24

    def client(self, _config):
        return SimpleNamespace(user_management=self, close=lambda: None)

    def list_sessions(self, subject, *, limit, after, order):
        self.calls.append((subject, limit, after, order))
        if self.failure:
            raise RuntimeError("PRIVATE provider outage")
        rows = list(self.active.values())
        start = next((i + 1 for i, row in enumerate(rows) if row.id == after), 0)
        chosen = rows[start : start + self.page_size]
        cursor = chosen[-1].id if chosen and start + len(chosen) < len(rows) else None
        return SimpleNamespace(data=chosen, list_metadata=SimpleNamespace(after=cursor))

    def add(self, sid, subject, *, created=900):
        self.active[sid] = SimpleNamespace(
            id=sid,
            user_id=subject,
            status="active",
            ended_at=None,
            created_at=datetime.fromtimestamp(created, timezone.utc),
            expires_at=datetime.fromtimestamp(self.clock() + 5000, timezone.utc),
        )

    def revoke_session(self, *, session_id):
        self.revokes.append(session_id)
        self.active.pop(session_id, None)
        if self.after_revoke:
            raise RuntimeError("PRIVATE lost response")

    def get_logout_url(self, *, session_id, return_to):
        from urllib.parse import urlencode

        return "https://api.workos.com/user_management/sessions/logout?" + urlencode(
            {"session_id": session_id, "return_to": return_to}
        )


class AccountSessionTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.auth = Auth(
            self.repo.engine,
            tenant=self.scope.tenant_id,
            mode="local-test",
            clock=lambda: self.now,
        )
        self.subject = "user_" + "A" * 24
        self.user_id = str(uuid.uuid4())
        self.settings = SimpleNamespace(
            tenant_id=self.scope.tenant_id, public_origin="https://staging.inkseq.com"
        )
        with self.repo.transaction() as conn:
            metadata.create_all(conn)
            for owner, ident, subject in (
                ("superdan", str(uuid.uuid4()), None),
                ("supervan", str(uuid.uuid4()), None),
                ("creator", self.user_id, self.subject),
            ):
                conn.execute(
                    insert(users).values(
                        id=ident,
                        tenant=self.scope.tenant_id,
                        owner=owner,
                        role="user",
                        status="active",
                        display_name=owner,
                        email=None,
                        email_verified=0,
                        created_at=self.now,
                        updated_at=self.now,
                        workos_user_id=subject,
                    )
                )
        self.provider = Provider(lambda: self.now)
        self.workos = WorkOSAuth(
            self.repo, self.auth, self.settings, sdk_factory=self.provider.client
        )
        self.workos.config = lambda: {
            "client_id": self.provider.client_id,
            "api_key": "offline-unused",
        }
        self.service = AccountSessions(self.repo, self.auth, self.settings, self.workos)
        self.auth.session_checker = self.service.check

    def hosted(self, *, sid=None, metadata=True):
        sid = sid or "session_" + uuid.uuid4().hex
        self.provider.add(sid, self.subject)
        binding = {
            "sid": sid,
            "subject": self.subject,
            "client_id": self.provider.client_id,
            "access_expires_at": int(self.now) + 900,
        }
        return self.auth.issue_workos_session(
            "creator",
            self.subject,
            provider_session=binding if metadata else None,
            captured_epoch=self.auth.capture_epoch(),
        )

    def operation(self, token, key="once"):
        return self.service.revoke_all(self.auth.session(token), {}, key)

    def saved(self, ident):
        with self.repo.engine.connect() as conn:
            return read(conn, self.scope.tenant_id, "revocation", ident)

    def test_local_all_sessions_including_current_and_replay_preserve_other_owner(self):
        one = self.auth.login("superdan", "")
        two = self.auth.login("superdan", "", source="second")
        other = self.auth.login("supervan", "")
        result = self.operation(one)
        self.assertEqual(result["state"], "completed")
        self.assertIsNone(self.auth.session(one))
        self.assertIsNone(self.auth.session(two))
        self.assertIsNotNone(self.auth.session(other))
        fresh = self.auth.login("superdan", "", source="third")
        self.assertEqual(self.operation(fresh), result)
        self.assertIsNotNone(self.auth.session(fresh))
        self.assertEqual(self.provider.calls, [])

    def test_epoch_captured_before_password_verification_prevents_late_inflight_login(
        self,
    ):
        original = self.auth.login("superdan", "")
        reserve = self.auth.reserve_login

        def race(source):
            reserve(source)
            self.operation(original)

        with patch.object(self.auth, "reserve_login", side_effect=race):
            with self.assertRaises(AuthenticationError):
                self.auth.login("superdan", "", source="racing")
        self.assertIsNone(self.auth.session(original))
        self.assertIsNotNone(
            self.auth.session(self.auth.login("superdan", "", source="new"))
        )

    def test_epoch_before_hosted_verification_not_fresh_commit_timestamp(self):
        captured = self.auth.capture_epoch()
        token = self.hosted()
        self.operation(token)
        self.now += 1
        with self.assertRaises(AuthenticationError):
            self.auth.issue_workos_session(
                "creator",
                self.subject,
                provider_session={
                    "sid": "session_" + "Z" * 24,
                    "subject": self.subject,
                    "client_id": self.provider.client_id,
                    "access_expires_at": int(self.now) + 900,
                },
                captured_epoch=captured,
            )

    def test_provider_check_cache_expires_and_complete_absence_revokes(self):
        token = self.hosted()
        self.assertIsNotNone(self.auth.session(token))
        calls = len(self.provider.calls)
        self.provider.active.clear()
        self.now += 59
        self.assertIsNotNone(self.auth.session(token))
        self.assertEqual(len(self.provider.calls), calls)
        self.now += 1
        self.assertIsNone(self.auth.session(token))
        with self.repo.engine.connect() as conn:
            self.assertIsNone(
                conn.execute(
                    select(sessions).where(sessions.c.token_hash == digest(token))
                ).first()
            )

    def test_check_outage_after_cache_expiry_refuses_without_deleting_or_leaking(self):
        token = self.hosted()
        self.auth.session(token)
        self.now += 60
        self.provider.failure = True
        with self.assertRaises(SessionUnavailable) as result:
            self.auth.session(token)
        self.assertNotIn("PRIVATE", str(result.exception))
        with self.repo.engine.connect() as conn:
            self.assertIsNotNone(
                conn.execute(
                    select(sessions).where(sessions.c.token_hash == digest(token))
                ).first()
            )
        self.provider.failure = None
        self.assertIsNotNone(self.auth.session(token))

    def test_sidless_hosted_reauth_and_missing_binding_does_not_call_provider(self):
        token = self.hosted(metadata=False)
        self.assertIsNone(self.auth.session(token))
        self.assertEqual(self.provider.calls, [])

    def test_pagination_never_revokes_session_missing_from_first_page(self):
        self.provider.page_size = 1
        self.provider.add("session_" + "A" * 24, self.subject)
        token = self.hosted(sid="session_" + "B" * 24)
        self.assertIsNotNone(self.auth.session(token))
        self.assertEqual(len(self.provider.calls), 2)

    def test_incomplete_or_corrupt_provider_inventory_fails_closed(self):
        token = self.hosted()
        self.provider.page_size = 1
        for i in range(11):
            self.provider.add("session_" + str(i).zfill(24), self.subject)
        with self.assertRaises(SessionUnavailable):
            self.auth.session(token)
        self.provider.active = {
            "bad": SimpleNamespace(
                id="session_" + "X" * 24,
                user_id="user_" + "Z" * 24,
                status="active",
                ended_at=None,
            )
        }
        with self.assertRaises(SessionUnavailable):
            self.auth.session(token)

    def test_provider_network_has_no_open_database_transaction(self):
        token = self.hosted()
        original = self.provider.list_sessions

        def independent(*args, **kwargs):
            with self.repo.transaction() as conn:
                advance(conn, self.scope.tenant_id, "other-unrelated", self.now)
            return original(*args, **kwargs)

        self.provider.list_sessions = independent
        self.assertIsNotNone(self.auth.session(token))

    def test_unknown_revoke_is_reconciled_from_original_sid_without_second_post(self):
        token = self.hosted()
        self.provider.after_revoke = True
        result = self.operation(token)
        self.assertEqual(result["state"], "pending")
        self.assertIn("logout_url", result)
        self.assertIsNone(self.auth.session(token))
        ident = result["operation_id"]
        self.assertFalse(self.service.turn(self.scope.tenant_id, ident)["done"])
        self.assertTrue(self.service.turn(self.scope.tenant_id, ident)["done"])
        self.assertEqual(len(self.provider.revokes), 1)
        self.assertEqual(
            list(self.saved(ident)["provider_sessions"].values()), ["revoked"]
        )

    def test_new_provider_and_local_sessions_after_cutoff_survive_original_operation(
        self,
    ):
        token = self.hosted()
        result = self.operation(token)
        self.now += 1
        fresh = self.hosted(sid="session_" + "N" * 24)
        self.provider.active["session_" + "N" * 24].created_at = datetime.fromtimestamp(
            self.now, timezone.utc
        )
        self.service.turn(self.scope.tenant_id, result["operation_id"])
        self.service.turn(self.scope.tenant_id, result["operation_id"])
        self.assertIsNotNone(self.auth.session(fresh))
        self.assertNotIn("session_" + "N" * 24, self.provider.revokes)

    def test_provider_outage_persists_pending_original_without_new_epoch_or_id(self):
        token = self.hosted()
        result = self.operation(token)
        self.provider.failure = True
        for _ in range(2):
            self.assertFalse(
                self.service.turn(self.scope.tenant_id, result["operation_id"])["done"]
            )
        saved = self.saved(result["operation_id"])
        self.assertEqual(saved["epoch"], 1)
        self.assertEqual(saved["error_code"], "session_provider_unavailable")
        self.assertEqual(self.provider.revokes, [])

    def test_machine_cross_owner_invalid_body_and_invalid_key_refuse_without_revocation(
        self,
    ):
        token = self.auth.login("superdan", "")
        principal = self.auth.session(token)
        for actor, body, key in (
            (Principal("superdan", "machine", machine=True), {}, "key"),
            (principal, {"owner": "supervan"}, "key"),
            (principal, {}, None),
            (
                Principal(
                    "supervan",
                    "wrong",
                    user_id=principal.user_id,
                    session_hash=principal.session_hash,
                ),
                {},
                "key",
            ),
        ):
            with (
                self.subTest(actor=actor.actor_id, body=body, key=key),
                self.assertRaises(SessionError),
            ):
                self.service.revoke_all(actor, body, key)
        self.assertIsNotNone(self.auth.session(token))

    def test_simultaneous_revocations_have_single_epoch_and_original_operation(self):
        token = self.auth.login("superdan", "")
        principal = self.auth.session(token)

        def attempt(_):
            try:
                return self.service.revoke_all(principal, {}, "same")
            except SessionError:
                return None

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(len([result for result in results if result]), 1)
        with self.repo.engine.connect() as conn:
            self.assertEqual(
                read(conn, self.scope.tenant_id, "epoch", "current"), {"value": 1}
            )

    def test_private_sid_shape_unchanged_after_successful_cached_check(self):
        token = self.hosted()
        with self.repo.engine.connect() as conn:
            before = conn.execute(
                select(documents.c.payload).where(
                    *self.auth._workos_document("creator", digest(token))
                )
            ).scalar_one()
        self.auth.session(token)
        with self.repo.engine.connect() as conn:
            after = conn.execute(
                select(documents.c.payload).where(
                    *self.auth._workos_document("creator", digest(token))
                )
            ).scalar_one()
        self.assertEqual(before, after)

    def test_bound_transport_enqueued_in_same_transaction_and_failure_rolls_back(self):
        token = self.hosted()
        principal = self.auth.session(token)

        class BrokenTransport:
            def enqueue_account_operation(_self, *args):
                raise RuntimeError("offline enqueue failure")

        self.repo.generation_transport = BrokenTransport()
        with self.assertRaises(RuntimeError):
            self.service.revoke_all(principal, {}, "enqueue")
        self.repo.generation_transport = None
        self.assertIsNotNone(self.auth.session(token))
        with self.repo.engine.connect() as conn:
            self.assertIsNone(read(conn, self.scope.tenant_id, "epoch", "current"))

    def test_http_current_cookie_csrf_machine_and_other_browser(self):
        from fastapi.testclient import TestClient
        from studio_platform.api import COOKIE, create_app
        from studio_platform.settings import Settings

        app = create_app(
            Settings(
                Path(self.temp.name) / "session-http",
                database_url=self.url,
                tenant_id=self.scope.tenant_id,
                auth_mode="local-test",
                deployment_target="staging",
            ),
            repository=self.repo,
        )
        app.state.auth.clock = lambda: self.now
        one = app.state.auth.login("superdan", "")
        two = app.state.auth.login("superdan", "", source="second-browser")
        other = app.state.auth.login("supervan", "")
        with TestClient(app, base_url="https://testserver") as client:
            self.assertEqual(
                client.post(
                    "/api/auth/sessions/revoke-all",
                    json={},
                    headers={"Idempotency-Key": "once"},
                ).status_code,
                401,
            )
            client.cookies.set(COOKIE, one)
            self.assertEqual(
                client.post(
                    "/api/auth/sessions/revoke-all",
                    json={},
                    headers={"Idempotency-Key": "once", "Origin": "https://evil.test"},
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post("/api/auth/sessions/revoke-all", json={}).status_code, 422
            )
            result = client.post(
                "/api/auth/sessions/revoke-all",
                json={},
                headers={
                    "Idempotency-Key": "once",
                    "Origin": "https://testserver",
                },
            )
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["state"], "completed")
            self.assertIn("Max-Age=0", result.headers["set-cookie"])
            self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertIsNone(app.state.auth.session(two))
        self.assertIsNotNone(app.state.auth.session(other))

    def test_transport_keeps_existing_queue_and_original_operation_identity(self):
        from studio_platform.dbos_transport import GenerationTransport, QUEUE

        calls = []
        transport = object.__new__(GenerationTransport)
        transport.client = SimpleNamespace(
            enqueue_in_transaction=lambda *args: calls.append(args)
        )
        ident = str(uuid.uuid4())
        with (
            self.repo.transaction() as conn,
            patch("studio_platform.workflow_schema.assert_execution_allowed"),
        ):
            transport.enqueue_account_operation(conn, self.scope.tenant_id, ident)
        _, options, tenant, operation_id = calls[0]
        self.assertEqual(options["queue_name"], QUEUE)
        self.assertEqual(options["workflow_name"], "inkseq.account.lifecycle.v1")
        self.assertEqual((tenant, operation_id), (self.scope.tenant_id, ident))

    def late_binding(self, sid):
        return {
            "sid": sid,
            "subject": self.subject,
            "client_id": self.provider.client_id,
            "access_expires_at": int(self.now) + 900,
        }

    def test_verified_rejected_late_sid_reopens_same_completed_operation_once(self):
        token = self.hosted()
        captured = self.auth.capture_epoch()
        operation = self.operation(token)
        ident = operation["operation_id"]
        self.service.turn(self.scope.tenant_id, ident)
        self.service.turn(self.scope.tenant_id, ident)
        self.assertEqual(self.saved(ident)["state"], "completed")
        self.now += 1
        late = "session_" + "L" * 24
        fresh = "session_" + "F" * 24
        self.provider.add(late, self.subject, created=self.now)
        self.provider.add(fresh, self.subject, created=self.now)
        calls = []
        self.repo.generation_transport = SimpleNamespace(
            enqueue_account_operation=lambda *args, **kwargs: calls.append(
                (args[1:], kwargs)
            )
        )
        try:
            self.assertTrue(
                self.service.rejected_flow("creator", captured, self.late_binding(late))
            )
            self.assertTrue(
                self.service.rejected_flow("creator", captured, self.late_binding(late))
            )
        finally:
            self.repo.generation_transport = None
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], (self.scope.tenant_id, ident))
        self.assertEqual(self.saved(ident)["state"], "pending")
        self.service.turn(self.scope.tenant_id, ident)
        self.service.turn(self.scope.tenant_id, ident)
        self.assertEqual(self.saved(ident)["state"], "completed")
        self.assertEqual(self.provider.revokes.count(late), 1)
        self.assertIn(fresh, self.provider.active)

    def test_late_append_between_scan_and_observation_is_never_false_absence(self):
        token = self.hosted()
        captured = self.auth.capture_epoch()
        result = self.operation(token)
        ident = result["operation_id"]
        self.service.turn(self.scope.tenant_id, ident)
        original = self.service._save
        late = "session_" + "L" * 24
        appended = []

        def race(operation_id, change):
            if change.__name__ == "observed" and not appended:
                appended.append(True)
                self.now += 1
                self.provider.add(late, self.subject, created=self.now)
                self.assertTrue(
                    self.service.rejected_flow(
                        "creator", captured, self.late_binding(late)
                    )
                )
            return original(operation_id, change)

        with patch.object(self.service, "_save", side_effect=race):
            self.assertFalse(self.service.turn(self.scope.tenant_id, ident)["done"])
        self.assertEqual(self.saved(ident)["provider_sessions"][late], "unrequested")
        self.service.turn(self.scope.tenant_id, ident)
        self.service.turn(self.scope.tenant_id, ident)
        self.assertEqual(self.saved(ident)["state"], "completed")

    def test_unverified_or_foreign_late_binding_never_modifies_original_operation(self):
        token = self.hosted()
        captured = self.auth.capture_epoch()
        ident = self.operation(token)["operation_id"]
        original = self.saved(ident)
        binding = self.late_binding("session_" + "L" * 24)
        for owner, epoch, value in (
            ("supervan", captured, binding),
            ("creator", captured + 1, binding),
            ("creator", captured, {**binding, "subject": "user_" + "Z" * 24}),
            ("creator", captured, {**binding, "client_id": "client_" + "Z" * 24}),
            ("creator", captured, {**binding, "sid": "invalid"}),
        ):
            self.assertFalse(self.service.rejected_flow(owner, epoch, value))
        self.assertEqual(self.saved(ident), original)

    def test_transport_resume_cannot_lose_wake_as_original_becomes_terminal(self):
        from studio_platform.dbos_transport import GenerationTransport
        from studio_platform.repository import request_hash

        calls = []
        transport = object.__new__(GenerationTransport)
        transport.client = SimpleNamespace(
            enqueue_in_transaction=lambda *args: calls.append(args)
        )

        def no_racy_status_query(*args):
            self.fail("resume_must_not_select_then_send_to_original")

        connection = SimpleNamespace(execute=no_racy_status_query)
        ident = str(uuid.uuid4())
        event = request_hash({"verified_sid": "offline"})
        with patch("studio_platform.workflow_schema.assert_execution_allowed"):
            # Whether the old SDK workflow is PENDING or commits SUCCESS at
            # this boundary, a new deterministic epoch is durably enqueued.
            for _ in range(3):
                transport.enqueue_account_operation(
                    connection, self.scope.tenant_id, ident, resume_event=event
                )
        self.assertEqual(len({call[1]["workflow_id"] for call in calls}), 1)
        self.assertTrue(calls[0][1]["workflow_id"].endswith("/" + event))
        self.assertEqual([call[3] for call in calls], [ident] * 3)

    def test_concurrent_original_and_resume_only_revoke_exact_frozen_sids(self):
        token = self.hosted()
        ident = self.operation(token)["operation_id"]
        original_ids = set(self.saved(ident)["provider_sessions"])
        self.now += 1
        fresh = "session_" + "N" * 24
        self.provider.add(fresh, self.subject, created=self.now)
        barrier = threading.Barrier(2)
        revoke = self.provider.revoke_session

        def concurrent_revoke(*, session_id):
            barrier.wait(timeout=5)
            revoke(session_id=session_id)

        with patch.object(
            self.provider, "revoke_session", side_effect=concurrent_revoke
        ):
            with ThreadPoolExecutor(2) as pool:
                results = list(
                    pool.map(
                        lambda _: self.service.turn(self.scope.tenant_id, ident),
                        range(2),
                    )
                )
        self.assertTrue(all(not result["done"] for result in results))
        self.assertEqual(set(self.provider.revokes), original_ids)
        self.assertIn(fresh, self.provider.active)
        self.assertTrue(self.service.turn(self.scope.tenant_id, ident)["done"])


if __name__ == "__main__":
    unittest.main()
