"""Conversation flow for the promo-film bot (#382).

Written for a busy shop owner: send photos and one sentence, get one proposal
(storyboard grid) with two main buttons, generate, then fix any moment with a
tap or a sentence. Photos are always "照片N" and the film's parts "第N段".
Details (each part, photo names) are there for whoever wants them.

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

from .adapter import MODES, Film, Storyboard
from .render import clock
from .telegram_api import TelegramError

log = logging.getLogger("telegram_bot")


class GenerationHeld(Exception):
    """Raised by an adapter that deliberately does not submit paid generation."""


WELCOME = """你好！我帮小店把照片做成宣传短片。

📷 发几张店里的照片，再说一句想做什么，比如「咖啡店开业」。
其余的我来排，你看了满意再生成。

（演示模式：不扣积分。发 /help 看更多）"""
COMMANDS = [
    ("new", "做一条新片子（当前的会存档）"),
    ("go", "继续下一步"),
    ("find", "找以前做过的片子"),
    ("status", "现在做到哪一步"),
    ("budget", "积分和花费"),
    ("help", "怎么用"),
]
HELP = (
    "直接聊天就行：发照片、说想要什么、说哪里要改。\n"
    "要花积分或做决定时，我会给按钮让你点。\n\n"
    "改方案可以这样说：\n· 第2段用照片3\n· 删掉第4段\n· 短一点 / 横屏\n· 3 是吧台（改照片名字）\n\n"
    + "\n".join(f"/{name}  {text}" for name, text in COMMANDS)
    + "\n\n/go 碰到要花积分的步骤，也会先让你确认。"
)
PHASES = {
    "collecting": "收照片",
    "proposed": "看方案",
    "confirm": "等你确认生成",
    "generating": "生成中",
    "delivered": "片子做好了",
}
BRIEFS = ["开业宣传", "新品上市", "节日促销", "日常宣传"]
BRIEF_ICONS = ["🎉", "🆕", "🎁", "☕"]
# Quick feedback after the film: (button, words handed to the fix proposal)
ISSUES = {"dark": ("🔆 太暗", "画面太暗"), "slow": ("🐢 太慢", "节奏太慢"),
          "face": ("🙂 人物怪", "人物变形")}  # fmt: skip
QUIET_SECONDS = 3.5  # wait this long after the last photo/text before acting
MIN_PHOTOS = 3  # fewer than this: ask for more instead of starting on our own
HISTORY_LIMIT = 10
NEED_PHOTOS = "先发几张店里的照片吧：门头、产品、环境、人都可以。"
SECOND_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:秒|s\b|sec)|(\d+):(\d{2})", re.I)
PART_RE = re.compile(r"第\s*(\d+)\s*[段张]|#\s*(\d+)|card\s*(\d+)", re.I)
RENAME_RE = re.compile(r"^(?:照片|素材)?\s*(\d+)\s*(?:号)?\s*(?:是|叫|改成|改叫|=|:|：)\s*(.+)$")


def fresh():
    return {"phase": "collecting", "photos": [], "brief": "", "board": None,
            "film": None, "pending_fix": None, "pending_input": None,
            "last_fix": None, "status_msg": None, "picks_msg": None,
            "last_input": None}  # fmt: skip


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
        state = {**fresh(), **json.loads(path.read_text(encoding="utf-8"))}
        if state["phase"] not in PHASES:  # older flow's intermediate steps
            state["phase"] = "proposed" if state["board"] else "collecting"
        return state

    def _write(self, path, value):
        fd, tmp = tempfile.mkstemp(dir=self.folder, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def save(self, chat_id, state):
        self._write(self._path(chat_id), state)

    def _history_path(self, chat_id):
        return self.folder / f"chat-{int(chat_id)}-history.json"

    def history(self, chat_id):
        path = self._history_path(chat_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def archive(self, chat_id, state, when):
        """Keep a finished or abandoned film so /find can reopen it."""
        if not state.get("board"):
            return
        board = state["board"]
        entries = [e for e in self.history(chat_id) if e["id"] != board["id"]]
        entries.insert(0, {"id": board["id"], "title": board["title"], "when": when,
                           "phase": state["phase"], "state": state})  # fmt: skip
        self._write(self._history_path(chat_id), entries[:HISTORY_LIMIT])

    def offset(self):
        path = self.folder / "offset.json"
        return json.loads(path.read_text())["offset"] if path.exists() else None

    def save_offset(self, value):
        (self.folder / "offset.json").write_text(json.dumps({"offset": value}))


def orientation(board):
    return "竖屏" if board.aspect == "9:16" else "横屏"


def timeline_text(board, photos=True):
    """Proposal lists which photo each part uses; the film caption stays short
    enough not to wrap on a phone."""
    lines = [f"{board.title} · {clock(board.duration)} · {orientation(board)}"]
    for number, card, start, end in board.timeline():
        if photos:
            lines.append(f"第{number}段  {clock(start)}–{clock(end)}  {card.title}（照片{card.photo}）")
        else:
            lines.append(f"{clock(start)}  第{number}段 {card.title}")
    return "\n".join(lines)


def part_text(board, number):
    card = board.cards[number - 1]
    _n, _c, start, end = board.timeline()[number - 1]
    return (
        f"第{number}段 · {clock(start)}–{clock(end)} · {card.title}\n"
        f"用：{board.material(card.photo)}{MODES[card.mode]}\n"
        f"画面：{card.description}\n"
        f"{card.seconds:g} 秒 · {board.tier}"
    )


def parse_second(text):
    match = SECOND_RE.search(text)
    if not match:
        return None
    if match.group(1):
        return float(match.group(1))
    return int(match.group(2)) * 60 + int(match.group(3))


def parse_part(text):
    match = PART_RE.search(text)
    if not match:
        return None
    return int(next(g for g in match.groups() if g))


def number_rows(prefix, count, per_row=6, label="{}"):
    buttons = [(label.format(i), f"{prefix}{i}") for i in range(1, count + 1)]
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


class Bot:
    def __init__(self, api, adapter, store, allowed_ids, media_dir, clock=time.time):
        if not allowed_ids:
            raise ValueError("allowlist is empty; refusing to run")
        self.api, self.adapter, self.store = api, adapter, store
        self.allowed = {int(x) for x in allowed_ids}
        self.media_dir = Path(media_dir)
        self.clock = clock
        self.ignored = 0
        self._waiting = set()  # chats whose photos/text may still be arriving

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
            try:
                return self._callback(chat["id"], query)
            except Exception as error:
                self._failed(chat["id"], error)
                raise
        message = update.get("message")
        if not message or not self._allowed(message.get("from"), message.get("chat")):
            self.ignored += 1
            return
        chat_id = message["chat"]["id"]
        try:
            image = self._image_of(message)
            if image:
                return self._photo(chat_id, message, image)
            text = (message.get("text") or "").strip()
            if text:
                return self._text(chat_id, text)
            self._unsupported(chat_id, message)
        except Exception as error:
            self._failed(chat_id, error)
            raise

    @staticmethod
    def _image_of(message):
        if message.get("photo"):
            return max(message["photo"], key=lambda p: p.get("file_size", 0))
        document = message.get("document") or {}
        if str(document.get("mime_type", "")).startswith("image/"):
            return document  # photo sent "as file": keeps full quality
        return None

    def _unsupported(self, chat_id, message):
        if message.get("voice") or message.get("audio"):
            text = "语音我暂时还听不懂，打几个字告诉我就行 🙏"
        elif message.get("video") or message.get("video_note"):
            text = "视频素材过阵子支持，现在请发照片。"
        elif message.get("sticker"):
            text = "😄 收到！发照片或者说一句想做什么吧。"
        else:
            text = "我现在只认照片和文字。"
        self.api.send_message(chat_id, text)

    # ---- helpers ----------------------------------------------------------

    def _board(self, state):
        return Storyboard.from_dict(state["board"])

    def _save_board(self, chat_id, state, board, **changes):
        state.update(board=board.to_dict(), **changes)
        self.store.save(chat_id, state)

    def _action(self, chat_id, action):
        """Show "typing…"/"sending video…" while working; purely cosmetic."""
        try:
            self.api.send_chat_action(chat_id, action)
        except (TelegramError, AttributeError):
            pass

    def _send_proposal(self, chat_id, board, lead):
        rows = [
            [("🎬 开始生成", "gen"), ("✏️ 改一下", "revise")],
            [("🔍 看每一段", "parts"), ("📷 照片名字", "photos")],
            [("📝 看每段提示词", "prompts")],
        ]
        link = self.adapter.web_link(board)
        if link:
            rows.append([("🌐 在网页里打开", "url:" + link)])
        rows.append([("🆕 重新开始", "new")])
        self.api.send_photo(
            chat_id,
            self.adapter.grid(board),
            f"{lead}\n\n{timeline_text(board)}\n\n满意就点「开始生成」；想改直接说，比如「第2段用照片3」「短一点」。",
            rows,
        )

    def _send_prompts(self, chat_id, board):
        """Every part's full generation description in one message."""
        blocks = [
            f"第{n}段 {clock(start)}–{clock(end)} · {card.seconds:g}秒 · 照片{card.photo}{MODES[card.mode]}\n{card.prompt}"
            for n, card, start, end in board.timeline()
        ]
        self.api.send_message(
            chat_id,
            "📝 每一段的提示词（生成时就按这个来）：\n\n" + "\n\n".join(blocks)
            + "\n\n想改哪段就说，比如「第2段改成咖啡师倒拉花」。",
            [[("⬅️ 回到方案", "board")]],
        )

    def _send_photos(self, chat_id, board, lead="📷 我认出了这些照片："):
        self.api.send_photo(
            chat_id,
            self.adapter.materials_sheet(board),
            f"{lead}\n名字不对直接说「3 是吧台」。",
            [[("⬅️ 回到方案", "board")]],
        )

    def _send_part(self, chat_id, board, number):
        self.api.send_photo(
            chat_id,
            self.adapter.card_preview(board, number),
            part_text(board, number),
            [
                [("🖼 换照片", f"swap:{number}"), ("✏️ 改画面", f"edit:{number}")],
                [("🗑 删掉这段", f"del:{number}"), ("📝 完整描述", f"prompt:{number}")],
                [("⬅️ 回到方案", "board")],
            ],
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
                    chat_id, message["message_id"], f"{text}\n进度 {done}/{total}"
                )
            except TelegramError:
                pass  # "message is not modified" and similar are harmless

        return progress

    def _failed(self, chat_id, error):
        detail = getattr(error, "step", None)
        where = f"（{detail}）" if detail else ""
        try:
            self.api.send_message(
                chat_id, f"出错了{where}，已经停下，没有扣费。可以再试一次，或发 /status 看看。"
            )
        except Exception:
            pass

    def _stale(self, chat_id):
        self.api.send_message(chat_id, "这个按钮已经过期了，按最新的消息操作就好。")

    # ---- inputs -----------------------------------------------------------

    def _photo(self, chat_id, message, image):
        state = self.store.load(chat_id)
        if state["phase"] in ("generating", "delivered", "confirm"):
            self.api.send_message(chat_id, "这条片子已经在生成或做好了。用新照片做新的一条，先发 /new。")
            return
        folder = self.media_dir / str(chat_id)
        folder.mkdir(parents=True, exist_ok=True)
        number = len(state["photos"]) + 1
        dest = folder / f"photo{number:02d}-{image['file_unique_id']}.jpg"
        self.api.download(image["file_id"], dest)
        state["photos"].append(str(dest))
        caption = (message.get("caption") or "").strip()
        if caption:
            state["brief"] = f"{state['brief']}；{caption}" if state["brief"] else caption
        if state["board"]:
            board = self._board(state)
            board.photos.append(str(dest))
            board.materials.append(f"照片{number}")
            state["board"] = self.adapter.add_photo(board, str(dest)).to_dict()
            self.store.save(chat_id, state)
            self.api.send_message(
                chat_id, f"加进来了，这是照片{number}。说「第2段用照片{number}」就能用上。"
            )
            return
        self._collecting(chat_id, state)

    def _text(self, chat_id, text):
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command == "/start":
            self.api.send_message(chat_id, WELCOME)
            return
        if command == "/help":
            self.api.send_message(chat_id, HELP)
            return
        if command == "/new":
            return self._new(chat_id)
        state = self.store.load(chat_id)
        if command:
            return self._command(chat_id, state, command)
        pending = state.get("pending_input")
        if pending:
            state["pending_input"] = None
            if pending["kind"] == "edit" and state["phase"] == "proposed":
                board = self.adapter.edit_card(self._board(state), pending["n"], text)
                self._save_board(chat_id, state, board)
                self._send_part(chat_id, board, pending["n"])
                return
            if pending["kind"] == "fix" and state["phase"] == "delivered":
                return self._propose_fix(chat_id, state, pending["n"], text)
        phase = state["phase"]
        if phase == "collecting":
            state["brief"] = f"{state['brief']}；{text}" if state["brief"] else text
            return self._collecting(chat_id, state)
        if phase in ("proposed", "confirm"):
            match = RENAME_RE.match(text)
            board = self._board(state)
            if match and 1 <= int(match.group(1)) <= len(board.materials):
                board = self.adapter.rename_material(board, int(match.group(1)), match.group(2))
                self._save_board(chat_id, state, board, phase="proposed")
                self._send_photos(chat_id, board, f"改好了：照片{match.group(1)} 是「{match.group(2).strip()}」。")
                return
            self._action(chat_id, "typing")
            board, summary = self.adapter.revise(board, text)
            self._save_board(chat_id, state, board, phase="proposed")
            self._send_proposal(chat_id, board, f"改好了（{summary}）：")
            return
        if phase == "delivered":
            return self._feedback(chat_id, state, text)
        self.api.send_message(chat_id, "还在生成，稍等一下～")

    def _callback(self, chat_id, query):
        data = query.get("data") or ""
        self.api.answer_callback(query["id"])
        state = self.store.load(chat_id)
        phase, has_board = state["phase"], bool(state["board"])
        name, _, arg = data.partition(":")
        n = int(arg) if arg.isdigit() else None
        board = self._board(state) if has_board else None
        parts = len(board.cards) if board else 0
        editing = phase == "proposed"

        if name == "new":
            self._new(chat_id)
        elif name == "open":
            self._open(chat_id, state, arg)
        elif name == "start" and phase == "collecting":
            if state["photos"] and not state["brief"]:
                self._ask_brief(chat_id, state)
            else:
                self._start(chat_id, state)
        elif name == "more" and phase == "collecting":
            self._close(chat_id, state.get("picks_msg"), "好，等你再发几张。")
            state["picks_msg"] = None
            self.store.save(chat_id, state)
        elif name == "prompts" and phase in ("proposed", "confirm", "delivered"):
            self._send_prompts(chat_id, board)
        elif name == "brief" and phase == "collecting" and n and n <= len(BRIEFS):
            state["brief"] = BRIEFS[n - 1]
            self._start(chat_id, state)
        elif name == "board" and phase in ("proposed", "confirm"):
            self._send_proposal(chat_id, board, "当前方案：")
        elif name == "parts" and editing:
            self.api.send_message(
                chat_id, "看哪一段？", number_rows("card:", parts, 4, "第{}段")
            )
        elif name == "photos" and editing:
            self._send_photos(chat_id, board)
        elif name == "card" and editing and n and n <= parts:
            self._send_part(chat_id, board, n)
        elif name == "swap" and editing and n and n <= parts:
            self.api.send_photo(
                chat_id, self.adapter.materials_sheet(board), f"第{n}段换成哪张照片？",
                number_rows(f"use:{n}:", len(board.photos), 6, "照片{}")
                + [[("⬅️ 不换了", f"card:{n}")]],
            )  # fmt: skip
        elif name == "use" and editing:
            part, _, photo = arg.partition(":")
            if part.isdigit() and photo.isdigit() and 1 <= int(part) <= parts \
                    and 1 <= int(photo) <= len(board.photos):  # fmt: skip
                board = self.adapter.swap_material(board, int(part), int(photo))
                self._save_board(chat_id, state, board)
                self._send_part(chat_id, board, int(part))
            else:
                self._stale(chat_id)
        elif name == "edit" and editing and n and n <= parts:
            state["pending_input"] = {"kind": "edit", "n": n}
            self.store.save(chat_id, state)
            self.api.send_message(chat_id, f"第{n}段想拍成什么样？直接描述，比如「咖啡师倒拉花，慢动作」。")
        elif name == "del" and editing and n and n <= parts:
            board = self.adapter.delete_card(board, n)
            self._save_board(chat_id, state, board)
            self._send_proposal(chat_id, board, f"删掉了第{n}段：")
        elif name == "prompt" and phase in ("proposed", "delivered") and n and n <= parts:
            self.api.send_message(chat_id, f"第{n}段的完整描述（给生成用的）：\n\n{board.cards[n - 1].prompt}")
        elif name == "revise" and phase in ("proposed", "confirm"):
            self.api.send_message(chat_id, "直接说要改什么，比如：\n· 第2段用照片3\n· 删掉第4段\n· 短一点 / 横屏\n· 更热闹一点")
        elif name == "gen" and phase in ("proposed", "confirm"):
            self._ask_generate(chat_id, state)
        elif name == "gen_ok" and phase == "confirm":
            self._generate(chat_id, state)
        elif name == "ok" and phase == "delivered":
            self.store.archive(chat_id, state, time.strftime("%m-%d %H:%M"))
            self.api.send_message(chat_id, "👍 好！片子已经保存，/find 随时能找回。长按视频就能转发。\n做下一条发 /new。")
        elif name == "q" and phase == "delivered" and arg in ISSUES:
            label, _words = ISSUES[arg]
            self.api.send_message(
                chat_id, f"{label[2:]}的是哪一段？（也可以直接说「第8秒{label[2:]}」）",
                number_rows(f"qf:{arg}:", parts, 4, "第{}段"),
            )  # fmt: skip
        elif name == "qf" and phase == "delivered":
            issue, _, part = arg.partition(":")
            if issue in ISSUES and part.isdigit() and 1 <= int(part) <= parts:
                self._propose_fix(chat_id, state, int(part), ISSUES[issue][1])
            else:
                self._stale(chat_id)
        elif name == "long" and phase == "delivered":
            self.api.send_message(
                chat_id, "删掉哪一段就能变短（不用重新生成）：",
                number_rows("cut:", parts, 4, "删第{}段"),
            )  # fmt: skip
        elif name == "cut" and phase == "delivered" and n and n <= parts and parts > 1:
            self._cut_part(chat_id, state, n)
        elif name == "other" and phase == "delivered":
            self.api.send_message(chat_id, "说说哪里不满意，比如「第8秒人脸怪」或「第2段太暗」。")
        elif name == "fix_yes" and phase == "delivered" and state["pending_fix"]:
            fix = state["pending_fix"]
            self._propose_fix(chat_id, state, fix["number"], fix["feedback"])
        elif name == "fix_no" and phase == "delivered" and state["pending_fix"]:
            state["pending_fix"] = None
            self.store.save(chat_id, state)
            self.api.send_message(chat_id, "那是哪一段？", number_rows("pick:", parts, 4, "第{}段"))
        elif name == "pick" and phase == "delivered" and n and n <= parts:
            self._propose_fix(chat_id, state, n, (state.get("pending_fix") or {}).get("feedback") or "按反馈调整")
        elif name in ("fix_go", "fix_roll") and phase == "delivered" \
                and (state["pending_fix"] or {}).get("proposal"):  # fmt: skip
            self._apply_fix(chat_id, state, reroll=name == "fix_roll")
        elif name == "fix_own" and phase == "delivered" and state["pending_fix"]:
            n = state["pending_fix"]["number"]
            state["pending_input"] = {"kind": "fix", "n": n}
            self.store.save(chat_id, state)
            self.api.send_message(chat_id, f"第{n}段你想怎么改？说具体一点，比如「换成窗边的光，人物别出现」。")
        elif name == "fix_diff" and phase == "delivered" and (state["pending_fix"] or {}).get("proposal"):
            proposal = state["pending_fix"]["proposal"]
            self.api.send_message(
                chat_id, f"改前：\n{proposal['prompt_before']}\n\n改后：\n{proposal['prompt_after']}"
            )
        elif name == "keep" and phase == "delivered" and state["last_fix"]:
            state["last_fix"] = None
            self.store.save(chat_id, state)
            self.api.send_message(chat_id, "好，保留新版。还有要改的继续说，或点「满意」。")
        elif name == "revert" and phase == "delivered" and state["last_fix"]:
            self._revert(chat_id, state)
        else:
            self._stale(chat_id)

    # ---- collecting ---------------------------------------------------------

    def _collecting(self, chat_id, state):
        """Photos and text keep arriving: keep one status message and wait."""
        state["last_input"] = self.clock()
        count = len(state["photos"])
        text = f"📷 收到 {count} 张照片" if count else "📝 记下了"
        if state["brief"]:
            text += f"\n想做：{state['brief'][:80]}"
        text += "\n\n继续发也行，停几秒我就开始排。" if count else "\n\n再发几张店里的照片吧。"
        buttons = [[("✅ 发完了，开始排", "start")]] if count else None
        message_id = state.get("status_msg")
        try:
            if not message_id:
                raise TelegramError("sendMessage", "no status message yet")
            self.api.edit_message_text(chat_id, message_id, text, buttons)
        except TelegramError:
            state["status_msg"] = self.api.send_message(chat_id, text, buttons)["message_id"]
        self.store.save(chat_id, state)
        self._waiting.add(chat_id)

    def tick(self):
        """Act on chats that went quiet; the poll loop calls this between polls."""
        for chat_id in list(self._waiting):
            state = self.store.load(chat_id)
            last = state.get("last_input")
            if state["phase"] != "collecting" or last is None:
                self._waiting.discard(chat_id)
                continue
            if self.clock() - last < QUIET_SECONDS:
                continue
            self._waiting.discard(chat_id)
            state["last_input"] = None
            self.store.save(chat_id, state)
            if len(state["photos"]) < MIN_PHOTOS and state["photos"]:
                # Too few photos to tell a story: ask once, never start on our own.
                count = len(state["photos"])
                sent = self.api.send_message(
                    chat_id,
                    f"现在只有 {count} 张照片，好几段会用同一张。\n"
                    "再发几张门头、产品、店里、人的照片，片子会好看很多。",
                    [[("📷 我再发几张", "more"), ("▶️ 就用这些", "start")]],
                )
                state["picks_msg"] = sent["message_id"]
                self.store.save(chat_id, state)
            elif state["photos"] and state["brief"]:
                self._start(chat_id, state)
            elif state["photos"]:
                self._ask_brief(chat_id, state)
            else:
                self.api.send_message(chat_id, NEED_PHOTOS)

    def _ask_brief(self, chat_id, state):
        self._close(chat_id, state.get("picks_msg"), "👌 好，就用这些照片。")
        buttons = [[(f"{BRIEF_ICONS[i]} {b}", f"brief:{i + 1}") for i, b in enumerate(BRIEFS[:2])],
                   [(f"{BRIEF_ICONS[i]} {b}", f"brief:{i + 1}") for i, b in enumerate(BRIEFS[2:], 2)]]  # fmt: skip
        sent = self.api.send_message(chat_id, "想做什么样的片子？点一个，或者直接说一句。", buttons)
        state["picks_msg"] = sent["message_id"]
        self.store.save(chat_id, state)

    def _start(self, chat_id, state):
        """Photos and a sentence become one complete proposal."""
        self._waiting.discard(chat_id)
        if not state["photos"]:
            self.api.send_message(chat_id, NEED_PHOTOS)
            return
        # Retire the waiting messages so their buttons can't be pressed again.
        count = len(state["photos"])
        self._close(chat_id, state.get("status_msg"), f"✅ 收到 {count} 张照片，开始排了……")
        if state["brief"]:
            self._close(chat_id, state.get("picks_msg"), f"✅ 要做：{state['brief'][:40]}")
        self._action(chat_id, "typing")
        board = self.adapter.start(chat_id, state["photos"], state["brief"])
        board = self.adapter.plan_shots(board)
        self._save_board(chat_id, state, board, phase="proposed", film=None,
                         last_input=None, status_msg=None, picks_msg=None)  # fmt: skip
        lead = "📋 方案排好了，看看行不行："
        if count < 3:
            lead += f"\n💡 只有 {count} 张照片，好几段会用同一张。多发几张门头、产品、人的照片，片子会更丰富。"
        self._send_proposal(chat_id, board, lead)

    def _close(self, chat_id, message_id, text):
        """Replace an earlier message's text and drop its buttons."""
        if not message_id:
            return
        try:
            self.api.edit_message_text(chat_id, message_id, text)
        except TelegramError:
            pass

    # ---- commands -----------------------------------------------------------

    def _new(self, chat_id):
        state = self.store.load(chat_id)
        self.store.archive(chat_id, state, time.strftime("%m-%d %H:%M"))
        self.store.save(chat_id, fresh())
        self._waiting.discard(chat_id)
        saved = "，刚才那条已存档（/find 能找回）" if state.get("board") else ""
        self.api.send_message(chat_id, f"好，做一条新的{saved}。\n{NEED_PHOTOS}")

    def _open(self, chat_id, state, film_id):
        entry = next((e for e in self.store.history(chat_id) if e["id"] == film_id), None)
        if entry is None:
            return self._stale(chat_id)
        if state.get("board") and state["board"]["id"] != film_id:
            self.store.archive(chat_id, state, time.strftime("%m-%d %H:%M"))
        restored = {**fresh(), **entry["state"], "pending_input": None,
                    "status_msg": None, "last_input": None}  # fmt: skip
        if restored["phase"] not in PHASES:
            restored["phase"] = "proposed" if restored["board"] else "collecting"
        if restored["phase"] == "generating":
            restored["phase"] = "confirm"
        self.store.save(chat_id, restored)
        board = self._board(restored)
        lead = f"找回了「{board.title}」："
        if restored["film"]:
            self._send_film(chat_id, board, Film.from_dict(restored["film"]), lead)
        else:
            self._send_proposal(chat_id, board, lead)

    def _command(self, chat_id, state, command):
        phase = state["phase"]
        if command == "/go":
            if phase == "collecting":
                return self._start(chat_id, state)
            if phase in ("proposed", "confirm"):
                return self._ask_generate(chat_id, state)  # never spends: asks to confirm
            if phase == "generating":
                return self.api.send_message(chat_id, "还在生成，稍等一下～")
            return self.api.send_message(
                chat_id, "片子已经做好了。要改就说「第8秒……」，要做新的发 /new。"
            )
        if command == "/status":
            lines = [f"现在：{PHASES[phase]}", f"照片：{len(state['photos'])} 张"]
            if state["board"]:
                board = self._board(state)
                lines.append(f"片子：{board.title}，{len(board.cards)} 段，{clock(board.duration)}")
            if state["brief"]:
                lines.append(f"想做：{state['brief'][:60]}")
            lines.append("发 /go 继续下一步。")
            return self.api.send_message(chat_id, "\n".join(lines))
        if command == "/find":
            entries = self.store.history(chat_id)
            if not entries:
                return self.api.send_message(chat_id, "还没有存档的片子。做完一条后发 /new，它就会存在这里。")
            rows = [[(f"{e['title']} · {e['when']}", f"open:{e['id']}")] for e in entries]
            return self.api.send_message(chat_id, "以前做过的片子，点一下接着改：", rows)
        if command == "/budget":
            return self.api.send_message(
                chat_id, "演示模式：不扣积分，也不会用显卡。\n接上正式账户后，这里显示剩余积分和最近的花费。"
            )
        self.api.send_message(chat_id, "没有这个命令。发 /help 看能用的命令。")

    # ---- generate and deliver -------------------------------------------------

    def _ask_generate(self, chat_id, state):
        board = self._board(state)
        estimate = self.adapter.estimate(board)
        state["phase"] = "confirm"
        self.store.save(chat_id, state)
        check = ("⚠️ 先看一下：\n" + "\n".join(f"· {c}" for c in estimate.checks)
                 if estimate.checks else "✅ 内容检查通过")  # fmt: skip
        self.api.send_message(
            chat_id,
            f"{check}\n生成 {clock(estimate.seconds)} 的片子，约 {estimate.credits} 积分，大约 1 分钟。\n{estimate.note}",
            [[("✅ 确认生成", "gen_ok"), ("✏️ 再改改", "revise")]],
        )

    def _generate(self, chat_id, state):
        board = self._board(state)
        state["phase"] = "generating"
        self.store.save(chat_id, state)
        self._action(chat_id, "upload_video")
        progress = self._progress_message(chat_id, "🎬 生成中，大约 1 分钟……")
        try:
            board, film = self.adapter.generate(chat_id, board, progress)
        except GenerationHeld:
            state["phase"] = "confirm"
            self.store.save(chat_id, state)
            link = self.adapter.web_link(board)
            self.api.send_message(
                chat_id,
                "⏸ 停在这里了：真的生成要开显卡，需要老板批准，这次测试不提交。\n"
                "方案已经保存在网站上，可以在网页里继续看和改。",
                [[("🌐 在网页里打开", "url:" + link)]] if link else None,
            )
            return
        except Exception:
            log.exception("generate failed")
            state["phase"] = "confirm"
            self.store.save(chat_id, state)
            self.api.send_message(chat_id, "生成失败了，没有扣费。可以再点一次「确认生成」。",
                                  [[("✅ 确认生成", "gen_ok")]])  # fmt: skip
            return
        self._save_board(chat_id, state, board, phase="delivered", film=film.to_dict())
        self._send_film(chat_id, board, film, "🎉 片子好了！")

    def _send_film(self, chat_id, board, film, lead, buttons=None):
        rows = buttons or [
            [("👍 满意，就这样", "ok")],
            [(ISSUES["dark"][0], "q:dark"), (ISSUES["slow"][0], "q:slow"), ("✂️ 太长", "long")],
            [(ISSUES["face"][0], "q:face"), ("✏️ 其他问题", "other")],
            [("🆕 做新的", "new")],
        ]
        self.api.send_video(
            chat_id,
            film.path,
            f"{lead}\n\n{timeline_text(board, photos=False)}\n\n哪里不满意点下面，或直接说「第8秒太暗」。",
            rows,
        )

    # ---- fixes ----------------------------------------------------------------

    def _feedback(self, chat_id, state, text):
        board = self._board(state)
        film = Film.from_dict(state["film"])
        second = parse_second(text)
        if second is not None:
            number = board.locate(second)
            if number is None:
                self.api.send_message(chat_id, f"片子只有 {clock(board.duration)}，第{second:g}秒不在里面。")
                return
            state["pending_fix"] = {"number": number, "feedback": text}
            self.store.save(chat_id, state)
            self.api.send_photo(
                chat_id,
                self.adapter.frame(film, second),
                f"第{second:g}秒是第{number}段「{board.cards[number - 1].title}」，是这一段吗？",
                [[("✅ 是这段", "fix_yes"), ("❌ 不是", "fix_no")]],
            )
            return
        number = parse_part(text)
        if number is not None and 1 <= number <= len(board.cards):
            return self._propose_fix(chat_id, state, number, text)
        state["pending_fix"] = {"number": None, "feedback": text}
        self.store.save(chat_id, state)
        self.api.send_message(
            chat_id, "是哪一段？点一下就行（或说「第8秒……」）。",
            number_rows("pick:", len(board.cards), 4, "第{}段"),
        )  # fmt: skip

    def _propose_fix(self, chat_id, state, number, feedback):
        board = self._board(state)
        proposal = self.adapter.propose_fix(board, number, feedback)
        state["pending_fix"] = {"number": number, "feedback": feedback,
                                "proposal": proposal.to_dict()}  # fmt: skip
        self.store.save(chat_id, state)
        card = board.cards[number - 1]
        self.api.send_message(
            chat_id,
            f"🛠 第{number}段「{card.title}」打算这样改：\n· {proposal.summary}\n"
            f"照片不变：{board.material(card.photo)}\n约 {proposal.credits} 积分（演示不扣）",
            [
                [("✅ 就这样改", "fix_go")],
                [("🎲 不改，再生成一次", "fix_roll"), ("✏️ 我自己说", "fix_own")],
                [("📝 改前改后对比", "fix_diff")],
            ],
        )

    def _apply_fix(self, chat_id, state, reroll):
        fix = state["pending_fix"]
        number = fix["number"]
        board = self._board(state)
        before = board.cards[number - 1].version
        prompt = None if reroll else fix["proposal"]["prompt_after"]
        self._action(chat_id, "upload_video")
        progress = self._progress_message(chat_id, f"🔁 重做第{number}段，大约 1 分钟……")
        board, film = self.adapter.apply_fix(board, number, prompt, progress)
        self._save_board(chat_id, state, board, film=film.to_dict(), pending_fix=None,
                         last_fix={"number": number, "previous": before})  # fmt: skip
        self._send_film(
            chat_id, board, film,
            f"第{number}段换成了新版，重新剪好了。",
            [[("👍 用新版", "keep"), ("↩️ 还是旧的好", "revert")],
             [("👍 满意，就这样", "ok"), ("🆕 做新的", "new")]],
        )  # fmt: skip

    def _revert(self, chat_id, state):
        last = state["last_fix"]
        board, film = self.adapter.adopt(self._board(state), last["number"], last["previous"])
        self._save_board(chat_id, state, board, film=film.to_dict(), last_fix=None)
        self._send_film(chat_id, board, film, f"第{last['number']}段换回了旧版，重新剪好了。")

    def _cut_part(self, chat_id, state, number):
        board = self.adapter.delete_card(self._board(state), number)
        board, film = self.adapter.recut(board)
        self._save_board(chat_id, state, board, film=film.to_dict(), last_fix=None)
        self._send_film(chat_id, board, film, f"删掉了第{number}段，重新剪好了（没有重新生成）。")

    # ---- loop -------------------------------------------------------------

    def poll_forever(self):
        offset = self.store.offset()
        while True:
            try:
                # Poll briefly while a chat is still sending, so it can go quiet.
                updates = self.api.get_updates(offset, timeout=1 if self._waiting else None)
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
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
