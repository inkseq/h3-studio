"""Inky answers a complaint about a finished take from our recorded fixes (#1227)."""

from pathlib import Path
import re
import tempfile
import unittest

from fastapi.testclient import TestClient

from studio_platform import inky_fix_guide as guide
from studio_platform.api import create_app
from studio_platform.prompt_files import text as prompt_text
from studio_platform.settings import Settings
from test_story_chat import CAST, ScriptedModel

# What a person would type for each row; the row must come first.
COMPLAINTS = {
    "too_slow": "这条太慢了，像慢动作",
    "reveal_too_fast": "手印一闪就过去了，看不清",
    "speech_flat": "她说话很慢，像念稿",
    "set_mismatch": "门对不上，每个镜头的电梯不一样",
    "background_jump": "背景突然变了，冒出一扇旋转门",
    "extra_person": "后面多了一个人",
    "prop_changed": "桶变成了杯子",
    "sleeve_wrong": "手上的袖子不对",
    "texture_overlay": "整个画面像盖了一层花纹",
    "wrong_motion": "掀错方向了，门开得很奇怪",
    "camera_move_missing": "镜头没转过去",
    "sound_out_of_sync": "撞击声和画面对不上",
    "wrong_expression": "该难过的时候她在笑，赔笑太夸张",
    "other_mouth_moves": "嘴在动但不是他在说",
    "line_misspoken": "台词说错字了",
    "line_late": "台词来得太晚，开口前站了好久",
    "wrong_line": "他说的不是这句",
    "subtitles": "画面下面出现了字幕",
    "voice_changes": "声音前后不像同一个人",
    "stuck_waiting": "任务一直排队不动",
    "failed_with_sound": "加了声音就失败",
    "generation_failed": "生成失败，让我稍后再试",
}
ENGLISH = {
    "too_slow": "It's too slow, like slow motion",
    "set_mismatch": "The doors don't match between shots",
    "line_late": "The line comes late, he waits before speaking",
    "extra_person": "Someone appeared behind her, who is that?",
}
# Names users must never see (owner: no models, suppliers, prices or internal steps).
INTERNAL = re.compile(
    r"H3|Pika|EveryFrame|Vivix|Modal|RunPod|Gemini|Terra|Jev|Eleven|Seedance|Kling|"
    r"\bL20\d{6}|USD|\$|\.py\b|ASR|识别时间戳|omni",
    re.I,
)


class FixGuideRowTests(unittest.TestCase):
    def test_every_row_is_found_by_its_complaint(self):
        self.assertEqual(
            {"take." + name for name in COMPLAINTS}, {row["id"] for row in guide.ROWS}
        )
        for row_id, complaint in {**COMPLAINTS, **ENGLISH}.items():
            found = guide.match(complaint)
            self.assertTrue(found, complaint)
            self.assertEqual(found[0]["id"], "take." + row_id, complaint)

    def test_every_row_has_chinese_and_english_phrases(self):
        for row in guide.ROWS:
            phrases = [phrase for phrase, _pattern in row["when"]]
            self.assertTrue(any(re.search(r"[㐀-鿿]", p) for p in phrases), row["id"])
            self.assertTrue(any(re.search(r"[a-z]", p) for p in phrases), row["id"])

    def test_rows_and_note_name_no_internals(self):
        said = [
            f"{row['cause']} {row['fix']} {row['edit'] or ''}" for row in guide.ROWS
        ]
        text = " ".join(said) + prompt_text("inky_fix_note")
        self.assertIsNone(INTERNAL.search(text), INTERNAL.search(text))

    def test_ordinary_requests_bring_no_rows(self):
        for text in (
            "帮我把她的名字改成小红",
            "Add a shot where Mia waves",
            "it says terror in the title",
            "再加一个镜头，她走进电梯",
        ):
            self.assertEqual(guide.match(text), [], text)

    def test_at_most_three_rows_with_the_longest_match_first(self):
        found = guide.match("太慢了，门对不上，多了一个人，还有字幕，声音对不上")
        self.assertEqual(len(found), guide.LIMIT)
        self.assertEqual(guide.match("声音对不上")[0]["id"], "take.sound_out_of_sync")

    def test_note_marks_what_inky_may_edit(self):
        self.assertEqual(guide.note([]), "")
        edit = guide.note(guide.match("后面多了一个人"))
        self.assertIn("You can edit:", edit)
        self.assertIn("off-screen person", edit)
        point = guide.note(guide.match("台词来得太晚"))
        self.assertIn("The person does it", point)
        self.assertIn("film editor", point)

    def test_rows_are_the_table_entries_that_answer_complaints(self):
        from studio_platform import pitfalls

        answering = [e["id"] for e in pitfalls.ENTRIES if "complaint" in e["uses"]]
        self.assertEqual([row["id"] for row in guide.ROWS], answering)


class FixGuideTurnTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = Settings(
            Path(tmp.name), auth_mode="local-test", story_chat_enabled=True
        )
        self.model = ScriptedModel()
        self.app = create_app(settings, story_chat_client=self.model)
        self.addCleanup(self.app.state.repository.engine.dispose)
        self.client = TestClient(self.app)
        login = self.client.post("/api/auth/login", json={"username": "superdan"})
        self.assertEqual(login.status_code, 200)
        chat = self.client.post("/v1/story-chats", json={"language": "zh"}).json()
        self.project_id = chat["project_id"]
        self.model.plans.append(CAST)
        self.story = self.client.post(
            f"/v1/story-chats/{self.project_id}/turns",
            json={"turn_id": "turn-0001", "text": "外星人"},
        ).json()
        self.turns = 1

    def project(self):
        return self.client.get(f"/v1/projects/{self.project_id}").json()["project"]

    def shot(self):
        return next(e for e in self.project()["entities"] if e["type"] == "shot")

    def finish_take(self):
        """The first shot gets one finished video, as a generation leaves it."""
        project, shot = self.project(), self.shot()
        response = self.client.post(
            f"/v1/projects/{self.project_id}/actions",
            json={
                "expected_version": self.client.get(
                    f"/v1/projects/{self.project_id}"
                ).json()["version"],
                "actions": [
                    {
                        "op": "entity.create",
                        "entity": {
                            "id": "video-take-0001",
                            "type": "video",
                            "parentId": None,
                            "title": "take",
                            "description": "",
                            "data": {"cloudArtifactId": "take0001"},
                        },
                    },
                    {
                        "op": "entity.update",
                        "entity_id": shot["id"],
                        "patch": {
                            "data": {
                                **shot["data"],
                                "candidateIds": ["video-take-0001"],
                                "selectedAssetId": "video-take-0001",
                            }
                        },
                    },
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(project)

    def say(self, text, plan, inky=True):
        self.turns += 1
        self.model.plans.append(plan)
        body = {"turn_id": f"turn-{self.turns:04d}", "text": text}
        if inky:
            body["inky"] = {"shot_id": self.shot()["id"]}
        response = self.client.post(
            f"/v1/story-chats/{self.project_id}/turns", json=body
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["turn"], str(self.model.calls[-1]["messages"])

    def test_a_matched_complaint_is_tagged_with_its_entry(self):
        from unittest import mock

        from studio_platform import llm_tracing

        seen, observe = [], llm_tracing.observe

        def recording(name, **fields):
            seen.append((name, fields.get("tags")))
            return observe(name, **fields)

        self.finish_take()
        reply = {"reply": "镜头描述里提到了画外的人。", "actions": []}
        with mock.patch.object(llm_tracing, "observe", recording):
            self.say("后面多了一个人", reply)
            self.say("把她的名字改成小红", reply)
        turns = [tags for name, tags in seen if name == "story-chat-turn"]
        self.assertEqual(len(turns), 2)
        self.assertIn("pitfall:take.extra_person", turns[0])
        self.assertFalse([tag for tag in turns[1] if tag.startswith("pitfall:")])

    def test_a_complaint_gets_the_cause_and_an_offer_then_yes_edits_with_undo(self):
        self.finish_take()
        before = self.shot()["data"].get("prompt")
        offer = {
            "reply": "多出来的人是因为镜头描述里提到了画外的人。要我把这句删掉、视线改成朝画外看吗？",
            "actions": [],
        }
        turn, sent = self.say("后面多了一个人", offer)
        self.assertIn("mentions someone who is off screen", sent)
        self.assertIn("You can edit:", sent)
        self.assertIn("ask whether to make it", sent)
        self.assertEqual(turn["changes"], [])
        self.assertEqual(self.shot()["data"].get("prompt"), before)

        shot = self.shot()
        yes = {
            "reply": "已改：去掉画外的人，视线朝左看出画外。",
            "actions": [
                {"tool": "upsert_shot", "ref": shot["id"], "text": "她朝左看出画外。"}
            ],
        }
        turn, sent = self.say("好，改吧", yes)
        self.assertIn("要我把这句删掉", sent)  # the offer is in the history
        self.assertNotIn("notes on similar complaints", sent)
        self.assertEqual(self.shot()["data"]["prompt"], "她朝左看出画外。")
        undone = self.client.post(
            f"/v1/story-chats/{self.project_id}/undo",
            json={"turn_id": turn["turn_id"]},
        )
        self.assertEqual(undone.status_code, 200, undone.text)
        self.assertEqual(self.shot()["data"].get("prompt"), before)

    def test_a_fix_done_elsewhere_points_there_and_spends_nothing(self):
        self.finish_take()
        plan = {
            "reply": "开口晚了。在剪辑里把这段开头裁到开口前约 0.3 秒。",
            "actions": [{"tool": "propose_generate", "shots": ["all"]}],
        }
        turn, sent = self.say("台词来得太晚", plan)
        self.assertIn("The person does it", sent)
        self.assertIn("0.3 seconds before the line begins", sent)
        self.assertIsNone(turn["proposal"])
        self.assertEqual(turn["changes"], [])

    def test_no_finished_take_or_no_complaint_sends_no_notes(self):
        _turn, sent = self.say("后面多了一个人", {"reply": "好的。", "actions": []})
        self.assertNotIn("notes on similar complaints", sent)
        self.finish_take()
        _turn, sent = self.say("把她的名字改成小红", {"reply": "好的。", "actions": []})
        self.assertNotIn("notes on similar complaints", sent)

    def test_the_story_chat_gets_the_notes_too_but_a_script_does_not(self):
        self.finish_take()
        _turn, sent = self.say(
            "这条太慢了", {"reply": "好的。", "actions": []}, inky=False
        )
        self.assertIn("notes on similar complaints", sent)
        self.assertNotIn("typing to Inky", sent)
        self.turns += 1
        self.model.plans.append({"reply": "好的。", "actions": []})
        response = self.client.post(
            f"/v1/story-chats/{self.project_id}/turns",
            json={
                "turn_id": f"turn-{self.turns:04d}",
                "text": "第一场\n小红：太慢了。",
                "script": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(
            "notes on similar complaints", str(self.model.calls[-1]["messages"])
        )


if __name__ == "__main__":
    unittest.main()
