"""Telegram bot account linking (#382): parsing, exchange, routing, hygiene.

The real connect.py helper runs against a fake website (MockTransport-like
fake HTTP) and an in-memory secure store; no network or OS credential store.
"""

import json
from pathlib import Path
import tempfile
import unittest

from telegram_bot.accounts import AccountRouter
from telegram_bot.bot import Bot, Store
from telegram_bot.demo_adapter import DemoAdapter
from telegram_bot.feedback import FeedbackLog
from telegram_bot.linking import Linker, load_helper, parse_instruction
from tests.test_telegram_bot import OWNER, FakeAPI, private

STAGING = "https://staging.inkseq.com"
CODE = "sxc_" + "A" * 43
CONNECTION = "connection-" + "a" * 32


def grant():
    helper = load_helper()
    authorization = {"origin": STAGING, "connection_id": CONNECTION, "owner": "qa-user",
                     "tenant": "tenant-1", "profile_id": "creator-full", "profile_version": 1,
                     "scopes": ["projects:read"], "project_ids": [], "all_projects": True,
                     "key_expires_at": 1900000000}  # fmt: skip
    return helper, authorization, helper.digest(helper.canonical(authorization))


def instruction(authorization, fingerprint):
    metadata = {"connection_id": CONNECTION, "authorization_fingerprint": fingerprint,
                "authorization": authorization}  # fmt: skip
    return f"Code: {CODE}\n{json.dumps(metadata, indent=2)}"


class MemoryStore:
    def __init__(self):
        self.saved = {}

    def save(self, origin, connection_id, record, *, replace=False):
        self.saved[(origin, connection_id)] = json.loads(json.dumps(record))

    def load(self, origin, connection_id):
        return self.saved.get((origin, connection_id))


class FakeHTTP:
    """Plays /v1/agent-connect/exchange like the server, checking the body."""

    def __init__(self, authorization, fingerprint, fail=None):
        self.authorization, self.fingerprint, self.fail = (
            authorization,
            fingerprint,
            fail,
        )
        self.bodies = []

    def __call__(self, origin):
        return self

    def request(
        self, path, *, method="GET", body=None, token=None, idempotency_key=None
    ):
        assert path == "/v1/agent-connect/exchange" and method == "POST"
        self.bodies.append(body)
        if self.fail:
            raise self.error(self.fail)
        return {"connection": {"id": CONNECTION, "authorization": self.authorization,
                               "authorization_fingerprint": self.fingerprint,
                               "key_id": "key-1", "status": "connected"},
                "key": {"id": "key-1", "prefix": body["key_prefix"], "scopes": ["projects:read"],
                        "project_ids": [], "all_projects": True, "expires_at": 1900000000}}  # fmt: skip


class LinkingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.helper, self.authorization, self.fingerprint = grant()
        self.store = MemoryStore()
        self.http = FakeHTTP(self.authorization, self.fingerprint)
        self.http.error = self.helper.HelperError

        class Helper:  # the real helper, with fake HTTP injected
            HelperError = self.helper.HelperError
            SecureStore = MemoryStore

            @staticmethod
            def AgentClient(origin, store=None):
                return self.helper.AgentClient(origin, store=store, http=self.http)

        self.linker = Linker(
            self.root / "accounts", [STAGING], helper=Helper, store=self.store
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_instruction_needs_everything(self):
        fields = parse_instruction(instruction(self.authorization, self.fingerprint))
        self.assertEqual(
            (fields["code"], fields["owner"], fields["origin"]),
            (CODE, "qa-user", STAGING),
        )
        self.assertIsNone(parse_instruction(f"just a code {CODE}"))
        self.assertIsNone(parse_instruction("no code at all"))

    def test_exchange_saves_before_and_never_sends_the_key(self):
        link = self.linker.link(
            OWNER, parse_instruction(instruction(self.authorization, self.fingerprint))
        )
        self.assertEqual(link["owner"], "qa-user")
        body = self.http.bodies[0]
        token = self.linker.token(OWNER)
        self.assertTrue(token.startswith("sxp_"))
        self.assertNotIn(
            token, json.dumps(body)
        )  # only the digest and a 12-char prefix
        self.assertEqual(body["token_hash"], self.helper.digest(token))
        self.assertNotIn(token, (self.root / "accounts" / "links.json").read_text())

    def test_bot_flow_deletes_code_message_and_routes_per_account(self):
        api = FakeAPI(self.root)
        deleted = []
        api.delete_message = lambda chat, message: deleted.append(message)
        clients = []
        router = AccountRouter(self.linker, DemoAdapter(self.root / "demo"), gemini=None,
                               workdir=self.root / "work",
                               client_factory=lambda origin, token: clients.append(token) or object())  # fmt: skip
        log = FeedbackLog(self.root / "fb.jsonl")
        bot = Bot(api, router, Store(self.root / "state"), [OWNER], self.root / "media",
                  feedback=log, linker=self.linker)  # fmt: skip
        bot.handle(private(OWNER, text="/status"))
        self.assertIn("未绑定", api.sent[-1]["text"])
        update = private(OWNER, text=instruction(self.authorization, self.fingerprint))
        update["message"]["message_id"] = 42
        bot.handle(update)
        self.assertEqual(deleted, [42])
        self.assertIn("绑定好了：qa-user", api.sent[-1]["text"])
        state = bot.store.load(OWNER)
        self.assertEqual(state["brief"], "")  # the code never became a request
        self.assertNotIn(
            CODE,
            (self.root / "fb.jsonl").read_text()
            if (self.root / "fb.jsonl").exists()
            else "",
        )
        bot.handle(private(OWNER, text="/status"))
        self.assertIn("已绑定 qa-user", api.sent[-1]["text"])
        self.assertEqual(len(clients), 1)  # the linked adapter used this owner's key
        bot.handle(private(OWNER, text="/unlink"))
        self.assertIn("已解除绑定", api.sent[-1]["text"])
        self.assertIs(router.for_chat(OWNER), router.default)

    def test_expired_code_is_explained(self):
        self.http.fail = "connection_expired"
        api = FakeAPI(self.root)
        api.delete_message = lambda chat, message: None
        bot = Bot(api, DemoAdapter(self.root / "demo"), Store(self.root / "state"), [OWNER],
                  self.root / "media", linker=self.linker)  # fmt: skip
        bot.handle(
            private(OWNER, text=instruction(self.authorization, self.fingerprint))
        )
        self.assertIn("过期", api.sent[-1]["text"])
        self.assertIsNone(self.linker.get(OWNER))

    def test_other_origins_are_refused(self):
        bad = dict(self.authorization, origin="https://evil.example")
        fields = parse_instruction(instruction(bad, self.fingerprint))
        from telegram_bot.linking import LinkError

        with self.assertRaises(LinkError):
            self.linker.link(OWNER, fields)


if __name__ == "__main__":
    unittest.main()
