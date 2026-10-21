"""Bot context remains bound to saved client material; fake HTTP/store only."""

import copy
from importlib.util import module_from_spec, spec_from_file_location
import json
import io
from pathlib import Path
import unittest
from urllib.error import HTTPError


spec = spec_from_file_location(
    "telegram_connect_helper_test",
    Path(__file__).resolve().parents[1]
    / "skills"
    / "inkseq"
    / "scripts"
    / "connect.py",
)
helper = module_from_spec(spec)
spec.loader.exec_module(helper)

ORIGIN = "https://staging.inkseq.com"
CONNECTION = "connection-" + "a" * 32
ACCOUNT = "account-" + "b" * 32
CODE = "sxc_" + "C" * 43
NONCE = "N" * 43
USER = {"id": "12345678", "username": "qa_telegram", "display_name": "QA Bot User"}


class MemoryStore:
    def __init__(self):
        self.state = None
        self.writes = 0
        self.fail = False

    def load(self, origin, connection_id):
        assert (origin, connection_id) == (ORIGIN, CONNECTION)
        return copy.deepcopy(self.state)

    def save(self, origin, connection_id, record, *, replace=False):
        assert (origin, connection_id) == (ORIGIN, CONNECTION)
        if self.fail:
            raise helper.HelperError("os_credential_storage_failed")
        if self.state is not None:
            record = helper.SecureStore._preserve_record(self.state, record)
        self.state = copy.deepcopy(record)
        self.writes += 1


class FakeExchange:
    def __init__(self, authorization, store):
        self.authorization, self.store = authorization, store
        self.calls = []
        self.lose_after_claim = False
        self.lose_before_claim = False
        self.claim = None
        self.claimed_user = None
        self.returned_user = None

    def request(self, path, *, method="GET", body=None):
        assert (path, method) == ("/v1/agent-connect/exchange", "POST")
        assert self.store.state is not None, "must save before first exchange"
        saved = self.store.state
        assert body["token_hash"] == helper.digest(saved["api_key"])
        assert saved["api_key"] not in json.dumps(body), "never send raw PAT"
        context = saved.get("telegram_context")
        if context:
            assert body["telegram_user"] == context["telegram_user"]
            assert body.get("telegram_nonce") == context.get("telegram_nonce")
        self.calls.append(copy.deepcopy(body))
        if self.lose_before_claim:
            self.lose_before_claim = False
            raise helper.HelperError("network_result_unknown")
        if "recovery_verifier" in body and self.claim is None:
            raise helper.HelperError("connection_not_exchanged")
        if self.claim is None:
            self.claim = copy.deepcopy(body)
            self.claimed_user = copy.deepcopy(body.get("telegram_user"))
        else:
            assert body["token_hash"] == self.claim["token_hash"]
            assert (
                helper.digest(body["recovery_verifier"])
                == self.claim["client_challenge"]
            )
            assert body.get("telegram_nonce") == self.claim.get("telegram_nonce")
            assert body.get("telegram_user") == self.claim.get("telegram_user")
        if self.lose_after_claim:
            self.lose_after_claim = False
            raise helper.HelperError("network_result_unknown")
        connection = {
            "id": CONNECTION,
            "status": "connected",
            "key_id": "key-test",
            "authorization": copy.deepcopy(self.authorization),
            "authorization_fingerprint": helper.digest(
                helper.canonical(self.authorization)
            ),
        }
        if self.authorization.get("client") == "telegram":
            connection.update(
                client="telegram",
                claimed_telegram_user={
                    **(self.returned_user or self.claimed_user),
                    "claimed_at": 1900000000,
                },
            )
        return {
            "connection": connection,
            "key": {
                "id": "key-test",
                "prefix": body["key_prefix"],
                **{
                    key: self.authorization[key]
                    for key in ("scopes", "project_ids", "all_projects")
                },
                "expires_at": self.authorization["key_expires_at"],
            },
            "recovered": "recovery_verifier" in body,
        }


class TelegramHelperTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.authorization = {
            "origin": ORIGIN,
            "connection_id": CONNECTION,
            "owner": ACCOUNT,
            "tenant": "sixnine",
            "profile_id": "creator-full",
            "profile_version": 1,
            "scopes": ["projects:read"],
            "project_ids": [],
            "all_projects": True,
            "key_expires_at": 1900000000,
            "client": "telegram",
            "telegram_nonce_hash": helper.digest(NONCE),
        }
        self.http = FakeExchange(self.authorization, self.store)
        self.client = helper.AgentClient(ORIGIN, store=self.store, http=self.http)

    def connect(self, **changes):
        args = {
            "code": CODE,
            "connection_id": CONNECTION,
            "owner": self.authorization["owner"],
            "tenant": "sixnine",
            "fingerprint": helper.digest(helper.canonical(self.authorization)),
            "telegram_user": copy.deepcopy(USER),
            "telegram_nonce": NONCE,
        }
        return self.client.connect(**{**args, **changes})

    def test_context_saved_before_exchange_and_never_in_public_result(self):
        result = self.connect()
        self.assertEqual(
            self.store.state["telegram_context"],
            {
                "telegram_user": USER,
                "telegram_nonce": NONCE,
            },
        )
        self.assertEqual(self.http.calls[0]["telegram_user"], USER)
        self.assertEqual(self.http.calls[0]["telegram_nonce"], NONCE)
        for secret in (
            CODE,
            NONCE,
            self.store.state["api_key"],
            self.store.state["verifier"],
        ):
            self.assertNotIn(secret, json.dumps(result))

    def test_storage_failure_prevents_exchange(self):
        self.store.fail = True
        with self.assertRaisesRegex(helper.HelperError, "os_credential_storage_failed"):
            self.connect()
        self.assertEqual(self.http.calls, [])

    def test_lost_response_resume_reuses_original_context_key_and_verifier(self):
        self.http.lose_after_claim = True
        with self.assertRaisesRegex(helper.HelperError, "network_result_unknown"):
            self.connect()
        original = copy.deepcopy(self.store.state)
        result = self.client.resume(CONNECTION)
        self.assertTrue(result["recovered"])
        for field in ("api_key", "verifier", "code", "telegram_context", "expected"):
            self.assertEqual(self.store.state[field], original[field])
        self.assertEqual(
            self.http.calls[1],
            {
                **self.http.calls[0],
                "recovery_verifier": original["verifier"],
            },
        )

    def test_lost_before_claim_retries_original_context_after_definite_unclaimed(self):
        self.http.lose_before_claim = True
        user = copy.deepcopy(USER)
        with self.assertRaises(helper.HelperError):
            self.connect(telegram_user=user)
        user["id"] = "999"
        user["display_name"] = "Later mutation"
        result = self.client.resume(CONNECTION)
        self.assertFalse(result["recovered"])
        self.assertEqual(len(self.http.calls), 3)
        self.assertEqual(self.http.calls[0], self.http.calls[2])
        self.assertEqual(self.store.state["telegram_context"]["telegram_user"], USER)

    def test_later_connect_cannot_change_user_nonce_or_original_metadata(self):
        self.http.lose_after_claim = True
        with self.assertRaises(helper.HelperError):
            self.connect()
        before = copy.deepcopy(self.store.state)
        for changes in (
            {"telegram_user": {**USER, "id": "999"}},
            {"telegram_nonce": "X" * 43},
            {"telegram_user": {**USER, "display_name": "Changed"}},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(
                    helper.HelperError, "saved_authorization_mismatch"
                ),
            ):
                self.connect(**changes)
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.store.state, before)
        # Omission resumes the stored context; it does not drop the binding.
        self.connect(telegram_user=None, telegram_nonce=None)
        self.assertEqual(self.http.calls[-1]["telegram_user"], USER)
        self.assertEqual(self.http.calls[-1]["telegram_nonce"], NONCE)

    def test_returned_claim_must_match_id_and_grant_nonce_hash(self):
        self.http.returned_user = {**USER, "id": "999"}
        with self.assertRaisesRegex(
            helper.HelperError, "exchange_authorization_mismatch"
        ):
            self.connect()
        self.assertEqual(self.store.state["status"], "unknown")
        self.store = MemoryStore()
        self.http.store = self.store
        self.client.store = self.store
        self.http.claim = None
        self.http.returned_user = None
        with self.assertRaisesRegex(
            helper.HelperError, "exchange_authorization_mismatch"
        ):
            self.connect(telegram_nonce="X" * 43)
        self.assertEqual(self.store.state["status"], "unknown")

    def test_returned_mutable_profile_metadata_does_not_override_original_context(self):
        self.http.returned_user = {
            **USER,
            "username": "new_name",
            "display_name": "New Name",
        }
        self.connect()
        self.assertEqual(self.store.state["telegram_context"]["telegram_user"], USER)

    def test_website_first_grant_has_claimed_user_but_no_nonce(self):
        del self.authorization["telegram_nonce_hash"]
        self.connect(telegram_nonce=None, telegram_user={"id": USER["id"]})
        self.assertEqual(
            self.http.calls[0]["telegram_user"],
            {
                "id": USER["id"],
                "username": None,
                "display_name": None,
            },
        )
        self.assertNotIn("telegram_nonce", self.http.calls[0])

    def test_ordinary_agent_state_and_http_body_remain_unchanged(self):
        self.authorization.pop("client")
        self.authorization.pop("telegram_nonce_hash")
        self.authorization["owner"] = "ordinary-owner"
        self.connect(telegram_user=None, telegram_nonce=None)
        self.assertNotIn("telegram_context", self.store.state)
        self.assertEqual(
            set(self.http.calls[0]),
            {
                "code",
                "client_challenge",
                "token_hash",
                "key_prefix",
                "expected_authorization_fingerprint",
            },
        )
        self.assertEqual(
            set(self.store.state),
            {
                "protocol",
                "status",
                "expected",
                "code",
                "api_key",
                "verifier",
                "key_id",
                "expires_at",
            },
        )

    def test_invalid_telegram_input_stops_before_any_store_write_or_http(self):
        invalid = [
            {"telegram_user": user}
            for user in (
                {},
                {"id": 123},
                {"id": True},
                {"id": "0"},
                {"id": "01"},
                {"id": "9223372036854775808"},
                {"id": "1", "first_name": "Extra"},
                {"id": "1", "username": "bad-name"},
                {"id": "1", "username": "x" * 33},
                {"id": "1", "display_name": "a\nb"},
                {"id": "1", "display_name": "x" * 161},
            )
        ] + [{"telegram_nonce": nonce} for nonce in ("short", "N" * 129, 123)]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(helper.HelperError):
                self.connect(**changes)
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.http.calls, [])

    def test_secure_store_rejects_context_override(self):
        self.connect()
        original = copy.deepcopy(self.store.state)
        tampered = copy.deepcopy(original)
        tampered["telegram_context"]["telegram_nonce"] = "X" * 43
        with self.assertRaisesRegex(helper.HelperError, "saved_authorization_mismatch"):
            helper.SecureStore._preserve_record(original, tampered)

    def test_corrupt_saved_context_is_not_replayed(self):
        self.http.lose_before_claim = True
        with self.assertRaises(helper.HelperError):
            self.connect()
        self.store.state["telegram_context"]["telegram_nonce"] = "short"
        with self.assertRaisesRegex(
            helper.HelperError, "connection_telegram_nonce_invalid"
        ):
            self.client.resume(CONNECTION)
        self.assertEqual(len(self.http.calls), 1)

    def test_http_error_only_exposes_closed_telegram_error_codes(self):
        class Refuse:
            def __init__(self, code):
                self.code = code

            def open(self, request, timeout):
                raise HTTPError(
                    request.full_url,
                    409,
                    "server details must stay private",
                    {},
                    io.BytesIO(
                        json.dumps({"code": self.code, "secret": NONCE}).encode()
                    ),
                )

        http = helper.JsonHTTP(ORIGIN)
        for code in (
            "connection_client_invalid",
            "connection_telegram_nonce_invalid",
            "connection_telegram_nonce_mismatch",
            "connection_telegram_user_invalid",
            "connection_telegram_user_mismatch",
            "untrusted_" + NONCE,
        ):
            http.opener = Refuse(code)
            with (
                self.subTest(code=code),
                self.assertRaises(helper.HelperError) as caught,
            ):
                http.request("/v1/agent-connect/exchange", method="POST", body={})
            self.assertEqual(
                str(caught.exception),
                code if not code.startswith("untrusted_") else "http_request_failed",
            )


if __name__ == "__main__":
    unittest.main()
