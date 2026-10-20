"""Telegram bot staging adapter (#382) against a fake studio server.

No network: httpx.MockTransport stands in for staging. Checks the story/asset/
shot writes, that deletions touch only bot-created drafts, that preflight is
read but generation is never submitted, and that credentials stay hidden.
"""

import json
import re
from pathlib import Path
import tempfile
import unittest

import httpx
from PIL import Image

from telegram_bot.bot import GenerationHeld
from telegram_bot.staging_adapter import STAGING, StagingAdapter, StudioClient

PASSWORD = "synthetic-qa-password"


class FakeStudio:
    def __init__(self):
        self.version = 1
        self.actions = []
        self.paths = []
        self.keys = []

    def __call__(self, request):
        path, method = request.url.path, request.method
        self.paths.append(f"{method} {path}")
        if request.headers.get("Idempotency-Key"):
            self.keys.append(request.headers["Idempotency-Key"])
        if path == "/api/auth/login":
            return httpx.Response(200, json={"ok": True})
        if path == "/v1/projects" and method == "POST":
            return httpx.Response(201, json={"id": "project-1", "version": 1})
        if path == "/v1/assets" and method == "POST":
            # IDs follow the photo number, so parallel uploads stay deterministic.
            n = int(re.search(rb"tg-[0-9a-f]+-p(\d+)", request.content).group(1))
            return httpx.Response(
                201, json={"asset_id": f"asset-{n}", "status": "ready"}
            )
        if path.endswith("/meta"):
            return httpx.Response(200, json={"version": self.version})
        if path.endswith("/actions"):
            body = json.loads(request.content)
            assert body["expected_version"] == self.version
            self.actions.extend(body["actions"])
            self.version += 1
            return httpx.Response(
                200, json={"id": "project-1", "version": self.version}
            )
        if path.endswith("/storyboard/preflights") and method == "POST":
            return httpx.Response(
                202,
                json={"id": "pf-1", "status": "ready", "estimate": {"credits": 120}},
            )
        if "/batches" in path or "/jobs" in path or "/films" in path:
            raise AssertionError("paid submission attempted: " + path)
        return httpx.Response(404, json={"code": "not_found"})


class StagingAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.photos = []
        for i in range(3):
            path = root / f"p{i}.jpg"
            Image.new("RGB", (640, 800), (40 * i, 90, 120)).save(path)
            self.photos.append(str(path))
        self.server = FakeStudio()
        client = StudioClient(
            STAGING, "qa-user", PASSWORD, transport=httpx.MockTransport(self.server)
        )
        self.adapter = StagingAdapter(root / "work", client)

    def tearDown(self):
        self.tmp.cleanup()

    def ops(self, name):
        return [a for a in self.server.actions if a["op"] == name]

    def test_unavailable_tier_is_reported_not_raised(self):
        board = self.adapter.plan_shots(self.adapter.start(1, self.photos, "花店"))
        original = self.server.__call__

        def refuse(request):
            if (
                request.url.path.endswith("/actions")
                and b"configure_generation" in request.content
            ):
                return httpx.Response(409, json={"code": "quality_tier_unavailable"})
            return original(request)

        self.adapter.client._client._transport = httpx.MockTransport(refuse)
        estimate = self.adapter.estimate(board)
        self.assertIn("quality_tier_unavailable", estimate.checks[0])

    def test_gemini_plan_is_saved_as_the_staging_story(self):
        from telegram_bot.llm_adapter import GeminiClient
        from telegram_bot.staging_adapter import LlmStagingAdapter
        from tests.test_telegram_bot_llm import KEY, FakeGemini

        gemini = GeminiClient(
            KEY, "gemini-test", transport=httpx.MockTransport(FakeGemini())
        )
        adapter = LlmStagingAdapter(
            Path(self.tmp.name) / "both", self.adapter.client, gemini
        )
        notices = []
        adapter.on_saved = lambda chat_id, link: notices.append((chat_id, link))
        board = adapter.plan_shots(adapter.start(7, self.photos[:2], "新品上市"))
        self.assertEqual(len(board.cards), 3)  # the proposal is ready before the save
        board = adapter._settle(board)  # wait for the background save
        self.assertEqual(
            notices, [(7, "https://staging.inkseq.com/?project=project-1")]
        )
        self.assertEqual([c.ref for c in board.cards], ["shot-1", "shot-2", "shot-3"])
        self.assertIn("POST /v1/projects", self.server.paths)
        self.assertEqual(
            self.ops("project.update")[0]["patch"]["title"], "阿明咖啡新品"
        )
        titles = [a["entity"]["title"] for a in self.ops("entity.create")
                  if a["entity"]["type"] == "shot"]  # fmt: skip
        self.assertEqual(titles, ["开场", "超出范围", "结尾"])  # Gemini's parts, saved
        self.assertEqual(
            [a["title"] for a in self.ops("asset.attach")], ["门头招牌", "拿铁拉花"]
        )
        self.assertEqual(self.ops("storyboard.defaults")[0]["value"]["controls"],
                         {"aspect_ratio": "9:16"})  # fmt: skip
        self.assertEqual(self.ops("journey.update")[0]["patch"]["brief"]["aspect"], "9:16")
        board, _summary = adapter.revise(board, "结尾不要了")  # Gemini keeps one part
        self.assertEqual(
            sorted(a["entity_id"] for a in self.ops("entity.delete")),
            ["shot-2", "shot-3"],
        )
        self.assertEqual(board.remote["project"], "project-1")

    def test_refuses_anything_but_staging(self):
        with self.assertRaises(ValueError):
            StudioClient("https://inkseq.com", "qa-user", PASSWORD)
        self.assertNotIn(PASSWORD, repr(self.adapter.client))

    def test_story_materials_shots_and_unpaid_preflight(self):
        board = self.adapter.start(1, self.photos, "咖啡店开业")
        self.assertEqual(board.remote["assets"], ["asset-1", "asset-2", "asset-3"])
        self.assertEqual(len(self.ops("asset.attach")), 3)
        board = self.adapter.rename_material(board, 2, "拿铁")
        self.assertEqual(self.ops("library.rename")[0]["title"], "拿铁")

        board = self.adapter.plan_shots(board)
        created = [a["entity"] for a in self.ops("entity.create")]
        self.assertEqual([e["type"] for e in created[:2]], ["chapter", "scene"])
        shots = [e for e in created if e["type"] == "shot"]
        self.assertEqual(len(shots), len(board.cards))
        first = self.ops("link.create")[0]["link"]
        self.assertEqual(
            (first["source"], first["target"], first["role"]),
            ("material-1", "shot-1", "firstFrame"),
        )
        self.assertEqual(
            self.ops("shot.configure_generation"), []
        )  # only at quote time

        board = self.adapter.swap_material(board, 2, 3)
        self.assertEqual(self.ops("link.delete")[-1]["link_id"], "link-shot-2-2")
        self.assertEqual(self.ops("link.create")[-1]["link"]["source"], "material-3")
        board = self.adapter.delete_card(board, 4)
        self.assertEqual(
            [a["entity_id"] for a in self.ops("entity.delete")], ["shot-4"]
        )

        estimate = self.adapter.estimate(board)
        config = self.ops("shot.configure_generation")[0]
        self.assertEqual(config["inputs"]["first_frame"], {"asset_id": "asset-1"})
        self.assertGreaterEqual(config["controls"]["duration"], 4)
        self.assertEqual(estimate.credits, 120)
        self.assertIn("未扣费", estimate.note)
        with self.assertRaises(GenerationHeld):
            self.adapter.generate(1, board, lambda done, total: None)
        self.assertTrue(
            all("batches" not in p and "jobs" not in p for p in self.server.paths)
        )
        self.assertTrue(all(k.startswith("tg-") for k in self.server.keys))
        self.assertIn(
            "?project=project-1&entity=shot-1", self.adapter.web_link(board, 1)
        )


if __name__ == "__main__":
    unittest.main()
