"""Client-held PAT and recoverable local state; all API calls stay in process."""
from importlib.util import module_from_spec, spec_from_file_location
import io
import copy
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from studio_platform.agent_connect import AgentConnect, ConnectError, PROFILE_ID, PROFILE_VERSION
from studio_platform.auth import Auth, digest
from studio_platform.repository import Repository

HELPER_PATH = Path(__file__).resolve().parent / "skills" / "sixnine-yingxu" / "scripts" / "connect.py"
spec = spec_from_file_location("sixnine_connect_test", HELPER_PATH)
helper = module_from_spec(spec)
spec.loader.exec_module(helper)


class MemoryStore:
    def __init__(self):
        self.values, self.writes, self.fail = {}, 0, False
    def load(self, origin, connection_id):
        result = self.values.get((origin, connection_id))
        return json.loads(json.dumps(result)) if result else None
    def save(self, origin, connection_id, record, *, replace=False):
        if self.fail:
            raise helper.HelperError("os_credential_storage_failed")
        if not replace and (origin, connection_id) in self.values:
            raise helper.HelperError("connection_already_saved")
        self.values[(origin, connection_id)] = json.loads(json.dumps(record))
        self.writes += 1


class InProcessHTTP:
    def __init__(self, service, origin, store):
        self.service, self.origin, self.store = service, origin, store
        self.calls, self.lose_response, self.lose_before_claim, self.tamper = [], False, False, False
    def request(self, path, *, method="GET", body=None, token=None, idempotency_key=None):
        self.calls.append({"path": path, "method": method, "token_in_header": bool(token), "idempotency_key": idempotency_key})
        if path == "/v1/agent-connect/exchange":
            if not self.store.writes:
                raise AssertionError("token must be saved before exchange")
            self.assert_no_raw_pat(body)
            if self.lose_before_claim:
                self.lose_before_claim = False
                raise helper.HelperError("network_result_unknown")
            try:
                result = self.service.exchange(**body, origin=self.origin, source="in-process")
            except ConnectError as error:
                raise helper.HelperError(error.code) from None
            if self.lose_response:
                self.lose_response = False
                raise helper.HelperError("network_result_unknown")
            if self.tamper:
                result["connection"]["authorization"]["owner"] = "supervan"
            return result
        principal = self.service.auth.bearer(token)
        if not principal:
            raise helper.HelperError("http_request_failed")
        return {"username": principal.owner, "machine": principal.machine, "actor_id": principal.actor_id,
            "scopes": list(principal.scopes), "all_projects": principal.all_projects, "project_ids": list(principal.project_ids)}
    @staticmethod
    def assert_no_raw_pat(body):
        if "api_key" in body:
            raise AssertionError("raw PAT reached exchange")


class AgentConnectHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository("sqlite:///" + (Path(self.temp.name) / "test.sqlite").as_posix())
        self.addCleanup(self.repo.close)
        self.auth = Auth(self.repo.engine, mode="local-test")
        session = self.auth.login("superdan", "", source="helper")
        self.service = AgentConnect(self.auth)
        self.origin = "http://127.0.0.1:8850"
        self.grant = self.service.issue(self.auth.session(session), authorization_profile_id=PROFILE_ID,
            authorization_profile_version=PROFILE_VERSION, idempotency_key="helper-create", origin=self.origin)
        self.connection_id = self.grant["connection"]["id"]
        self.store = MemoryStore()
        self.http = InProcessHTTP(self.service, self.origin, self.store)
        self.client = helper.AgentClient(self.origin, store=self.store, http=self.http)

    def connect(self):
        return self.client.connect(code=self.grant["code"], connection_id=self.connection_id,
            owner="superdan", tenant="sixnine", fingerprint=self.grant["connection"]["authorization_fingerprint"])

    def test_local_token_saved_first_and_output_contains_no_credentials(self):
        result = self.connect()
        self.assertEqual(result["status"], "connected")
        record = self.store.load(self.origin, self.connection_id)
        public_output = json.dumps(result)
        for value in (record["api_key"], record["verifier"], record["code"]):
            self.assertTrue(value not in public_output, "credential leaked in public result")
        self.assertEqual(self.client.request(self.connection_id, "/api/auth/me")["username"], "superdan")

    def test_storage_failure_prevents_any_registration(self):
        self.store.fail = True
        with self.assertRaisesRegex(helper.HelperError, "os_credential_storage_failed"):
            self.connect()
        self.assertEqual(len(self.http.calls), 0)

    def test_lost_response_recovers_same_key_and_saved_pat(self):
        self.http.lose_response = True
        with self.assertRaisesRegex(helper.HelperError, "network_result_unknown"):
            self.connect()
        before = self.store.load(self.origin, self.connection_id)
        recovered = self.client.resume(self.connection_id)
        after = self.store.load(self.origin, self.connection_id)
        self.assertTrue(before["api_key"] == after["api_key"], "recovery rotated key")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(len(self.http.calls), 2)

    def test_request_lost_before_server_recovers_then_exact_original_claim(self):
        self.http.lose_before_claim = True
        with self.assertRaises(helper.HelperError):
            self.connect()
        before = self.store.load(self.origin, self.connection_id)
        result = self.client.resume(self.connection_id)
        self.assertEqual(result["status"], "connected")
        self.assertTrue(before["api_key"] == self.store.load(self.origin, self.connection_id)["api_key"], "retry rotated key")
        self.assertEqual(len(self.http.calls), 3)

    def test_connected_local_resume_does_not_claim_online_verified(self):
        self.connect()
        calls = len(self.http.calls)
        result = self.client.resume(self.connection_id)
        self.assertEqual(result["status"], "connected_locally")
        self.assertFalse(result["online_verified"])
        self.assertEqual(len(self.http.calls), calls)

    def test_wrong_account_return_fails_closed(self):
        self.http.tamper = True
        with self.assertRaisesRegex(helper.HelperError, "exchange_authorization_mismatch"):
            self.connect()
        self.assertEqual(self.store.load(self.origin, self.connection_id)["status"], "unknown")
        with self.assertRaisesRegex(helper.HelperError, "saved_connection_not_active"):
            self.client.request(self.connection_id, "/v1/projects")

    def test_original_saved_authorization_cannot_be_changed(self):
        self.connect()
        with self.assertRaisesRegex(helper.HelperError, "saved_authorization_mismatch"):
            self.client.connect(code=self.grant["code"], connection_id=self.connection_id,
                owner="supervan", tenant="sixnine", fingerprint=self.grant["connection"]["authorization_fingerprint"])

    def test_request_uses_process_key_and_preserves_business_idempotency(self):
        self.connect()
        result = self.client.request(self.connection_id, "/v1/quick-chat/sessions", method="POST",
            body={"title": "A story"}, idempotency_key="my-create-one")
        self.assertEqual(result["username"], "superdan")
        self.assertTrue(self.http.calls[-1]["token_in_header"])
        self.assertEqual(self.http.calls[-1]["idempotency_key"], "my-create-one")

    def test_insecure_authenticated_origin_and_redirect_rejected(self):
        for url in ("http://remote.example", "https://user:pass@example.com", "https://example.com/?token=x",
                    "https://example.com/#code", "https://example.com/path"):
            with self.assertRaises(helper.HelperError):
                helper.checked_origin(url)
        with self.assertRaisesRegex(helper.HelperError, "redirect_rejected"):
            helper._NoRedirect().redirect_request(None)

    def test_linux_without_session_safe_storage_stops(self):
        with patch.object(helper.os, "name", "posix"), patch.object(helper.sys, "platform", "linux"), \
                patch.object(helper.shutil, "which", return_value=None):
            with self.assertRaisesRegex(helper.HelperError, "os_credential_storage_unavailable"):
                helper.SecureStore()

    def legacy_connection(self, origin="https://www.sixnine.art"):
        self.origin = origin
        self.grant = self.service.issue(self.auth.session(self.auth.login("superdan", "", source="migration")),
            authorization_profile_id=PROFILE_ID, authorization_profile_version=PROFILE_VERSION,
            idempotency_key="migration-create", origin=self.origin)
        self.connection_id = self.grant["connection"]["id"]
        self.http = InProcessHTTP(self.service, self.origin, self.store)
        self.target_http = InProcessHTTP(self.service, helper.CANONICAL_ORIGIN, self.store)
        def target(origin):
            self.assertEqual(origin, helper.CANONICAL_ORIGIN)
            return self.target_http
        self.client = helper.AgentClient(self.origin, store=self.store, http=self.http, http_factory=target)
        self.connect()
        return copy.deepcopy(self.store.values[(self.origin, self.connection_id)])

    def test_explicit_origin_migration_checks_original_authority_and_only_adds_a_secret_free_alias(self):
        original = self.legacy_connection()
        before = self.store.writes
        result = self.client.migrate_origin(self.connection_id, helper.CANONICAL_ORIGIN)
        self.assertEqual(self.store.writes, before + 1)

        self.assertEqual(self.store.values[(self.origin, self.connection_id)], original)
        alias = self.store.load(helper.CANONICAL_ORIGIN, self.connection_id)
        self.assertEqual(alias["protocol"], helper.ROUTE_PROTOCOL)
        for secret in (original["api_key"], original["verifier"], original["code"]):
            self.assertNotIn(secret, json.dumps(alias))
            self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(alias["source_reference"], helper.SecureStore.reference(self.origin, self.connection_id))
        self.assertEqual(result["expires_at"], original["expires_at"])
        self.assertEqual([(call["method"], call["path"]) for call in self.target_http.calls], [("GET", "/api/auth/me")])
        with patch.object(self.target_http, "request", wraps=self.target_http.request) as request:
            self.client.request(self.connection_id, "/v1/projects", method="POST", body={"title": "kept"}, idempotency_key="same-write")
            self.assertEqual(request.call_args.kwargs["body"], {"title": "kept"})
            self.assertEqual(request.call_args.kwargs["method"], "POST")
        self.assertEqual(self.target_http.calls[-1]["idempotency_key"], "same-write")
        new_client = helper.AgentClient(helper.CANONICAL_ORIGIN, store=self.store, http=self.target_http)
        self.assertEqual(new_client.request(self.connection_id, "/api/auth/me")["actor_id"], "key:" + original["key_id"])
        self.assertEqual(self.client.resume(self.connection_id)["origin"], helper.CANONICAL_ORIGIN)
        self.assertEqual(new_client.resume(self.connection_id)["origin"], helper.CANONICAL_ORIGIN)
        self.assertEqual(self.client.migrate_origin(self.connection_id, helper.CANONICAL_ORIGIN)["status"], "origin_migrated")
        self.assertEqual(self.store.writes, before + 1)

    def test_exact_legacy_apex_can_migrate_without_accepting_the_other_legacy_origin(self):
        original = self.legacy_connection("https://sixnine.art")
        self.client.migrate_origin(self.connection_id, helper.CANONICAL_ORIGIN)
        self.assertEqual(helper.resolve_connection(self.store, self.origin, self.connection_id), (helper.CANONICAL_ORIGIN, original))
        with self.assertRaisesRegex(helper.HelperError, "origin_migration_route_mismatch"):
            helper.resolve_connection(self.store, "https://www.sixnine.art", self.connection_id)

    def test_origin_migration_rejects_changed_remote_authority_and_redirects_before_saving(self):
        original = self.legacy_connection()
        identity = self.target_http.request("/api/auth/me", token=original["api_key"])
        for patch_value in ({"username": "supervan"}, {"machine": False}, {"actor_id": "key:other"},
                {"scopes": identity["scopes"] + ["new:scope"]}, {"scopes": identity["scopes"][:-1]},
                {"all_projects": False}, {"project_ids": ["other"]}):
            with self.subTest(patch_value=patch_value), patch.object(self.target_http, "request", return_value={**identity, **patch_value}):
                with self.assertRaisesRegex(helper.HelperError, "origin_migration_identity_mismatch"):
                    self.client.migrate_origin(self.connection_id, helper.CANONICAL_ORIGIN)
                self.assertIsNone(self.store.load(helper.CANONICAL_ORIGIN, self.connection_id))
        with patch.object(self.target_http, "request", side_effect=helper.HelperError("redirect_rejected")):
            with self.assertRaisesRegex(helper.HelperError, "redirect_rejected"):
                self.client.migrate_origin(self.connection_id, helper.CANONICAL_ORIGIN)
        self.assertEqual(self.store.values[(self.origin, self.connection_id)], original)

        self.assertIsNone(self.store.load(helper.CANONICAL_ORIGIN, self.connection_id))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
    def test_origin_alias_uses_existing_encrypted_store_without_rewriting_source_bytes(self):
        original = self.legacy_connection()
        with patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name}):
            store = helper.SecureStore()
            store.save(self.origin, self.connection_id, original)
            source_path = store.directory / (store.reference(self.origin, self.connection_id) + ".dpapi")
            before = source_path.read_bytes()
            client = helper.AgentClient(self.origin, store=store, http=self.http, http_factory=lambda _: self.target_http)
            client.migrate_origin(self.connection_id, helper.CANONICAL_ORIGIN)
            self.assertEqual(source_path.read_bytes(), before)
            alias_path = store.directory / (store.reference(helper.CANONICAL_ORIGIN, self.connection_id) + ".dpapi")
            self.assertNotIn(original["api_key"].encode(), alias_path.read_bytes())
            self.assertNotIn(b"source_origin", alias_path.read_bytes())
            self.assertNotIn("api_key", store.load(helper.CANONICAL_ORIGIN, self.connection_id))
            self.assertEqual(helper.resolve_connection(store, helper.CANONICAL_ORIGIN, self.connection_id), (helper.CANONICAL_ORIGIN, original))

    def test_origin_migration_rejects_pending_expired_unknown_or_tampered_original_grants_without_http(self):
        original = self.legacy_connection()
        for change in ({"status": "pending"}, {"status": "unknown"}, {"status": "revoked"},
                {"expires_at": time.time()-1}, {"expires_at": float("nan")}, {"expires_at": float("inf")},
                {"expires_at": True}, {"expires_at": original["expires_at"]+1},
                {"expected": {**original["expected"], "profile_version": 2}},
                {"expected": {**original["expected"], "owner": "supervan"}},
                {"expected": {**original["expected"], "fingerprint": "0"*64}}):
            with self.subTest(change=change):
                self.store.values[(self.origin, self.connection_id)] = {**original, **change}
                with self.assertRaises(helper.HelperError):
                    self.client.migrate_origin(self.connection_id, helper.CANONICAL_ORIGIN)
        self.assertEqual(self.target_http.calls, [])
        self.assertIsNone(self.store.load(helper.CANONICAL_ORIGIN, self.connection_id))

    def test_routing_alias_has_no_fallback_chain_overwrite_or_arbitrary_origin(self):
        original = self.legacy_connection()
        for target in ("http://inkseq.com", "https://inkseq.com:443", "https://staging.inkseq.com", "https://inkseq.com.evil.test", self.origin):
            with self.subTest(target=target), self.assertRaises(helper.HelperError):
                self.client.migrate_origin(self.connection_id, target)
        self.assertEqual(self.target_http.calls, [])
        self.store.values[(helper.CANONICAL_ORIGIN, self.connection_id)] = copy.deepcopy(original)
        with self.assertRaisesRegex(helper.HelperError, "origin_migration_target_conflict"):
            self.client.migrate_origin(self.connection_id, helper.CANONICAL_ORIGIN)
        with self.assertRaisesRegex(helper.HelperError, "saved_authorization_mismatch"):
            helper.resolve_connection(self.store, helper.CANONICAL_ORIGIN, self.connection_id)
        del self.store.values[(helper.CANONICAL_ORIGIN, self.connection_id)]
        self.client.migrate_origin(self.connection_id, helper.CANONICAL_ORIGIN)
        alias = self.store.load(helper.CANONICAL_ORIGIN, self.connection_id)
        with self.assertRaisesRegex(helper.HelperError, "origin_migration_route_mismatch"):
            helper.resolve_connection(self.store, "https://sixnine.art", self.connection_id)
        for change in ({"source_origin": helper.CANONICAL_ORIGIN}, {"source_binding": "0"*64},
                {"expires_at": alias["expires_at"]+1}, {"api_key": original["api_key"]}):
            with self.subTest(change=change):
                self.store.values[(helper.CANONICAL_ORIGIN, self.connection_id)] = {**alias, **change}
                with self.assertRaises(helper.HelperError):
                    self.client.request(self.connection_id, "/v1/projects", method="POST")
        self.store.values[(helper.CANONICAL_ORIGIN, self.connection_id)] = alias
        for change in ({"key_id": "other"}, {"api_key": "sxp_"+"z"*43}):
            self.store.values[(self.origin, self.connection_id)] = {**original, **change}
            with self.assertRaisesRegex(helper.HelperError, "origin_migration_binding_mismatch"):
                helper.resolve_connection(self.store, helper.CANONICAL_ORIGIN, self.connection_id)

    def test_source_change_during_target_check_prevents_alias_creation(self):
        original = self.legacy_connection()
        identity = self.target_http.request("/api/auth/me", token=original["api_key"])
        def changed(*args, **kwargs):
            self.store.values[(self.origin, self.connection_id)] = {**original, "key_id": "changed"}
            return identity
        with patch.object(self.target_http, "request", side_effect=changed), self.assertRaisesRegex(helper.HelperError, "origin_migration_source_changed"):
            self.client.migrate_origin(self.connection_id, helper.CANONICAL_ORIGIN)
        self.assertIsNone(self.store.load(helper.CANONICAL_ORIGIN, self.connection_id))

    def test_source_remains_exact_origin_bound_until_explicit_migration_and_racing_target_is_not_overwritten(self):
        original = self.legacy_connection()
        effective, saved = helper.resolve_connection(self.store, self.origin, self.connection_id)
        self.assertEqual(effective, self.origin)
        self.assertEqual(saved, original)
        self.assertEqual(helper.resolve_connection(self.store, helper.CANONICAL_ORIGIN, self.connection_id), (helper.CANONICAL_ORIGIN, None))
        with patch.object(self.target_http, "request", wraps=self.target_http.request) as request:
            def racing_save(origin, connection_id, record, *, replace=False):
                self.store.values[(origin, connection_id)] = {"protocol": "foreign"}
                raise helper.HelperError("connection_already_saved")
            with patch.object(self.store, "save", side_effect=racing_save), self.assertRaisesRegex(helper.HelperError, "connection_already_saved"):
                self.client.migrate_origin(self.connection_id, helper.CANONICAL_ORIGIN)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.store.load(helper.CANONICAL_ORIGIN, self.connection_id), {"protocol": "foreign"})
        self.assertEqual(self.store.values[(self.origin, self.connection_id)], original)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
    def test_windows_dpapi_temp_file_is_encrypted_and_roundtrips(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name}):
            store = helper.SecureStore()
            fake = {"api_key": "fake-unit-test-not-a-real-key", "verifier": "fake-test-data"}
            store.save(self.origin, self.connection_id, fake)
            path = store.directory / (store.reference(self.origin, self.connection_id) + ".dpapi")
            self.assertTrue(b"fake-unit-test-not-a-real-key" not in path.read_bytes(), "plaintext credential file")
            self.assertTrue(store.load(self.origin, self.connection_id) == fake, "DPAPI mismatch")
            with self.assertRaisesRegex(helper.HelperError, "connection_already_saved"):
                store.save(self.origin, self.connection_id, fake)

    @unittest.skipUnless(os.name == "nt", "Windows cross-process DPAPI storage only")
    def test_windows_concurrent_resume_cannot_regress_connected_record(self):
        # Pause the stale writer after reading pending. The connected writer
        # must wait on the same OS lock, then reread and publish connected.
        with patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name}):
            store = helper.SecureStore()
            store.save(self.origin, self.connection_id, {"api_key": "synthetic-test-key", "status": "pending"})
            code = r'''
import importlib.util, os, sys, time
from pathlib import Path
spec=importlib.util.spec_from_file_location("connect_race",sys.argv[1]); m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
root=Path(sys.argv[2]); role=sys.argv[5];store=m.SecureStore()
if role=="unknown":
    original=store._preserve_record
    def pause(existing,incoming):
        (root/"stale-read").touch()
        deadline=time.monotonic()+10
        while not (root/"release-stale").exists():
            if time.monotonic()>deadline: raise RuntimeError("fixture_deadline")
            time.sleep(.02)
        return original(existing,incoming)
    store._preserve_record=pause
else:
    (root/"connected-started").touch()
store.save(sys.argv[3],sys.argv[4],{"api_key":"synthetic-test-key","status":role},replace=True)
(root/(role+"-done")).touch()
'''
            root = Path(self.temp.name)
            def wait_for(name):
                deadline = time.monotonic() + 8
                while not (root/name).exists():
                    self.assertLess(time.monotonic(), deadline, "child fixture did not reach expected barrier")
                    time.sleep(.02)
            stale = subprocess.Popen([sys.executable, "-c", code, str(HELPER_PATH), str(root), self.origin, self.connection_id, "unknown"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            connected = None
            try:
                wait_for("stale-read")
                connected = subprocess.Popen([sys.executable, "-c", code, str(HELPER_PATH), str(root), self.origin, self.connection_id, "connected"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                wait_for("connected-started")
                time.sleep(.2)
                self.assertFalse((root/"connected-done").exists(), "connected writer passed the stale writer's lock")
                (root/"release-stale").touch()
                self.assertEqual(stale.wait(timeout=10), 0)
                self.assertEqual(connected.wait(timeout=10), 0)
                self.assertEqual(store.load(self.origin, self.connection_id)["status"], "connected")
                # A genuinely late unknown response also cannot downgrade it.
                store.save(self.origin, self.connection_id, {"api_key": "synthetic-test-key", "status": "unknown"}, replace=True)
                self.assertEqual(store.load(self.origin, self.connection_id)["status"], "connected")
            finally:
                (root/"release-stale").touch()
                for child in (stale, connected):
                    if child is not None and child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
