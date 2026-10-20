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
        self.edits = []
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

    def edit_message_text(self, chat_id, message_id, text, buttons=None):
        self.edits.append(text)
        return True

    def send_chat_action(self, chat_id, action="typing"):
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
        self.now = [1000.0]
        self.bot = Bot(self.api, DemoAdapter(root / "work"), Store(root / "state"),
                       [OWNER], root / "media", clock=lambda: self.now[0])  # fmt: skip

    def quiet(self):
        self.now[0] += 10
        self.bot.tick()

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

    def cards(self, photos, brief):
        for n in range(1, photos + 1):
            self.bot.handle(photo(OWNER, n, group="g1"))
        self.bot.handle(private(OWNER, text=brief))
        self.quiet()
        self.bot.handle(tap(OWNER, "outline"))
        self.bot.handle(tap(OWNER, "plan"))
        return self.board()

    def board(self):
        return Storyboard.from_dict(self.bot.store.load(OWNER)["board"])

    def test_materials_then_outline_then_cards(self):
        for n in range(1, 5):
            self.bot.handle(photo(OWNER, n, group="g1"))
        self.assertEqual(sum(m["kind"] == "message" for m in self.api.sent), 1)
        self.bot.handle(private(OWNER, text="咖啡店开业"))
        self.bot.handle(private(OWNER, text="温馨一点"))
        self.assertIn("已收到 4 张", self.api.edits[-1])
        self.bot.tick()  # not quiet yet: nothing happens
        self.assertNotIn("素材整理好了", self.api.sent[-1]["text"])
        self.quiet()
        self.assertIn("素材整理好了", self.api.sent[-1]["text"])
        self.assertEqual(self.board().brief, "咖啡店开业；温馨一点")
        self.bot.handle(private(OWNER, text="3 是吧台"))
        self.assertEqual(self.board().materials[2], "吧台")
        self.bot.handle(tap(OWNER, "outline"))
        self.assertIn("故事大纲", self.api.sent[-1]["text"])
        self.bot.handle(private(OWNER, text="更热闹一点"))
        self.assertIn("更热闹一点", self.api.sent[-1]["text"])
        self.bot.handle(tap(OWNER, "plan"))
        board_msg = self.api.sent[-1]
        self.assertEqual(board_msg["kind"], "photo")
        self.assertIn("#5", board_msg["text"])
        self.assertIn(("2", "card:2"), board_msg["buttons"][1])

    def test_card_review_swap_edit_delete(self):
        self.cards(4, "咖啡店开业")
        self.bot.handle(tap(OWNER, "card:2"))
        self.assertIn("用：素材2「手冲」当开头画面", self.api.sent[-1]["text"])
        self.bot.handle(tap(OWNER, "swap:2"))
        self.assertIn(("4", "use:2:4"), self.api.sent[-1]["buttons"][0])
        self.bot.handle(tap(OWNER, "use:2:4"))
        self.assertEqual(self.board().cards[1].photo, 4)
        self.assertIn("店内", self.board().cards[1].prompt)
        self.bot.handle(tap(OWNER, "edit:2"))
        self.bot.handle(private(OWNER, text="咖啡师倒拉花，慢动作"))
        self.assertEqual(self.board().cards[1].description, "咖啡师倒拉花，慢动作")
        self.bot.handle(tap(OWNER, "prompt:2"))
        self.assertIn("倒拉花", self.api.sent[-1]["text"])
        self.bot.handle(tap(OWNER, "del:2"))
        self.assertEqual(len(self.board().cards), 4)

    def test_revision_numbers_refer_to_board_as_shown(self):
        self.cards(4, "咖啡店开业")
        self.bot.handle(private(OWNER, text="删掉第2张，第3张换成素材4，横屏"))
        board = self.board()
        self.assertEqual([c.photo for c in board.cards], [1, 4, 4, 1])
        self.assertEqual(board.cards[1].title, "招牌饮品上桌")
        self.assertEqual(board.aspect, "16:9")
        self.assertIn("删掉了第2张", self.api.sent[-1]["text"])

    def test_parsers(self):
        self.assertEqual(parse_second("第12秒人脸怪"), 12)
        self.assertEqual(parse_second("at 0:07 too dark"), 7)
        self.assertEqual(parse_second("3.5s"), 3.5)
        self.assertIsNone(parse_second("第3张太暗"))
        self.assertEqual(parse_card("第3张太暗"), 3)
        self.assertEqual(parse_card("card 2 brighter"), 2)
        adapter = DemoAdapter(self.tmp.name)
        board = adapter.plan_shots(adapter.start(1, ["a", "b", "c"], ""))
        self.assertEqual(board.locate(0), 1)
        self.assertEqual(board.locate(3.2), 2)
        self.assertIsNone(board.locate(board.duration))

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
    def test_generate_fix_review_keep_and_revert(self):
        self.cards(3, "花店母亲节")
        self.bot.handle(private(OWNER, text="短一点"))
        self.bot.handle(tap(OWNER, "gen"))
        self.assertIn("内容检查通过", self.api.sent[-1]["text"])
        self.assertIn("不会扣积分", self.api.sent[-1]["text"])
        self.bot.handle(tap(OWNER, "gen_ok"))
        self.assertEqual(self.api.sent[-1]["kind"], "video")
        first = self.board()
        clips = [c.take()["clip"] for c in first.cards]

        self.bot.handle(private(OWNER, text="第3秒太暗"))
        self.assertIn("第2张", self.api.sent[-1]["text"])
        self.bot.handle(tap(OWNER, "fix_yes"))
        self.assertIn("调亮画面", self.api.sent[-1]["text"])
        self.bot.handle(tap(OWNER, "fix_diff"))
        self.assertIn("改后", self.api.sent[-1]["text"])
        self.bot.handle(tap(OWNER, "fix_go"))
        fixed = self.board()
        self.assertEqual(fixed.cards[1].version, 2)
        self.assertEqual(fixed.cards[1].look["brightness"], 1.35)
        self.assertIn("提高曝光", fixed.cards[1].prompt)
        self.assertNotEqual(fixed.cards[1].take()["clip"], clips[1])
        self.assertEqual([c.take()["clip"] for c in fixed.cards[2:]], clips[2:])
        self.assertIn(("↩️ 换回 v1", "revert"), self.api.sent[-1]["buttons"][0])

        self.bot.handle(tap(OWNER, "revert"))
        reverted = self.board()
        self.assertEqual(reverted.cards[1].version, 1)
        self.assertEqual(reverted.cards[1].take()["clip"], clips[1])
        self.assertEqual(self.api.sent[-1]["kind"], "video")

        self.bot.handle(private(OWNER, text="第1张人脸怪"))
        self.bot.handle(tap(OWNER, "fix_roll"))
        rolled = self.board()
        self.assertEqual(rolled.cards[0].version, 2)
        self.assertEqual(rolled.cards[0].prompt, first.cards[0].prompt)

    def test_text_without_photos_waits_then_asks_for_photos(self):
        self.bot.handle(private(OWNER, text="想给花店做个片子"))
        self.assertEqual(len(self.api.sent), 1)  # one status message only
        self.quiet()
        self.assertIn("先发几张", self.api.sent[-1]["text"])
        self.assertEqual(self.bot.store.load(OWNER)["phase"], "collecting")

    def test_commands_help_go_status(self):
        self.bot.handle(private(OWNER, text="/help"))
        for name in ("/new", "/go", "/find", "/status", "/budget"):
            self.assertIn(name, self.api.sent[-1]["text"])
        for n in range(1, 3):
            self.bot.handle(photo(OWNER, n))
        self.bot.handle(private(OWNER, text="/go"))  # no need to wait
        self.assertIn("素材整理好了", self.api.sent[-1]["text"])
        self.bot.handle(private(OWNER, text="/go"))
        self.assertIn("故事大纲", self.api.sent[-1]["text"])
        self.bot.handle(private(OWNER, text="/go"))
        self.assertEqual(self.bot.store.load(OWNER)["phase"], "proposed")
        self.bot.handle(private(OWNER, text="/go"))
        self.bot.handle(private(OWNER, text="/go"))
        # /go never spends: it only asks for the confirm button again.
        self.assertEqual(self.bot.store.load(OWNER)["phase"], "confirm")
        self.assertIn(("✅ 确认生成", "gen_ok"), self.api.sent[-1]["buttons"][0])
        self.bot.handle(private(OWNER, text="/status"))
        self.assertIn("等你确认生成", self.api.sent[-1]["text"])
        self.bot.handle(private(OWNER, text="/nope"))
        self.assertIn("/help", self.api.sent[-1]["text"])

    def test_new_archives_and_find_reopens(self):
        first = self.cards(2, "咖啡店开业")
        self.bot.handle(private(OWNER, text="/new"))
        self.assertIn("已存档", self.api.sent[-1]["text"])
        self.assertEqual(self.bot.store.load(OWNER)["phase"], "collecting")
        self.bot.handle(private(OWNER, text="/find"))
        self.assertEqual(self.api.sent[-1]["buttons"][0][0][1], f"open:{first.id}")
        self.bot.handle(tap(OWNER, f"open:{first.id}"))
        self.assertEqual(self.board().id, first.id)
        self.assertEqual(self.bot.store.load(OWNER)["phase"], "proposed")
        self.assertIn("找回了", self.api.sent[-1]["text"])

    def test_stale_button_does_nothing_dangerous(self):
        for data in ("gen_ok", "fix_go", "revert", "use:1:1", "card:1"):
            self.bot.handle(tap(OWNER, data))
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
