"""Conversation flow for the promo-film bot (#382).

Steps mirror the studio: materials → outline → shot cards (review each) →
content check and cost → generate → film → fix one card (proposal, confirm,
new take, keep or revert) → re-cut. Only allowlisted users in private chats
are answered; every other update is dropped without a reply. The bot never
starts a conversation by itself.
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


class GenerationHeld(Exception):
    """Raised by an adapter that deliberately does not submit paid generation."""

log = logging.getLogger("telegram_bot")

WELCOME = """你好！我帮小店做宣传短片。

1. 发几张店里的照片（门头、产品、环境、人都行）
2. 说一句想做什么，比如「咖啡店开业，温馨一点」
3. 我先整理素材、写个故事大纲，再排成镜头卡给你审
4. 满意就点「开始生成」；片子出来后说「第12秒太暗」，我只改那一段

现在是演示模式：不扣积分、不用显卡，片子是照片做的示意。
发 /help 看所有命令。"""
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
    + "\n".join(f"/{name}  {text}" for name, text in COMMANDS)
    + "\n\n/go 碰到要花积分的步骤，也会先让你确认。"
)
PHASES = {
    "collecting": "收照片",
    "materials": "整理素材",
    "outline": "写故事大纲",
    "proposed": "审镜头卡",
    "confirm": "等你确认生成",
    "generating": "生成中",
    "delivered": "片子做好了，可以按秒修改",
}
QUIET_SECONDS = 3.5  # wait this long after the last photo/text before acting
HISTORY_LIMIT = 10
NEED_PHOTOS = "先发几张店里的照片吧：门头、产品、环境、人都可以。"
SECOND_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:秒|s\b|sec)|(\d+):(\d{2})", re.I)
CARD_RE = re.compile(r"第\s*(\d+)\s*张|#\s*(\d+)|card\s*(\d+)", re.I)
RENAME_RE = re.compile(r"^(?:素材)?\s*(\d+)\s*(?:号)?\s*(?:是|叫|改成|改叫|=|:|：)\s*(.+)$")


def fresh():
    return {"phase": "collecting", "photos": [], "brief": "", "board": None,
            "film": None, "pending_fix": None, "pending_input": None,
            "last_fix": None, "status_msg": None, "last_input": None}  # fmt: skip


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


def timeline_text(board):
    lines = [f"{board.title} · {clock(board.duration)} · {board.tier}"]
    for number, card, start, end in board.timeline():
        lines.append(f"#{number}  {clock(start)}–{clock(end)}  {card.title}  ·  素材{card.photo}")
    return "\n".join(lines)


def card_text(board, number):
    card = board.cards[number - 1]
    _n, _c, start, end = board.timeline()[number - 1]
    return (
        f"#{number} · {clock(start)}–{clock(end)} · {card.title}\n"
        f"用：{board.material(card.photo)}{MODES[card.mode]}\n"
        f"画面：{card.description}\n"
        f"档位：{board.tier} · {card.seconds:g} 秒"
    )


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


def number_rows(prefix, count, per_row=6):
    buttons = [(str(i), f"{prefix}{i}") for i in range(1, count + 1)]
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
            if message.get("photo"):
                return self._photo(chat_id, message)
            text = (message.get("text") or "").strip()
            if text:
                return self._text(chat_id, text)
            self.api.send_message(chat_id, "目前支持照片和文字消息。")
        except Exception as error:
            self._failed(chat_id, error)
            raise

    # ---- helpers ----------------------------------------------------------

    def _board(self, state):
        return Storyboard.from_dict(state["board"])

    def _save_board(self, chat_id, state, board, **changes):
        state.update(board=board.to_dict(), **changes)
        self.store.save(chat_id, state)

    def _send_materials(self, chat_id, board, lead="📦 素材整理好了。"):
        self.api.send_photo(
            chat_id,
            self.adapter.materials_sheet(board),
            f"{lead}\n名字不对直接说「3 是吧台」，还可以继续发照片。",
            [[("✅ 没问题，写故事", "outline")], [("🆕 重新开始", "new")]],
        )

    def _send_outline(self, chat_id, board, lead="📝 故事大纲："):
        lines = "\n".join(f"{i}. {beat}" for i, beat in enumerate(board.outline, 1))
        self.api.send_message(
            chat_id,
            f"{lead}\n{lines}\n\n想改就直接说，比如「更热闹一点」。",
            [[("✅ 就这样，排镜头", "plan")], [("🆕 重新开始", "new")]],
        )

    def _send_board(self, chat_id, board, lead):
        rows = [[("🔍 看卡片：", "noop")]] + number_rows("card:", len(board.cards))
        rows += [[("🎬 开始生成", "gen"), ("✏️ 怎么改", "revise")], [("🆕 重新开始", "new")]]
        link = self.adapter.web_link(board)
        if link:
            rows.append([("🌐 在网页里打开这个故事", "url:" + link)])
        self.api.send_photo(
            chat_id, self.adapter.grid(board), f"{lead}\n\n{timeline_text(board)}", rows
        )

    def _send_card(self, chat_id, board, number):
        self.api.send_photo(
            chat_id,
            self.adapter.card_preview(board, number),
            card_text(board, number),
            [
                [("🖼 换素材", f"swap:{number}"), ("✏️ 改画面", f"edit:{number}")],
                [("🗑 删掉", f"del:{number}"), ("📝 看提示词", f"prompt:{number}")],
                [("⬅️ 回到分镜", "board")],
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
                    chat_id, message["message_id"], f"{text} {done}/{total}"
                )
            except TelegramError:
                pass  # "message is not modified" and similar are harmless

        return progress

    # ---- inputs -----------------------------------------------------------

    def _photo(self, chat_id, message):
        state = self.store.load(chat_id)
        if state["phase"] in ("generating", "delivered", "confirm"):
            self.api.send_message(chat_id, "这条片子已经在生成或做好了。要用新照片做一条新的，先发 /new。")
            return
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
        if state["board"]:
            board = self._board(state)
            board.photos.append(str(dest))
            board.materials.append(f"照片{number}")
            state["board"] = self.adapter.add_photo(board, str(dest)).to_dict()
        if state["board"]:
            self.store.save(chat_id, state)
            self.api.send_message(
                chat_id,
                f"已加入素材{number}。可以说「{number} 是招牌菜」改名字"
                + (f"，或「第3张换成素材{number}」。" if state["phase"] == "proposed" else "。"),
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
                self._send_card(chat_id, board, pending["n"])
                return
            if pending["kind"] == "fix" and state["phase"] == "delivered":
                return self._propose_fix(chat_id, state, pending["n"], text)
        phase = state["phase"]
        if phase == "collecting":
            state["brief"] = f"{state['brief']}；{text}" if state["brief"] else text
            return self._collecting(chat_id, state)
        if phase == "materials":
            match = RENAME_RE.match(text)
            board = self._board(state)
            if match and 1 <= int(match.group(1)) <= len(board.materials):
                board = self.adapter.rename_material(board, int(match.group(1)), match.group(2))
                self._save_board(chat_id, state, board)
                self._send_materials(chat_id, board, f"改好了：素材{match.group(1)} 是「{match.group(2).strip()}」。")
            else:
                board.brief = f"{board.brief}；{text}"
                self._save_board(chat_id, state, board)
                self.api.send_message(chat_id, "记下了，写故事时会用上。素材没问题就点「写故事」。")
            return
        if phase == "outline":
            board = self.adapter.revise_outline(self._board(state), text)
            self._save_board(chat_id, state, board)
            self._send_outline(chat_id, board, "📝 按你的意思改了：")
            return
        if phase in ("proposed", "confirm"):
            board, summary = self.adapter.revise(self._board(state), text)
            self._save_board(chat_id, state, board, phase="proposed")
            self._send_board(chat_id, board, f"改好了（{summary}）：")
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
        cards = len(board.cards) if board else 0

        if name == "new":
            self._new(chat_id)
        elif name == "open":
            self._open(chat_id, state, arg)
        elif name == "noop":
            pass
        elif name == "start" and phase == "collecting":
            self._start(chat_id, state)
        elif name == "outline" and phase == "materials":
            self._to_outline(chat_id, state)
        elif name == "plan" and phase == "outline":
            self._to_cards(chat_id, state)
        elif name == "board" and phase == "proposed":
            self._send_board(chat_id, board, "当前分镜：")
        elif name == "card" and phase == "proposed" and n and n <= cards:
            self._send_card(chat_id, board, n)
        elif name == "swap" and phase == "proposed" and n and n <= cards:
            rows = number_rows(f"use:{n}:", len(board.photos))
            self.api.send_photo(
                chat_id, self.adapter.materials_sheet(board),
                f"第{n}张换成哪个素材？", rows + [[("⬅️ 不换了", f"card:{n}")]],
            )  # fmt: skip
        elif name == "use" and phase == "proposed":
            card_no, _, material = arg.partition(":")
            if card_no.isdigit() and material.isdigit() and int(card_no) <= cards \
                    and 1 <= int(material) <= len(board.photos):  # fmt: skip
                board = self.adapter.swap_material(board, int(card_no), int(material))
                self._save_board(chat_id, state, board)
                self._send_card(chat_id, board, int(card_no))
            else:
                self._stale(chat_id)
        elif name == "edit" and phase == "proposed" and n and n <= cards:
            state["pending_input"] = {"kind": "edit", "n": n}
            self.store.save(chat_id, state)
            self.api.send_message(chat_id, f"第{n}张想拍成什么样？直接描述画面，比如「咖啡师倒拉花，慢动作」。")
        elif name == "del" and phase == "proposed" and n and n <= cards:
            board = self.adapter.delete_card(board, n)
            self._save_board(chat_id, state, board)
            self._send_board(chat_id, board, f"删掉了第{n}张：")
        elif name == "prompt" and phase in ("proposed", "delivered") and n and n <= cards:
            self.api.send_message(chat_id, f"第{n}张的完整提示词：\n\n{board.cards[n - 1].prompt}")
        elif name == "revise" and has_board and phase in ("proposed", "confirm"):
            self.api.send_message(
                chat_id,
                "直接告诉我改什么，比如：\n· 第3张换成素材5\n· 删掉第4张\n· 短一点 / 横屏\n· 第2张要更温馨\n也可以点数字进卡片里改。",
            )
        elif name == "gen" and phase in ("proposed", "confirm"):
            self._ask_generate(chat_id, state)
        elif name == "gen_ok" and phase == "confirm":
            self._generate(chat_id, state)
        elif name == "fix_yes" and phase == "delivered" and state["pending_fix"]:
            fix = state["pending_fix"]
            self._propose_fix(chat_id, state, fix["number"], fix["feedback"])
        elif name == "fix_no" and phase == "delivered" and state["pending_fix"]:
            state["pending_fix"] = None
            self.store.save(chat_id, state)
            self.api.send_message(chat_id, "那是哪一张？直接说「第N张……」。")
        elif name in ("fix_go", "fix_roll") and phase == "delivered" \
                and (state["pending_fix"] or {}).get("proposal"):  # fmt: skip
            self._apply_fix(chat_id, state, reroll=name == "fix_roll")
        elif name == "fix_own" and phase == "delivered" and state["pending_fix"]:
            n = state["pending_fix"]["number"]
            state["pending_input"] = {"kind": "fix", "n": n}
            self.store.save(chat_id, state)
            self.api.send_message(chat_id, f"第{n}张你想怎么改？说具体一点，比如「换成窗边的光，人物别出现」。")
        elif name == "fix_diff" and phase == "delivered" and (state["pending_fix"] or {}).get("proposal"):
            proposal = state["pending_fix"]["proposal"]
            self.api.send_message(
                chat_id,
                f"改前：\n{proposal['prompt_before']}\n\n改后：\n{proposal['prompt_after']}",
            )
        elif name == "keep" and phase == "delivered" and state["last_fix"]:
            state["last_fix"] = None
            self.store.save(chat_id, state)
            self.api.send_message(chat_id, "好，保留新版。还有哪里要改，继续说「第N秒……」。")
        elif name == "revert" and phase == "delivered" and state["last_fix"]:
            self._revert(chat_id, state)
        else:
            self._stale(chat_id)

    def _failed(self, chat_id, error):
        detail = getattr(error, "step", None)
        where = f"（{detail}）" if detail else ""
        try:
            self.api.send_message(chat_id, f"出错了{where}，已经停下，没有扣费。可以再试一次，或发 /status 看看现在的状态。")
        except Exception:
            pass

    def _stale(self, chat_id):
        self.api.send_message(chat_id, "这个按钮已经过期了，按最新的消息操作就好。")

    # ---- steps ------------------------------------------------------------

    def _collecting(self, chat_id, state):
        """Photos and text keep arriving: keep one status message and wait."""
        state["last_input"] = self.clock()
        count = len(state["photos"])
        text = f"📷 已收到 {count} 张照片" if count else "📝 记下了"
        if state["brief"]:
            text += f"\n要求：{state['brief'][:80]}"
        text += "\n\n继续发也行；停几秒我就开始整理，或者直接点按钮。"
        buttons = [[("📦 都发完了，整理素材", "start")]] if count else None
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
            if state["photos"] and state["brief"]:
                self._start(chat_id, state)
            elif state["brief"]:
                state["last_input"] = None
                self.store.save(chat_id, state)
                self.api.send_message(chat_id, NEED_PHOTOS)
            # photos without a sentence: wait for text, the button or /go

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
        if restored["phase"] == "generating":
            restored["phase"] = "confirm"
        self.store.save(chat_id, restored)
        board = self._board(restored)
        lead = f"找回了「{board.title}」："
        if restored["film"]:
            self._send_film(chat_id, board, Film.from_dict(restored["film"]), lead)
        elif restored["phase"] in ("proposed", "confirm"):
            self._send_board(chat_id, board, lead)
        else:
            self.api.send_message(
                chat_id, f"{lead}现在在「{PHASES[restored['phase']]}」这一步，发 /go 继续。"
            )

    def _command(self, chat_id, state, command):
        phase = state["phase"]
        if command == "/go":
            steps = {
                "collecting": self._start,
                "materials": self._to_outline,
                "outline": self._to_cards,
                "proposed": self._ask_generate,
                "confirm": self._ask_generate,  # never spends: re-asks to confirm
            }
            if phase in steps:
                return steps[phase](chat_id, state)
            if phase == "generating":
                return self.api.send_message(chat_id, "还在生成，稍等一下～")
            return self.api.send_message(
                chat_id, "片子已经做好了。要改就说「第12秒……」，要做新的发 /new。"
            )
        if command == "/status":
            lines = [f"现在：{PHASES[phase]}", f"照片：{len(state['photos'])} 张"]
            if state["board"]:
                board = self._board(state)
                lines.append(f"片子：{board.title}，{len(board.cards)} 个镜头，{clock(board.duration)}")
            if state["brief"]:
                lines.append(f"要求：{state['brief'][:60]}")
            lines.append("发 /go 继续下一步。")
            return self.api.send_message(chat_id, "\n".join(lines))
        if command == "/find":
            entries = self.store.history(chat_id)
            if not entries:
                return self.api.send_message(
                    chat_id, "还没有存档的片子。做完一条后发 /new，它就会存在这里。"
                )
            rows = [[(f"{e['title']} · {e['when']} · {PHASES.get(e['phase'], '')}",
                      f"open:{e['id']}")] for e in entries]  # fmt: skip
            return self.api.send_message(chat_id, "以前做过的片子，点一下接着改：", rows)
        if command == "/budget":
            return self.api.send_message(
                chat_id,
                "演示模式：不扣积分，也不会用显卡。\n接上正式账户后，这里显示剩余积分和最近的花费。",
            )
        self.api.send_message(chat_id, "没有这个命令。发 /help 看能用的命令。")

    def _to_outline(self, chat_id, state):
        state["phase"] = "outline"
        self.store.save(chat_id, state)
        self._send_outline(chat_id, self._board(state))

    def _to_cards(self, chat_id, state):
        self._action(chat_id, "typing")
        board = self.adapter.plan_shots(self._board(state))
        self._save_board(chat_id, state, board, phase="proposed")
        self._send_board(chat_id, board, "🎞 镜头卡排好了。点数字看每张卡的细节：")

    def _ask_generate(self, chat_id, state):
        board = self._board(state)
        estimate = self.adapter.estimate(board)
        state["phase"] = "confirm"
        self.store.save(chat_id, state)
        check = ("⚠️ 内容检查：\n" + "\n".join(f"· {c}" for c in estimate.checks)
                 if estimate.checks else "✅ 内容检查通过")  # fmt: skip
        self.api.send_message(
            chat_id,
            f"{check}\n生成 {clock(estimate.seconds)}，{board.tier}档，约 {estimate.credits} 积分。\n{estimate.note}",
            [[("✅ 确认生成", "gen_ok"), ("✏️ 再改改", "revise")]],
        )

    def _action(self, chat_id, action):
        """Show "typing…"/"sending video…" while working; purely cosmetic."""
        try:
            self.api.send_chat_action(chat_id, action)
        except (TelegramError, AttributeError):
            pass

    def _start(self, chat_id, state):
        self._waiting.discard(chat_id)
        if not state["photos"]:
            self.api.send_message(chat_id, NEED_PHOTOS)
            return
        self._action(chat_id, "typing")
        board = self.adapter.start(chat_id, state["photos"], state["brief"])
        self._save_board(chat_id, state, board, phase="materials", film=None,
                         last_input=None, status_msg=None)  # fmt: skip
        self._send_materials(chat_id, board)

    def _generate(self, chat_id, state):
        board = self._board(state)
        state["phase"] = "generating"
        self.store.save(chat_id, state)
        self._action(chat_id, "upload_video")
        progress = self._progress_message(chat_id, "🎬 生成中……")
        try:
            board, film = self.adapter.generate(chat_id, board, progress)
        except GenerationHeld:
            state["phase"] = "confirm"
            self.store.save(chat_id, state)
            link = self.adapter.web_link(board)
            self.api.send_message(
                chat_id,
                "⏸ 停在这里了：真的生成要开显卡，需要老板批准，这次测试不提交。\n"
                "故事板和镜头卡已经保存在网站上，可以在网页里继续看和改。",
                [[("🌐 在网页里打开", "url:" + link)]] if link else None,
            )
            return
        except Exception:
            log.exception("demo generate failed")
            state["phase"] = "confirm"
            self.store.save(chat_id, state)
            self.api.send_message(chat_id, "生成失败了，可以再点一次「确认生成」。",
                                  [[("✅ 确认生成", "gen_ok")]])  # fmt: skip
            return
        self._save_board(chat_id, state, board, phase="delivered", film=film.to_dict())
        self._send_film(chat_id, board, film, "🎉 片子好了！")

    def _send_film(self, chat_id, board, film, lead, buttons=None):
        self.api.send_video(
            chat_id,
            film.path,
            f"{lead}\n\n{timeline_text(board)}\n\n哪里不满意就说「第12秒……」或「第3张……」，我只改那一段。",
            buttons or [[("🆕 做新的", "new")]],
        )

    def _feedback(self, chat_id, state, text):
        board = self._board(state)
        film = Film.from_dict(state["film"])
        second = parse_second(text)
        if second is not None:
            number = board.locate(second)
            if number is None:
                self.api.send_message(
                    chat_id, f"片子只有 {clock(board.duration)}，第{second:g}秒不在里面。"
                )
                return
            state["pending_fix"] = {"number": number, "feedback": text}
            self.store.save(chat_id, state)
            self.api.send_photo(
                chat_id,
                self.adapter.frame(film, second),
                f"第{second:g}秒是第{number}张「{board.cards[number - 1].title}」，是这一段吗？",
                [[("✅ 是这张", "fix_yes"), ("❌ 不是", "fix_no")]],
            )
            return
        number = parse_card(text)
        if number is not None and 1 <= number <= len(board.cards):
            return self._propose_fix(chat_id, state, number, text)
        self.api.send_message(
            chat_id, "想改哪里？说「第12秒……」或「第3张……」。要做新的片子就发 /new。"
        )

    def _propose_fix(self, chat_id, state, number, feedback):
        board = self._board(state)
        proposal = self.adapter.propose_fix(board, number, feedback)
        state["pending_fix"] = {"number": number, "feedback": feedback,
                                "proposal": proposal.to_dict()}  # fmt: skip
        self.store.save(chat_id, state)
        card = board.cards[number - 1]
        self.api.send_message(
            chat_id,
            f"🛠 第{number}张「{card.title}」准备这样改：\n· {proposal.summary}\n"
            f"素材：{board.material(card.photo)}不变\n约 {proposal.credits} 积分（演示不扣）",
            [
                [("✅ 按这个重做", "fix_go")],
                [("🎲 不改，只重抽", "fix_roll"), ("✏️ 我自己说", "fix_own")],
                [("📝 看提示词对比", "fix_diff")],
            ],
        )

    def _apply_fix(self, chat_id, state, reroll):
        fix = state["pending_fix"]
        number = fix["number"]
        board = self._board(state)
        before = board.cards[number - 1].version
        prompt = None if reroll else fix["proposal"]["prompt_after"]
        progress = self._progress_message(chat_id, f"🔁 重做第{number}张……")
        board, film = self.adapter.apply_fix(board, number, prompt, progress)
        after = board.cards[number - 1].version
        self._save_board(chat_id, state, board, film=film.to_dict(), pending_fix=None,
                         last_fix={"number": number, "previous": before})  # fmt: skip
        self._send_film(
            chat_id, board, film,
            f"第{number}张出了新版（v{after}），重新剪好了。",
            [[("👍 保留新版", "keep"), (f"↩️ 换回 v{before}", "revert")],
             [("🆕 做新的", "new")]],
        )  # fmt: skip

    def _revert(self, chat_id, state):
        last = state["last_fix"]
        board, film = self.adapter.adopt(self._board(state), last["number"], last["previous"])
        self._save_board(chat_id, state, board, film=film.to_dict(), last_fix=None)
        self._send_film(chat_id, board, film, f"已换回第{last['number']}张的 v{last['previous']}，重新剪好了。")

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
