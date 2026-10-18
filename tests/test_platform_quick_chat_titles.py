"""One-shot optional history names: fake supplier, isolated database, no media."""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient
from studio_platform.api import create_app
from studio_platform.auth import Principal
from studio_platform.quick_chat import DEFAULT_SETTINGS, EMPTY_INPUTS, QuickChatService
from studio_platform.quick_chat_titles import TITLE_INPUT_CHARS, clean_title, title_error_code
from studio_platform.settings import Settings
from test_platform_repository import LedgerCase


class FakeTitleGenerator:
    def __init__(self, value="夜林水刃对决", error=None):
        self.value, self.error, self.calls = value, error, []
        self.entered, self.release = threading.Event(), None

    def generate(self, text):
        self.calls.append(text)
        self.entered.set()
        if self.release is not None:
            if not self.release.wait(5):
                raise TimeoutError()
        if self.error:
            raise self.error
        return self.value


class QuickChatTitleTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(Settings(Path(self.tmp.name), auth_mode="local-test", execution_backend="mock",
                                      database_url=self.url), repository=self.repo)
        self.service = self.app.state.quick_chat
        self.repo = self.app.state.repository
        self.generator = FakeTitleGenerator()
        self.service.title_generator = self.generator
        self.principal = Principal("superdan", "browser")
        self.agent = Principal("superdan", "agent-key", machine=True, all_projects=True,
            scopes=("projects:create", "projects:read", "projects:write", "assets:read", "assets:write"))
        self.session = self.service.create_session(self.principal, {}, "new-session")["session"]
        self.sid = self.session["id"]

    def tearDown(self):
        super().tearDown()
        self.tmp.cleanup()

    def current(self):
        return self.service.get_session(self.principal, self.sid)["session"]

    def turn_body(self, text="夜晚竹林中剑士交锋，水刃缠绕火焰"):
        return {"expected_version": self.current()["version"], "text": text,
            "model_id": "gemini-3.8-flash", "assistant_mode": "none", "create_card": True}

    def test_first_turn_claim_and_metadata_do_not_change_authoring_version_or_card(self):
        body = self.turn_body("夜林决战" + "追逐镜头" * 1000)
        turn = self.service.create_turn(self.agent, self.sid, body, "first-turn")
        revision_before = self.service.get_card(self.principal, self.sid, turn["card_id"])
        version = self.current()["version"]
        self.assertEqual(self.generator.calls, [])
        self.assertEqual(self.current()["title_generation"]["status"], "pending")
        self.assertTrue(self.service.generate_title(self.agent, self.sid))
        current = self.current()
        self.assertEqual(current["title"], "夜林水刃对决")
        self.assertEqual(current["version"], version)
        self.assertEqual(current["title_generation"], {"status": "completed", "model_id": "gemma-4-31b-it", "error_code": None})
        self.assertEqual(self.generator.calls, [body["text"][:TITLE_INPUT_CHARS]])
        self.assertEqual(self.service.get_card(self.principal, self.sid, turn["card_id"]), revision_before)
        self.service.create_turn(self.agent, self.sid, body, "first-turn")
        self.assertFalse(self.service.generate_title(self.agent, self.sid))
        self.service.create_turn(self.principal, self.sid, self.turn_body("同一场景镜头更近"), "second-turn")
        self.assertFalse(self.service.generate_title(self.principal, self.sid))
        self.assertEqual(len(self.generator.calls), 1)
        self.assertNotIn("source_id", current["title_generation"])
        self.assertNotIn("original_title", current["title_generation"])

    def test_agent_direct_card_and_route_background_share_title_authority(self):
        body = {**copy.deepcopy(DEFAULT_SETTINGS), "prompt": "雨夜火车上的追击", "inputs": EMPTY_INPUTS}
        one = self.service.save_card(self.agent, self.sid, body, "direct-card")
        self.service.save_card(self.agent, self.sid, body, "direct-card")
        self.assertTrue(self.service.generate_title(self.agent, self.sid))
        self.assertEqual(self.generator.calls, [body["prompt"]])
        self.assertEqual(self.service.get_revision(self.agent, self.sid, one["revision"]["id"])["prompt"], body["prompt"])
        with TestClient(self.app) as client:
            client.post("/api/auth/login", json={"username": "superdan"}).raise_for_status()
            created = client.post("/v1/quick-chat/sessions", json={}, headers={"Idempotency-Key": "route-session"})
            sid = created.json()["session"]["id"]
            response = client.post(f"/v1/quick-chat/sessions/{sid}/cards", json=body, headers={"Idempotency-Key": "route-card"})
            self.assertEqual(response.status_code, 201)
            current = client.get(f"/v1/quick-chat/sessions/{sid}").json()["session"]
            self.assertEqual(current["title_generation"]["status"], "completed")
            self.assertEqual(current["title"], "夜林水刃对决")
            client.post(f"/v1/quick-chat/sessions/{sid}/cards", json=body, headers={"Idempotency-Key": "route-card"})
        self.assertEqual(len(self.generator.calls), 2)

    def test_manual_title_initial_and_explicit_default_patch_never_overwritten(self):
        named = self.service.create_session(self.agent, {"title": "我的片段"}, "manual-session")["session"]
        self.service.create_turn(self.agent, named["id"], {**self.turn_body(), "expected_version": 1}, "manual-turn")
        self.assertFalse(self.service.generate_title(self.agent, named["id"]))
        self.service.create_turn(self.principal, self.sid, self.turn_body(), "first-turn")
        self.service.patch_session(self.principal, self.sid,
            {"expected_version": self.current()["version"], "title": "新的创作"}, "manual-default")
        self.assertFalse(self.service.generate_title(self.principal, self.sid))
        self.assertEqual(self.current()["title_generation"]["status"], "manual")
        self.assertEqual(self.generator.calls, [])

    def test_explicit_default_title_creation_is_manual_by_field_presence(self):
        for index, title in enumerate(("新的创作", " 新的创作 ", "\t新的创作\t")):
            with self.subTest(title=title):
                session = self.service.create_session(self.agent, {"title": title}, f"explicit-default-{index}")["session"]
                self.assertEqual(session["title_generation"]["status"], "manual")
                self.service.create_turn(self.agent, session["id"],
                    {**self.turn_body(), "expected_version": 1}, f"explicit-turn-{index}")
                self.assertFalse(self.service.generate_title(self.agent, session["id"]))
                self.assertEqual(self.service.get_session(self.agent, session["id"])["session"]["title"], title)
        self.assertEqual(self.generator.calls, [])

    def test_omitted_title_all_creation_shapes_remain_eligible(self):
        for index, body in enumerate(({}, {"model_id": "gemini-3.8-flash"}, {"model_id": "gemma-4-31b-it"})):
            with self.subTest(body=body):
                session = self.service.create_session(self.agent, body, f"omitted-title-{index}")["session"]
                self.assertEqual(session["title"], "新的创作")
                self.assertEqual(session["title_generation"]["status"], "awaiting_input")
                self.service.create_turn(self.agent, session["id"],
                    {**self.turn_body(), "expected_version": 1}, f"omitted-turn-{index}")
                self.assertTrue(self.service.generate_title(self.agent, session["id"]))
                current = self.service.get_session(self.agent, session["id"])["session"]
                self.assertEqual(current["title"], "夜林水刃对决")
                self.assertEqual(current["title_generation"]["status"], "completed")
        self.assertEqual(len(self.generator.calls), 3)

    def test_manual_update_while_network_running_wins_without_holding_database_lock(self):
        self.service.create_turn(self.principal, self.sid, self.turn_body(), "first-turn")
        self.generator.release = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.service.generate_title, self.principal, self.sid)
            self.assertTrue(self.generator.entered.wait(3))
            patched = self.service.patch_session(self.principal, self.sid,
                {"expected_version": self.current()["version"], "title": "保留人工标题"}, "manual-race")["session"]
            self.generator.release.set()
            self.assertFalse(future.result(timeout=3))
        self.assertEqual(self.current()["title"], "保留人工标题")
        self.assertEqual(self.current()["version"], patched["version"])
        self.assertEqual(len(self.generator.calls), 1)

    def test_parallel_api_claims_and_unrelated_settings_preserve_one_call_and_latest_payload(self):
        self.service.create_turn(self.principal, self.sid, self.turn_body(), "first-turn")
        other = QuickChatService(self.repo, self.service.assets, self.service.settings, self.service.storage,
                                title_generator=self.generator)
        self.generator.release = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.service.generate_title, self.principal, self.sid)
            self.assertTrue(self.generator.entered.wait(3))
            self.assertFalse(other.generate_title(self.agent, self.sid))
            next_settings = {**copy.deepcopy(DEFAULT_SETTINGS), "copies": 3}
            patched = self.service.patch_session(self.principal, self.sid,
                {"expected_version": self.current()["version"], "next_settings": next_settings}, "settings-race")["session"]
            self.generator.release.set()
            self.assertTrue(future.result(timeout=3))
        self.assertEqual(self.current()["next_settings"], next_settings)
        self.assertEqual(self.current()["version"], patched["version"])
        self.assertEqual(len(self.generator.calls), 1)

    def test_failure_safe_codes_no_retry_no_generation_block(self):
        self.generator.error = RuntimeError("PRIVATE PROMPT / API KEY")
        turn = self.service.create_turn(self.principal, self.sid, self.turn_body(), "first-turn")
        self.assertIsNotNone(turn["card_id"])
        self.assertFalse(self.service.generate_title(self.principal, self.sid))
        self.assertEqual(self.current()["title"], "新的创作")
        self.assertEqual(self.current()["title_generation"]["error_code"], "title_generation_failed")
        self.generator.error = None
        self.service.create_turn(self.principal, self.sid, self.turn_body("火车疾驶"), "next-turn")
        self.assertFalse(self.service.generate_title(self.principal, self.sid))
        self.assertEqual(len(self.generator.calls), 1)

    def test_isolation_and_readonly_queries_never_run_title(self):
        self.service.create_turn(self.principal, self.sid, self.turn_body(), "first-turn")
        self.assertFalse(self.service.generate_title(Principal("supervan", "browser"), self.sid))
        reader = Principal("superdan", "read-key", machine=True, all_projects=True, scopes=("projects:read",))
        self.service.get_session(reader, self.sid)
        self.service.list_sessions(reader)
        self.assertFalse(self.service.generate_title(reader, self.sid))
        self.assertEqual(self.generator.calls, [])
        self.assertEqual(self.current()["title_generation"]["status"], "pending")

    def test_disabled_and_existing_sessions_are_not_backfilled(self):
        self.service.title_generator = None
        old = self.service.create_session(self.principal, {}, "disabled-session")["session"]
        self.assertIsNone(old["title_generation"])
        self.service.title_generator = self.generator
        self.service.create_turn(self.principal, old["id"], {**self.turn_body(), "expected_version": 1}, "old-turn")
        self.assertFalse(self.service.generate_title(self.principal, old["id"]))
        self.assertEqual(self.generator.calls, [])

    def test_process_interruption_fences_late_result_and_recovery_never_calls_provider(self):
        self.service.create_turn(self.principal, self.sid, self.turn_body(), "first-turn")
        self.generator.release = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.service.generate_title, self.principal, self.sid)
            self.assertTrue(self.generator.entered.wait(3))
            now = self.repo.clock()
            self.repo.clock = lambda: now + 200
            self.assertEqual(self.service.recover_title_runs(), 1)
            self.generator.release.set()
            self.assertFalse(future.result(timeout=3))
        self.assertEqual(self.current()["title_generation"]["error_code"], "title_call_unknown")
        self.assertFalse(self.service.generate_title(self.principal, self.sid))
        self.assertEqual(self.service.recover_title_runs(), 0)
        self.assertEqual(len(self.generator.calls), 1)

    def test_interrupted_pending_task_is_fenced_after_restart_not_reissued(self):
        self.service.create_turn(self.principal, self.sid, self.turn_body(), "first-turn")
        now = self.repo.clock()
        self.repo.clock = lambda: now + 200
        replacement = QuickChatService(self.repo, self.service.assets, self.service.settings, self.service.storage,
                                      title_generator=self.generator)
        self.assertEqual(replacement.recover_title_runs(), 1)
        self.assertEqual(self.current()["title_generation"]["error_code"], "title_interrupted_before_call")
        self.assertFalse(replacement.generate_title(self.principal, self.sid))
        self.assertEqual(self.generator.calls, [])

    def test_title_sanitization_rejects_private_verbose_responses(self):
        self.assertEqual(clean_title("**标题：夜林对决**"), "夜林对决")
        self.assertEqual(clean_title({"text": '"Rainy Train Chase"'}), "Rainy Train Chase")
        self.assertEqual(clean_title("夜林\u202e对决"), "夜林对决")
        for value in ("", "---", "x" * 41, "Title\nExplanation", "<script>bad</script>", {"no_text": "private"}):
            with self.assertRaises(ValueError):
                clean_title(value)

    def test_safe_timeout_classification_and_minimum_recovery_window(self):
        self.assertEqual(title_error_code(TimeoutError("private")), "title_call_unknown")
        error = RuntimeError("private")
        error.code = "PRIVATE KEY"
        self.assertEqual(title_error_code(error), "title_generation_failed")
        error.code = {"private": "untrusted"}
        self.assertEqual(title_error_code(error), "title_generation_failed")
        with self.assertRaises(ValueError):
            self.service.recover_title_runs(older_than_s=1)


if __name__ == "__main__":
    unittest.main()
