"""Telegram bot Gemini adapter (#382) against a fake model endpoint.

No network: answers come from httpx.MockTransport. Checks that photos are sent
once, later rounds send text only, out-of-range answers are clamped, a failing
model falls back to the rule-based demo, and the key never appears.
"""

import json
from pathlib import Path
import tempfile
import unittest

import httpx
from PIL import Image

from telegram_bot.llm_adapter import GeminiClient, LlmAdapter

KEY = "synthetic-gemini-key"


def answer(payload):
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    })  # fmt: skip


def card(photo=1, seconds=3, title="开场"):
    return {"title": title, "description": "门口", "photo": photo, "mode": "first_frame",
            "seconds": seconds, "prompt": f"{title}的提示词"}  # fmt: skip


class FakeGemini:
    def __init__(self):
        self.requests = []
        self.fail = False

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        assert request.headers["x-goog-api-key"] == KEY
        if self.fail:
            return httpx.Response(503, json={})
        schema = body["generationConfig"]["responseSchema"]["properties"]
        if "materials" in schema:
            return answer({
                "title": "阿明咖啡新品", "aspect": "9:16", "outline": ["开门", "新品"],
                "materials": [{"name": "门头招牌", "description": "木门"},
                              {"name": "拿铁拉花", "description": "一杯拿铁"}],
                "cards": [card(1), card(9, 30, "超出范围"), card(2, 1, "结尾")],
            })  # fmt: skip
        if "understood" in schema:
            return answer({"summary": "删了结尾", "understood": True, "aspect": "9:16",
                           "renames": [{"photo": 2, "name": "拉花"}], "cards": [{**card(1), "from_part": 1}],
                           "signal": {"kind": "request", "summary": "想要自动字幕"}})  # fmt: skip
        if "part" in schema:
            return answer({"part": 3})
        return answer({"summary": "调亮", "prompt": "更亮的提示词", "seconds": 2})


class LlmAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.photos = []
        for i in range(2):
            path = root / f"p{i}.jpg"
            Image.new("RGB", (1600, 1200), (60 * i, 100, 140)).save(path)
            self.photos.append(str(path))
        self.fake = FakeGemini()
        client = GeminiClient(KEY, "gemini-test", transport=httpx.MockTransport(self.fake),
                              usage_log=root / "usage" / "llm.jsonl")  # fmt: skip
        self.adapter = LlmAdapter(root / "work", client)
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_uses_photos_once_and_clamps_answers(self):
        board = self.adapter.plan_shots(self.adapter.start(1, self.photos, "新品上市"))
        sent = self.fake.requests[0]["contents"][0]["parts"]
        self.assertEqual(sum("inlineData" in p for p in sent), 2)
        self.assertEqual(board.title, "阿明咖啡新品")
        self.assertEqual(board.materials, ["门头招牌", "拿铁拉花"])
        self.assertEqual([c.photo for c in board.cards], [1, 1, 2])  # 9 → 1
        self.assertEqual([c.seconds for c in board.cards], [3.0, 6.0, 2.0])  # clamped
        self.assertTrue((self.root / "usage" / "llm.jsonl").exists())
        self.assertNotIn(KEY, repr(self.adapter.llm))

        board, summary = self.adapter.revise(board, "结尾不要了")
        self.assertEqual(summary, "删了结尾")
        self.assertEqual(self.adapter.take_signal()["summary"], "想要自动字幕")
        self.assertIsNone(self.adapter.take_signal())  # consumed once
        self.assertEqual(len(board.cards), 1)
        self.assertEqual(board.materials[1], "拉花")
        later = self.fake.requests[1]["contents"][0]["parts"]
        self.assertFalse(
            any("inlineData" in p for p in later)
        )  # text only after the first look
        self.assertIn("木门", later[0]["text"])  # what was seen is carried as text

        self.assertIsNone(
            self.adapter.guess_part(board, "最后那个太拖了")
        )  # 3 > 1 part
        proposal = self.adapter.propose_fix(board, 1, "太暗")
        self.assertEqual(
            (proposal.summary, proposal.prompt_after), ("调亮", "更亮的提示词")
        )
        self.assertEqual((proposal.seconds_before, proposal.seconds_after), (3.0, 2.0))

    def test_model_failure_falls_back_to_rules(self):
        self.fake.fail = True
        board = self.adapter.plan_shots(
            self.adapter.start(1, self.photos, "咖啡店开业")
        )
        self.assertIn("（模板）", board.title)
        self.assertTrue(board.cards)
        board, summary = self.adapter.revise(board, "删掉第1段")
        self.assertIn("删掉了第1段", summary)


if __name__ == "__main__":
    unittest.main()
