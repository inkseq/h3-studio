"""Current-language and readiness advice, without generation or provider calls."""

import copy
import unittest

from studio_platform.generation_draft import read_draft
from studio_platform.story_chat_advice import guard, turn_chat
import test_story_chat as fixture
from test_story_chat_images import set_pictures
from test_platform_storyboard import storyboard_project, cap


class AdviceTests(unittest.TestCase):
    setUp = fixture.StoryChatTests.setUp
    login = fixture.StoryChatTests.login
    start = fixture.StoryChatTests.start
    turn = fixture.StoryChatTests.turn
    jobs = fixture.StoryChatTests.jobs

    def test_latest_language_persists_and_replay_is_unchanged(self):
        chat = self.start()
        plan = {"reply": "好", "chips": [], "actions": []}
        response = self.turn(chat, "一个小男孩在车站等车", plan=plan)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["language"], "zh")
        self.assertIn("message): Chinese", self.model.calls[-1]["messages"][-1]["text"])
        second = self.turn(chat, "ok", "turn-lang-02", plan).json()
        self.assertEqual(second["language"], "zh")
        third = self.turn(chat, "Make the ending happier", "turn-lang-03", plan).json()
        self.assertEqual(third["language"], "en")
        replay = self.turn(chat, "Make the ending happier", "turn-lang-03", None).json()
        self.assertEqual(replay["turn"], third["turn"])
        self.assertEqual(len(self.model.calls), 3)
        self.assertEqual(turn_chat({"language": "zh"}, "👍")["language"], "zh")

    def test_switch_change_during_model_call_blocks_proposals_and_chips(self):
        chat = self.start()
        built = self.turn(chat, "故事").json()
        character = built["cards"]["characters"][0]["id"]
        set_pictures(self.app, True)
        self.model.during = lambda: set_pictures(self.app, False)
        response = self.turn(
            chat,
            "画立绘并生成视频",
            "turn-pics-02",
            {
                "reply": "图片和视频已准备好",
                "chips": ["为角色生成立绘", "生成视频", "修改故事"],
                "actions": [
                    {"tool": "propose_image", "ref": character},
                    {"tool": "propose_generate", "shots": ["all"]},
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertIsNone(result["turn"]["proposal"])
        self.assertEqual(result["turn"]["chips"], ["修改故事"])
        self.assertIn("上传照片", result["messages"][-1]["text"])
        self.assertEqual(self.jobs(), 0)

    def test_enabled_pictures_remain_advice_and_missing_refs_block_video(self):
        chat = self.start()
        built = self.turn(chat, "story").json()
        character = built["cards"]["characters"][0]["id"]
        set_pictures(self.app, True)
        result = self.turn(
            chat,
            "Draw and make the video",
            "turn-pics-03",
            {
                "reply": "Review and confirm",
                "actions": [
                    {"tool": "propose_image", "ref": character},
                    {"tool": "propose_generate", "shots": ["all"]},
                ],
            },
        ).json()
        self.assertEqual(result["turn"]["proposal"]["kind"], "image")
        self.assertEqual(result["turn"]["proposal"]["picture_card_ids"], [character])
        self.assertEqual(result["turn"]["proposal"]["shot_ids"], [])
        self.assertEqual(self.jobs(), 0)

    def test_ready_fl_proposal_survives_without_mutating_source(self):
        project = storyboard_project()
        shot = next(e for e in project["entities"] if e["id"] == "shot-one")
        shot["data"]["storyboard"] = {"version": 1, "mode": "fl", "quality": "draft"}
        shot["data"]["h3"] = {"recipeId": "h3-base-fl2va-v1"}
        before = copy.deepcopy(project)
        plan = {"reply": "Review cost", "chips": ["Make video"]}
        proposal = {"kind": "generate", "shot_ids": ["shot-one"]}
        _, actual = guard(
            self.app.state.guided,
            self.settings,
            project,
            [],
            plan,
            proposal,
            enabled=False,
            lang="en",
        )
        self.assertEqual(actual, proposal)
        self.assertEqual(project, before)

    def test_empty_character_and_place_variants_do_not_add_suffixes(self):
        project = storyboard_project()
        for entity in project["entities"]:
            for key in ("looks", "versions"):
                for variant in entity["data"].get(key, []):
                    variant["description"] = "  "
        draft, _ = read_draft(project, "shot-one", capabilities=cap())
        parts = [
            p["text"]
            for p in draft["storyboard"]["prompt_parts"]
            if p["kind"] in {"cast", "place"}
        ]
        self.assertTrue(parts)
        self.assertTrue(all("；" not in text for text in parts))
