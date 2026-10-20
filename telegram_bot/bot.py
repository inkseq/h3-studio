"""Conversation flow for the promo-film bot (#382).

Only allowlisted users in private chats are answered; every other update is
dropped without a reply. The bot never starts a conversation by itself.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time

from .adapter import Film, Storyboard
from .render import clock
from .telegram_api import TelegramError

log = logging.getLogger("telegram_bot")

WELCOME = """你好！我帮小店做宣传短片。

1. 发几张店里的照片（门头、产品、环境、人都行）
2. 说一句想做什么，比如「咖啡店开业，温馨一点，30秒」
3. 我排好故事板发给你，满意就点「开始生成」
4. 片子做好后，说「第12秒那里人脸怪」我就只重做那一段

现在是演示模式：不扣积分、不用显卡，片子是照片做的示意。
随时发 /new 重新开始。"""
NEED_PHOTOS = "先发几张店里的照片吧：门头、产品、环境、人都可以。"
BOARD_BUTTONS = [[("🎬 开始生成", "gen"), ("✏️ 改一下", "revise")], [("🆕 重新开始", "new")]]
SECOND_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:秒|s\b|sec)|(\d+):(\d{2})", re.I)
CARD_RE = re.compile(r"第\s*(\d+)\s*张|#\s*(\d+)|card\s*(\d+)", re.I)


def fresh():
    return {"phase": "collecting", "photos": [], "brief": "", "board": None,
            "film": None, "pending_fix": None}  # fmt: skip


class Store:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def _path(self, chat_id):
        return self.folder / f"chat-{int(chat_id)}.json"

    def load(self, chat_id):
        path = self._path(chat_id)
        if not path.exists():
            return fresh()
        return {**fresh(), **json.loads(path.read_text(encoding="utf-8"))}

    def save(self, chat_id, state):
        path = self._path(chat_id)
        fd, tmp = tempfile.mkstemp(dir=self.folder, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def offset(self):
        path = self.folder / "offset.json"
        return json.loads(path.read_text())["offset"] if path.exists() else None

    def save_offset(self, value):
        (self.folder / "offset.json").write_text(json.dumps({"offset": value}))


def timeline_text(board):
    lines = [f"{board.title} · {clock(board.duration)}"]
    for number, card, start, end in board.timeline():
        lines.append(f"#{number}  {clock(start)}–{clock(end)}  {card.title}")
    return "\n".join(lines)


def parse_second(text):
    match = SECOND_RE.search(text)
    if not match:
        return None
    if match.group(1):
        return float(match.group(1))
    return int(match.group(2)) * 60 + int(match.group(3))


def parse_card(text):
    match = CARD_RE.search(text)
    if not match:
        return None
    return int(next(g for g in match.groups() if g))


class Bot:
    def __init__(self, api, adapter, store, allowed_ids, media_dir):
        if not allowed_ids:
            raise ValueError("allowlist is empty; refusing to run")
        self.api, self.adapter, self.store = api, adapter, store
        self.allowed = {int(x) for x in allowed_ids}
        self.media_dir = Path(media_dir)
        self.ignored = 0
        self._last_group = None

    # ---- gate -------------------------------------------------------------

    def _allowed(self, user, chat):
        return (
            isinstance(user, dict)
            and isinstance(chat, dict)
            and chat.get("type") == "private"
            and user.get("id") in self.allowed
            and chat.get("id") == user.get("id")
            and not user.get("is_bot")
        )

    def handle(self, update):
        if "callback_query" in update:
            query = update["callback_query"]
            chat = (query.get("message") or {}).get("chat")
            if not self._allowed(query.get("from"), chat):
                self.ignored += 1
                return
            return self._callback(chat["id"], query)
        message = update.get("message")
        if not message or not self._allowed(message.get("from"), message.get("chat")):
            self.ignored += 1
            return
        chat_id = message["chat"]["id"]
        if message.get("photo"):
            return self._photo(chat_id, message)
        text = (message.get("text") or "").strip()
        if text:
            return self._text(chat_id, text)
        self.api.send_message(chat_id, "目前支持照片和文字消息。")

    # ---- inputs -----------------------------------------------------------

    def _photo(self, chat_id, message):
        state = self.store.load(chat_id)
        folder = self.media_dir / str(chat_id)
        folder.mkdir(parents=True, exist_ok=True)
        best = max(message["photo"], key=lambda p: p.get("file_size", 0))
        number = len(state["photos"]) + 1
        dest = folder / f"photo{number:02d}-{best['file_unique_id']}.jpg"
        self.api.download(best["file_id"], dest)
        state["photos"].append(str(dest))
        caption = (message.get("caption") or "").strip()
        if caption:
            state["brief"] = caption
        self.store.save(chat_id, state)
        group = message.get("media_group_id")
        if group and group == self._last_group:
            return
        self._last_group = group
        if state["board"]:
            self.api.send_message(
                chat_id,
                f"已加入照片{number}。说「第3张换成照片{number}」就能用上。",
            )
        else:
            self.api.send_message(
                chat_id,
                "📷 收到照片。继续发，或者说一句想做什么；都发完了也可以直接点按钮。",
                [[("📋 排故事板", "draft")]],
            )

    def _text(self, chat_id, text):
        if text in ("/start", "/help"):
            self.api.send_message(chat_id, WELCOME)
            return
        if text == "/new":
            self.store.save(chat_id, fresh())
            self.api.send_message(chat_id, "好，重新开始。" + NEED_PHOTOS)
            return
        state = self.store.load(chat_id)
        phase = state["phase"]
        if phase == "collecting":
            state["brief"] = text
            self.store.save(chat_id, state)
            return self._draft(chat_id, state)
        if phase in ("proposed", "confirm"):
            return self._revise(chat_id, state, text)
        if phase == "delivered":
            return self._feedback(chat_id, state, text)
        self.api.send_message(chat_id, "还在生成，稍等一下～")

    def _callback(self, chat_id, query):
        data = query.get("data")
        self.api.answer_callback(query["id"])
        state = self.store.load(chat_id)
        if data == "new":
            self.store.save(chat_id, fresh())
            self.api.send_message(chat_id, "好，重新开始。" + NEED_PHOTOS)
        elif data == "draft" and state["phase"] == "collecting":
            self._draft(chat_id, state)
        elif data == "revise" and state["board"]:
            self.api.send_message(
                chat_id,
                "直接告诉我改什么，比如：\n· 第3张换成照片5\n· 删掉第4张\n· 短一点 / 横屏\n· 第2张要更温馨",
            )
        elif data == "gen" and state["phase"] in ("proposed", "confirm"):
            board = Storyboard.from_dict(state["board"])
            estimate = self.adapter.estimate(board)
            state["phase"] = "confirm"
            self.store.save(chat_id, state)
            self.api.send_message(
                chat_id,
                f"生成 {clock(estimate.seconds)} 的片子，约 {estimate.credits} 积分。\n{estimate.note}",
                [[("✅ 确认生成", "gen_ok"), ("✏️ 再改改", "revise")]],
            )
        elif data == "gen_ok" and state["phase"] == "confirm":
            self._generate(chat_id, state)
        elif data == "fix_ok" and state["pending_fix"]:
            self._regenerate(chat_id, state)
        elif data == "fix_no" and state["pending_fix"]:
            state["pending_fix"] = None
            self.store.save(chat_id, state)
            self.api.send_message(chat_id, "那是哪一张？直接说「第N张……」。")
        else:
            self.api.send_message(chat_id, "这个按钮已经过期了，按最新的消息操作就好。")

    # ---- steps ------------------------------------------------------------

    def _draft(self, chat_id, state):
        if not state["photos"]:
            self.api.send_message(chat_id, NEED_PHOTOS)
            return
        self.api.send_message(chat_id, "📋 正在排故事板……")
        board = self.adapter.draft(chat_id, state["photos"], state["brief"])
        state.update(phase="proposed", board=board.to_dict(), film=None)
        self.store.save(chat_id, state)
        self._send_board(chat_id, board, "故事板排好了，看看行不行：")

    def _revise(self, chat_id, state, text):
        board = Storyboard.from_dict(state["board"])
        board, summary = self.adapter.revise(board, text)
        state.update(phase="proposed", board=board.to_dict())
        self.store.save(chat_id, state)
        self._send_board(chat_id, board, f"改好了（{summary}）：")

    def _send_board(self, chat_id, board, lead):
        self.api.send_photo(
            chat_id,
            self.adapter.grid(board),
            f"{lead}\n\n{timeline_text(board)}",
            BOARD_BUTTONS,
        )

    def _progress_message(self, chat_id, text):
        message = self.api.send_message(chat_id, text)
        last = [0.0]

        def progress(done, total):
            now = time.monotonic()
            if done not in (0, total) and now - last[0] < 1.5:
                return
            last[0] = now
            try:
                self.api.edit_message_text(
                    chat_id, message["message_id"], f"{text} {done}/{total}"
                )
            except TelegramError:
                pass  # "message is not modified" and similar are harmless

        return progress

    def _generate(self, chat_id, state):
        board = Storyboard.from_dict(state["board"])
        state["phase"] = "generating"
        self.store.save(chat_id, state)
        progress = self._progress_message(chat_id, "🎬 生成中……")
        try:
            film = self.adapter.generate(chat_id, board, progress)
        except Exception:
            log.exception("demo generate failed")
            state["phase"] = "confirm"
            self.store.save(chat_id, state)
            self.api.send_message(chat_id, "生成失败了，可以再点一次「确认生成」。",
                                  [[("✅ 确认生成", "gen_ok")]])  # fmt: skip
            return
        state.update(phase="delivered", film=film.to_dict())
        self.store.save(chat_id, state)
        self._send_film(chat_id, board, film, "🎉 片子好了！")

    def _send_film(self, chat_id, board, film, lead):
        self.api.send_video(
            chat_id,
            film.path,
            f"{lead}\n\n{timeline_text(board)}\n\n哪里不满意就说「第12秒……」或「第3张……」，我只重做那一段。",
            [[("🆕 做新的", "new")]],
        )

    def _feedback(self, chat_id, state, text):
        board = Storyboard.from_dict(state["board"])
        film = Film.from_dict(state["film"])
        second = parse_second(text)
        number = parse_card(text)
        if second is not None:
            number = board.locate(second)
            if number is None:
                self.api.send_message(
                    chat_id, f"片子只有 {clock(board.duration)}，第{second:g}秒不在里面。"
                )
                return
            state["pending_fix"] = {"number": number, "note": text}
            self.store.save(chat_id, state)
            self.api.send_photo(
                chat_id,
                self.adapter.frame(film, second),
                f"第{second:g}秒是第{number}张「{board.cards[number - 1].title}」，是这一段吗？",
                [[("✅ 是，重做这张", "fix_ok"), ("❌ 不是", "fix_no")]],
            )
            return
        if number is not None and 1 <= number <= len(board.cards):
            state["pending_fix"] = {"number": number, "note": text}
            self.store.save(chat_id, state)
            self.api.send_message(
                chat_id,
                f"重做第{number}张「{board.cards[number - 1].title}」？",
                [[("✅ 重做这张", "fix_ok"), ("❌ 算了", "fix_no")]],
            )
            return
        self.api.send_message(
            chat_id, "想改哪里？说「第12秒……」或「第3张……」。要做新的片子就发 /new。"
        )

    def _regenerate(self, chat_id, state):
        board = Storyboard.from_dict(state["board"])
        film = Film.from_dict(state["film"])
        fix = state["pending_fix"]
        progress = self._progress_message(chat_id, f"🔁 重做第{fix['number']}张……")
        board, film = self.adapter.regenerate_card(
            chat_id, board, film, fix["number"], fix["note"], progress
        )
        state.update(board=board.to_dict(), film=film.to_dict(), pending_fix=None)
        self.store.save(chat_id, state)
        self._send_film(chat_id, board, film, f"第{fix['number']}张重做好了，重新剪了一版：")

    # ---- loop -------------------------------------------------------------

    def poll_forever(self):
        offset = self.store.offset()
        while True:
            try:
                updates = self.api.get_updates(offset)
            except TelegramError as error:
                log.warning("poll failed: %s", error)
                time.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                self.store.save_offset(offset)
                try:
                    self.handle(update)
                except Exception:
                    log.exception("update %s failed", update["update_id"])
