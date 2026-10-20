"""Telegram demo bot (#382): allowlist, chat flow and token hygiene.

Synthetic photos and a fake Telegram API only; no network, registry or GPU.
"""

from pathlib import Path
import shutil
import tempfile
import unittest

import httpx
from PIL import Image

from telegram_bot.adapter import Storyboard
from telegram_bot.bot import Bot, Store, parse_card, parse_second
from telegram_bot.demo_adapter import DemoAdapter
from telegram_bot.telegram_api import TelegramAPI, TelegramError

OWNER = 1001
STRANGER = 2002
HAS_FFMPEG = shutil.which("ffmpeg") is not None


class FakeAPI:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.sent = []
        self.downloads = 0

    def _record(self, kind, chat_id, text, buttons=None, path=None):
        self.sent.append(
            {
                "kind": kind,
                "chat": chat_id,
                "text": text,
                "buttons": buttons,
                "path": path,
            }
        )
        return {"message_id": len(self.sent)}

    def send_message(self, chat_id, text, buttons=None):
        return self._record("message", chat_id, text, buttons)

    def send_photo(self, chat_id, path, caption="", buttons=None):
        assert Path(path).exists()
        return self._record("photo", chat_id, caption, buttons, path)

    def send_video(self, chat_id, path, caption="", buttons=None):
        assert Path(path).stat().st_size > 0
        return self._record("video", chat_id, caption, buttons, path)

    def edit_message_text(self, chat_id, message_id, text):
        return True

    def answer_callback(self, callback_id, text=None):
        return True

    def download(self, file_id, dest):
        self.downloads += 1
        colour = [(200, 80, 60), (60, 160, 90), (70, 90, 200), (220, 200, 80)][
            self.downloads % 4
        ]
        Image.new("RGB", (640, 480), colour).save(dest, "JPEG")
        return str(dest)


def private(user, **message):
    return {
        "update_id": 1,
        "message": {
            "from": {"id": user, "is_bot": False},
            "chat": {"id": user, "type": "private"},
            **message,
        },
    }


def tap(user, data):
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cb",
            "from": {"id": user, "is_bot": False},
            "message": {"chat": {"id": user, "type": "private"}},
            "data": data,
        },
    }


def photo(user, n, group=None):
    return private(
        user,
        photo=[{"file_id": f"f{n}", "file_unique_id": f"u{n}", "file_size": 10}],
        media_group_id=group,
    )


class TelegramBotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.api = FakeAPI(root)
        self.bot = Bot(self.api, DemoAdapter(root / "work"), Store(root / "state"),
                       [OWNER], root / "media")  # fmt: skip

    def tearDown(self):
        self.tmp.cleanup()

    def test_refuses_empty_allowlist(self):
        with self.assertRaises(ValueError):
            Bot(self.api, None, None, [], self.tmp.name)

    def test_ignores_everyone_else_without_reply(self):
        group = private(OWNER, text="hi")
        group["message"]["chat"] = {"id": -5, "type": "group"}
        for update in (
            private(STRANGER, text="/start"),
            photo(STRANGER, 1),
            tap(STRANGER, "gen_ok"),
            group,
            {"update_id": 2, "edited_message": {}},
        ):
            self.bot.handle(update)
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.api.downloads, 0)
        self.assertEqual(self.bot.ignored, 5)

    def test_storyboard_proposal_and_revision(self):
        for n in range(1, 5):
            self.bot.handle(photo(OWNER, n, group="g1"))
        self.assertEqual(sum(m["kind"] == "message" for m in self.api.sent), 1)
        self.bot.handle(private(OWNER, text="咖啡店开业，温馨一点"))
        board_msg = self.api.sent[-1]
        self.assertEqual(board_msg["kind"], "photo")
        self.assertIn("咖啡店", board_msg["text"])
        self.assertIn("#5", board_msg["text"])  # four photos plus a closing card
        self.bot.handle(private(OWNER, text="删掉第2张，第3张换成照片4，横屏"))
        state = self.bot.store.load(OWNER)
        board = Storyboard.from_dict(state["board"])
        # Numbers refer to the board as shown: old card 3 gets photo 4, then
        # old card 2 goes, so old card 3 is now second.
        self.assertEqual(len(board.cards), 4)
        self.assertEqual([c.photo for c in board.cards], [1, 4, 4, 1])
        self.assertEqual(board.cards[1].title, "招牌饮品")
        self.assertEqual(board.aspect, "16:9")
        self.assertIn("删掉了第2张", self.api.sent[-1]["text"])
        self.assertIn("第3张换成照片4", self.api.sent[-1]["text"])

    def test_parsers(self):
        self.assertEqual(parse_second("第12秒人脸怪"), 12)
        self.assertEqual(parse_second("at 0:07 too dark"), 7)
        self.assertEqual(parse_second("3.5s"), 3.5)
        self.assertIsNone(parse_second("第3张太暗"))
        self.assertEqual(parse_card("第3张太暗"), 3)
        self.assertEqual(parse_card("card 2 brighter"), 2)
        board = DemoAdapter(self.tmp.name).draft(1, ["a", "b", "c"], "")
        self.assertEqual(board.locate(0), 1)
        self.assertEqual(board.locate(3.2), 2)
        self.assertIsNone(board.locate(board.duration))

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
    def test_generate_locate_and_regenerate_one_card(self):
        for n in range(1, 4):
            self.bot.handle(photo(OWNER, n))
        self.bot.handle(private(OWNER, text="花店，短一点"))
        self.bot.handle(private(OWNER, text="短一点"))
        self.bot.handle(tap(OWNER, "gen"))
        self.assertIn("不会扣积分", self.api.sent[-1]["text"])
        self.bot.handle(tap(OWNER, "gen_ok"))
        film_msg = self.api.sent[-1]
        self.assertEqual(film_msg["kind"], "video")
        before = self.bot.store.load(OWNER)["film"]["clips"]

        self.bot.handle(private(OWNER, text="第3秒人脸怪"))
        frame_msg = self.api.sent[-1]
        self.assertEqual(frame_msg["kind"], "photo")
        self.assertIn("第2张", frame_msg["text"])
        self.bot.handle(tap(OWNER, "fix_ok"))
        state = self.bot.store.load(OWNER)
        after = state["film"]["clips"]
        self.assertNotEqual(before[1], after[1])
        self.assertEqual(before[0], after[0])
        self.assertEqual(before[2:], after[2:])
        self.assertEqual(state["board"]["cards"][1]["version"], 2)
        self.assertEqual(self.api.sent[-1]["kind"], "video")

    def test_stale_button_does_nothing_dangerous(self):
        self.bot.handle(tap(OWNER, "gen_ok"))
        self.bot.handle(tap(OWNER, "fix_ok"))
        self.assertTrue(all("过期" in m["text"] for m in self.api.sent))


class TelegramApiTest(unittest.TestCase):
    TOKEN = "123456:synthetic-token-never-real"

    def test_errors_and_repr_never_include_token(self):
        def boom(request):
            raise httpx.ConnectError("failed " + str(request.url))

        api = TelegramAPI(self.TOKEN, transport=httpx.MockTransport(boom))
        with self.assertRaises(TelegramError) as caught:
            api.get_me()
        self.assertNotIn("synthetic-token", str(caught.exception))
        self.assertNotIn("synthetic-token", repr(api))

    def test_api_error_description(self):
        def reply(request):
            return httpx.Response(400, json={"ok": False, "description": "Bad Request"})

        api = TelegramAPI(self.TOKEN, transport=httpx.MockTransport(reply))
        with self.assertRaises(TelegramError) as caught:
            api.send_message(1, "x")
        self.assertIn("Bad Request", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
