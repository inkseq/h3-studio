"""Telegram front end of the core story flow (#531): renders the server's story chat and
relays taps; never plans, writes cards or submits without a confirm tap."""

import tempfile
import unittest
from pathlib import Path

from telegram_bot.flow import Flow, Store
from telegram_bot.studio import StudioError


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, buttons=None):
        self.sent.append(("text", chat_id, text, buttons))

    def send_video(self, chat_id, path, caption="", buttons=None):
        self.sent.append(("video", chat_id, caption, buttons))

    def send_chat_action(self, chat_id, action="typing"):
        pass

    def answer_callback(self, callback_id, text=None):
        pass

    def buttons(self):
        rows = self.sent[-1][3] or []
        return [data for row in rows for _, data in row]


class FakeLinker:
    def __init__(self, linked=True):
        self.linked = linked

    def get(self, chat_id):
        return {"origin": "https://staging.inkseq.com"} if self.linked else None

    def token(self, chat_id):
        return "sxp_test" if self.linked else None


VIEW = {
    "project_id": "project-1",
    "version": 3,
    "title": "Rain dumplings",
    "cards": {
        "characters": [{"id": "c1", "name": "Courier"}],
        "places": [{"id": "l1", "name": "Old street"}],
        "props": [],
        "shots": [
            {"id": "s1", "text": "He rides through the rain.", "takes": 0},
            {"id": "s2", "text": "She opens the door.", "takes": 2},
        ],
    },
}


class FakeStudio:
    def __init__(self):
        self.calls = []
        self.versions_rows = []

    def create_chat(self, language):
        self.calls.append(("create", language))
        return {**VIEW, "cards": {}}

    def chat(self, project_id):
        return VIEW

    def chats(self, limit=10):
        return [{"project_id": "project-1", "title": "Rain dumplings"}]

    def turn(self, project_id, turn_id, text):
        self.calls.append(("turn", project_id, turn_id, text))
        return {
            **VIEW,
            "turn": {
                "messages": [{"role": "assistant", "text": "Here is the story."}],
                "chips": ["Make it funny"],
                "proposal": {"shot_ids": ["s1", "s2"]},
            },
        }

    def preflight(self, project_id, expected_version, shot_ids, key, copies=1):
        self.calls.append(("preflight", expected_version, tuple(shot_ids)))
        return {"id": "pre-1", "status": "pending"}

    def preflight_status(self, project_id, preflight_id, **_):
        return {
            "id": preflight_id,
            "status": "completed",
            "ready": True,
            "credits": 24,
            "items": [{"plan_id": "plan-1"}, {"plan_id": "plan-2"}],
        }

    def submit(self, project_id, plan_ids, key):
        self.calls.append(("submit", tuple(plan_ids), key))
        return {}

    def versions(self, project_id, shot_id):
        return {"versions": self.versions_rows}

    def download(self, artifact_id, dest, rendition=None):
        Path(dest).write_bytes(b"mp4")
        return dest

    def choose(
        self, project_id, shot_id, artifact_id, expected_version, clear_segments=False
    ):
        self.calls.append(("choose", shot_id, artifact_id, clear_segments))
        if not clear_segments and shot_id == "segmented":
            raise StudioError(422, "storyboard_segments_source_changed")
        return {}


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tg, self.studio = FakeTelegram(), FakeStudio()
        self.flow = Flow(
            self.tg,
            FakeLinker(),
            Store(Path(self.tmp.name) / "state"),
            Path(self.tmp.name) / "media",
            origin="https://staging.inkseq.com",
            client_factory=lambda origin, token: self.studio,
            clock=lambda: 1000.0,
        )

    def message(self, text, message_id=1):
        self.flow.handle(
            {
                "message": {
                    "chat": {"id": 7},
                    "message_id": message_id,
                    "text": text,
                    "from": {"id": 7, "language_code": "en"},
                }
            }  # fmt: skip
        )

    def tap(self, data, query_id="q1"):
        self.flow.handle_tap(
            {"id": query_id, "data": data, "message": {"chat": {"id": 7}}}
        )

    def test_unlinked_people_get_the_connect_link_and_nothing_runs(self):
        self.flow.linker = FakeLinker(linked=False)
        self.message("Make a story")
        self.assertIn(
            "url:https://staging.inkseq.com/connect/telegram", self.tg.buttons()
        )
        self.assertEqual(self.studio.calls, [])

    def test_text_goes_to_the_story_chat_and_the_reply_shows_cards_and_buttons(self):
        self.message("A courier brings dumplings in the rain")
        self.assertEqual(self.studio.calls[0], ("create", "en"))
        self.assertEqual(self.studio.calls[1][0], "turn")
        kind, _, text, _ = self.tg.sent[-1]
        self.assertIn("Here is the story.", text)
        self.assertIn("Courier", text)
        self.assertIn("2. She opens the door. (2 takes to choose from)", text)
        self.assertEqual(self.tg.buttons()[:2], ["c:0", "g"])

    def test_a_chip_tap_sends_that_chip_as_the_next_turn(self):
        self.message("A story")
        self.tap("c:0")
        self.assertEqual(self.studio.calls[-1][3], "Make it funny")

    def test_generation_needs_a_quote_and_an_explicit_confirm(self):
        self.message("A story")
        self.tap("g")
        self.assertEqual(self.studio.calls[-1], ("preflight", 3, ("s1", "s2")))
        self.assertIn("24 credits", self.tg.sent[-1][2])
        self.assertNotIn("submit", [c[0] for c in self.studio.calls])
        self.tap("b:pre-1")
        self.assertEqual(
            self.studio.calls[-1], ("submit", ("plan-1", "plan-2"), "tg-batch-pre-1")
        )
        # A repeated confirm tap does not submit again.
        self.tap("b:pre-1")
        self.assertEqual(sum(c[0] == "submit" for c in self.studio.calls), 1)

    def test_cancel_submits_nothing(self):
        self.message("A story")
        self.tap("g")
        self.tap("x")
        self.assertNotIn("submit", [c[0] for c in self.studio.calls])

    def test_finished_takes_arrive_once_with_use_this(self):
        self.message("A story")
        self.tap("g")
        self.tap("b:pre-1")
        self.studio.versions_rows = [{"id": "art-1", "kind": "video"}]
        self.flow.deliver()
        videos = [s for s in self.tg.sent if s[0] == "video"]
        self.assertEqual(len(videos), 2)  # one per watched shot
        self.assertIn("u:s1|art-1", [d for _, d in videos[0][3][0]])
        self.flow.deliver()
        self.assertEqual(len([s for s in self.tg.sent if s[0] == "video"]), 2)
        self.tap("u:s1|art-1")
        self.assertEqual(self.studio.calls[-1], ("choose", "s1", "art-1", False))

    def test_choosing_a_take_on_a_shot_with_kept_parts_clears_them_in_the_same_edit(
        self,
    ):
        self.message("A story")
        self.tap("u:segmented|art-9")
        self.assertEqual(self.studio.calls[-1], ("choose", "segmented", "art-9", True))

    def test_story_list_and_switch(self):
        self.message("/stories")
        self.assertEqual(self.tg.buttons(), ["s:project-1"])
        self.tap("s:project-1")
        self.assertIn("Now on", self.tg.sent[-2][2])


if __name__ == "__main__":
    unittest.main()
