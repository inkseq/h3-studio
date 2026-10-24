"""The Telegram front end of the core story flow (#531).

One message per step, same steps as the website's Quick Create:
link → chat builds the story cards → (pictures, website for now) → make video with a
quote and an explicit confirm → takes arrive as videos with "Use this" → film on the website.
All decisions come from the server; this module only renders them and relays taps.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

from .studio import StudioClient, StudioError

log = logging.getLogger(__name__)

COMMANDS = [
    ("new", "Start a new story / 新故事"),
    ("stories", "Your stories / 我的故事"),
    ("story", "Show the current story / 当前故事"),
    ("link", "Link your Inkseq account / 绑定账号"),
]

TEXT = {
    "zh": {
        "link": "先绑定你的映序账号：点下面的按钮，在网页上确认后回到这里。",
        "link_button": "绑定映序账号",
        "linked": "已绑定。直接告诉我想做什么故事吧。",
        "link_failed": "绑定没有成功（{code}），请在网页上重新生成绑定码再试。",
        "new": "新故事已开始。说说你想做什么吧。",
        "thinking": "正在想…",
        "failed": "这一步没成功（{code}），可以再发一次。",
        "no_story": "还没有故事。发一句话开始，或者用 /new。",
        "stories": "你的故事：",
        "switched": "已切换到「{title}」。",
        "cast": "人物：{names}",
        "places": "场景：{names}",
        "props": "道具：{names}",
        "shots": "镜头：",
        "shot": "{n}. {text}{status}",
        "ready": " ✅",
        "takes": " （有 {count} 个版本可选）",
        "make_video": "生成视频（{count} 个镜头）",
        "new_story": "开始新故事：{idea}",
        "open_web": "在网页打开",
        "checking": "正在预检和估算…",
        "quote": "预计 {credits}。确认后开始生成；只按最后交付的结果收费。",
        "quote_unknown": "已预检。确认后开始生成；只按最后交付的结果收费。",
        "blocked": "现在还不能生成：{reason}",
        "why_invalid_generation_input": "镜头还缺参考图或开头画面，请在网页上补齐后再生成",
        "confirm": "确认生成",
        "cancel": "取消",
        "cancelled": "已取消，没有生成。",
        "started": "开始生成，好了会把视频发给你。",
        "take": "第 {n} 个镜头的新版本",
        "use": "用这个版本",
        "used": "已选用。",
    },
    "en": {
        "link": "First link your Inkseq account: tap below, confirm on the website, then come back.",
        "link_button": "Link Inkseq account",
        "linked": "Linked. Tell me what story you want to make.",
        "link_failed": "Linking did not work ({code}). Make a new link code on the website and try again.",
        "new": "New story started. What should it be about?",
        "thinking": "Thinking…",
        "failed": "That step did not work ({code}). You can send it again.",
        "no_story": "No story yet. Send a message to start one, or use /new.",
        "stories": "Your stories:",
        "switched": "Now on “{title}”.",
        "cast": "Characters: {names}",
        "places": "Places: {names}",
        "props": "Props: {names}",
        "shots": "Shots:",
        "shot": "{n}. {text}{status}",
        "ready": " ✅",
        "takes": " ({count} takes to choose from)",
        "make_video": "Make video ({count} shots)",
        "new_story": "Start a new story: {idea}",
        "open_web": "Open on the website",
        "checking": "Checking and estimating…",
        "quote": "Estimated {credits}. Confirm to start; you pay only for what is delivered.",
        "quote_unknown": "Checked. Confirm to start; you pay only for what is delivered.",
        "blocked": "Can't generate yet: {reason}",
        "why_invalid_generation_input": "a shot still needs reference pictures or a first frame; add them on the website, then generate",
        "confirm": "Confirm and generate",
        "cancel": "Cancel",
        "cancelled": "Cancelled. Nothing was generated.",
        "started": "Generating. I'll send each video when it is ready.",
        "take": "New take for shot {n}",
        "use": "Use this take",
        "used": "Chosen.",
    },
}


class Store:
    """Per-chat state: current story, open quotes and takes already sent. No secrets."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def _path(self, chat_id):
        return self.folder / f"{int(chat_id)}.json"

    def get(self, chat_id):
        path = self._path(chat_id)
        if not path.exists():
            return {
                "story": None,
                "language": "zh",
                "chips": [],
                "offers": {},
                "sent": {},
            }
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, chat_id, value):
        fd, tmp = tempfile.mkstemp(dir=self.folder, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
        os.replace(tmp, self._path(chat_id))

    def chats(self):
        return [int(p.stem) for p in self.folder.glob("*.json")]


def turn_key(raw):
    """A stable turn id from a Telegram message or tap: the server needs 8–80 of [A-Za-z0-9_-],
    and the same id again replays the stored turn instead of calling the model twice."""
    value = "tg-turn-" + re.sub(r"[^A-Za-z0-9_-]", "-", str(raw))
    return value[:80]


def credits_text(receipt):
    for key in ("credits", "total_credits", "credits_total", "estimated_credits"):
        if isinstance(receipt.get(key), int):
            return f"{receipt[key]} credits"
    return None


class Flow:
    def __init__(
        self,
        api,
        linker,
        store,
        media_dir,
        *,
        origin,
        client_factory=None,
        clock=time.time,
    ):
        self.api, self.linker, self.store = api, linker, store
        self.media = Path(media_dir)
        self.media.mkdir(parents=True, exist_ok=True)
        self.origin = origin
        self.client_factory = client_factory or (
            lambda origin, token: StudioClient(origin, token)
        )
        self.clock = clock

    # ---------- helpers ----------
    def t(self, state, key, **values):
        return TEXT[state.get("language", "zh")][key].format(**values)

    def client(self, chat_id):
        link = self.linker.get(chat_id)
        token = link and self.linker.token(chat_id)
        return self.client_factory(link["origin"], token) if token else None

    def ask_to_link(self, chat_id, state):
        self.api.send_message(
            chat_id,
            self.t(state, "link"),
            [
                [
                    (
                        self.t(state, "link_button"),
                        "url:" + self.origin + "/connect/telegram",
                    )
                ]
            ],
        )

    def summary(self, state, view):
        cards = view.get("cards", {})
        lines = [view.get("title") or ""]
        for group, key in (
            ("characters", "cast"),
            ("places", "places"),
            ("props", "props"),
        ):
            names = "、".join(c["name"] for c in cards.get(group, []))
            if names:
                lines.append(self.t(state, key, names=names))
        shots = cards.get("shots", [])
        if shots:
            lines.append(self.t(state, "shots"))
            for index, shot in enumerate(shots, 1):
                status = (
                    self.t(state, "ready")
                    if shot.get("selected_artifact_id")
                    else self.t(state, "takes", count=shot["takes"])
                    if shot.get("takes")
                    else ""
                )
                lines.append(
                    self.t(
                        state, "shot", n=index, text=shot["text"][:80], status=status
                    )
                )
        return "\n".join(line for line in lines if line)

    def reply_to_turn(self, chat_id, state, view):
        turn = view.get("turn") or {}
        assistant = next(
            (m for m in turn.get("messages", []) if m.get("role") == "assistant"), {}
        )
        buttons, offers = [], {}
        state["chips"] = list(turn.get("chips") or assistant.get("chips") or [])[:6]
        for index, chip in enumerate(state["chips"]):
            buttons.append([(chip[:60], f"c:{index}")])
        proposal = turn.get("proposal") or assistant.get("proposal") or {}
        if proposal.get("shot_ids"):
            offers["g"] = proposal["shot_ids"]
            buttons.append(
                [(self.t(state, "make_video", count=len(proposal["shot_ids"])), "g")]
            )
        if proposal.get("new_story"):
            offers["n"] = proposal["new_story"]
            buttons.append(
                [(self.t(state, "new_story", idea=proposal["new_story"][:40]), "n")]
            )
        buttons.append([(self.t(state, "open_web"), "url:" + self.web_link(state))])
        state["offers"] = offers
        text = (assistant.get("text") or "").strip()
        self.api.send_message(
            chat_id, text + "\n\n" + self.summary(state, view), buttons
        )

    def web_link(self, state):
        return (
            f"{self.origin}/quick-chat?story={state['story']}"
            if state.get("story")
            else self.origin
        )

    # ---------- updates ----------
    def handle(self, update):
        if "callback_query" in update:
            return self.handle_tap(update["callback_query"])
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None or not text:
            return
        state = self.store.get(chat_id)
        code = (message.get("from") or {}).get("language_code") or ""
        state["language"] = "en" if code.startswith("en") else "zh"
        try:
            if text.startswith("/start"):
                self.start(chat_id, state, text)
            elif text.startswith("/link"):
                self.ask_to_link(chat_id, state)
            elif not self.client(chat_id):
                self.ask_to_link(chat_id, state)
            elif text.startswith("/new"):
                self.new_story(chat_id, state)
            elif text.startswith("/stories"):
                self.list_stories(chat_id, state)
            elif text.startswith("/story"):
                self.show_story(chat_id, state)
            else:
                self.send_turn(
                    chat_id, state, text, f"tg-{chat_id}-{message['message_id']}"
                )
        finally:
            self.store.put(chat_id, state)

    def start(self, chat_id, state, text):
        payload = text.split(maxsplit=1)[1] if " " in text else ""
        if payload.startswith("l_"):
            try:
                fields = self.linker.fields_from_code(payload[2:])
                if not fields:
                    raise StudioError(0, "preview_unavailable")
                self.linker.link(chat_id, fields)
            except Exception as error:  # static, user-safe codes only
                code = getattr(error, "code", None) or str(error)[:60]
                self.api.send_message(chat_id, self.t(state, "link_failed", code=code))
                return
            self.api.send_message(chat_id, self.t(state, "linked"))
            return
        if self.client(chat_id):
            self.api.send_message(chat_id, self.t(state, "linked"))
        else:
            self.ask_to_link(chat_id, state)

    def new_story(self, chat_id, state, first_text=None):
        view = self.client(chat_id).create_chat(state["language"])
        state.update(story=view["project_id"], chips=[], offers={}, sent={})
        if first_text:
            return self.send_turn(
                chat_id, state, first_text, f"tg-{chat_id}-{int(self.clock())}"
            )
        self.api.send_message(chat_id, self.t(state, "new"))

    def list_stories(self, chat_id, state):
        rows = self.client(chat_id).chats(limit=10)
        if not rows:
            self.api.send_message(chat_id, self.t(state, "no_story"))
            return
        buttons = [[(row["title"][:50], "s:" + row["project_id"])] for row in rows]
        self.api.send_message(chat_id, self.t(state, "stories"), buttons)

    def show_story(self, chat_id, state):
        if not state.get("story"):
            self.api.send_message(chat_id, self.t(state, "no_story"))
            return
        view = self.client(chat_id).chat(state["story"])
        self.api.send_message(
            chat_id,
            self.summary(state, view),
            [[(self.t(state, "open_web"), "url:" + self.web_link(state))]],
        )

    def send_turn(self, chat_id, state, text, turn_id):
        client = self.client(chat_id)
        if not state.get("story"):
            view = client.create_chat(state["language"])
            state.update(story=view["project_id"], sent={})
        self.api.send_chat_action(chat_id)
        try:
            view = client.turn(state["story"], turn_key(turn_id), text)
        except StudioError as error:
            self.api.send_message(chat_id, self.t(state, "failed", code=error.code))
            return
        self.reply_to_turn(chat_id, state, view)

    # ---------- taps ----------
    def handle_tap(self, query):
        chat_id = query["message"]["chat"]["id"]
        data = query.get("data") or ""
        state = self.store.get(chat_id)
        self.api.answer_callback(query["id"])
        try:
            if not self.client(chat_id):
                self.ask_to_link(chat_id, state)
            elif data.startswith("c:"):
                index = int(data[2:])
                if index < len(state.get("chips", [])):
                    self.send_turn(
                        chat_id, state, state["chips"][index], f"tg-{query['id']}"
                    )
            elif data.startswith("s:"):
                state.update(story=data[2:], chips=[], offers={}, sent={})
                view = self.client(chat_id).chat(state["story"])
                self.api.send_message(
                    chat_id, self.t(state, "switched", title=view.get("title") or "")
                )
                self.show_story(chat_id, state)
            elif data == "n" and state.get("offers", {}).get("n"):
                self.new_story(chat_id, state, first_text=state["offers"]["n"])
            elif data == "g" and state.get("offers", {}).get("g"):
                self.quote(chat_id, state, state["offers"]["g"])
            elif data.startswith("b:"):
                self.submit(chat_id, state, data[2:])
            elif data == "x":
                state.pop("quote", None)
                self.api.send_message(chat_id, self.t(state, "cancelled"))
            elif data.startswith("u:"):
                self.use_take(chat_id, state, data[2:])
        except StudioError as error:
            self.api.send_message(chat_id, self.t(state, "failed", code=error.code))
        finally:
            self.store.put(chat_id, state)

    def quote(self, chat_id, state, shot_ids):
        client = self.client(chat_id)
        self.api.send_message(chat_id, self.t(state, "checking"))
        view = client.chat(state["story"])
        key = f"tg-pre-{chat_id}-{int(self.clock())}"
        receipt = client.preflight(state["story"], view["version"], shot_ids, key)
        receipt = client.preflight_status(state["story"], receipt["id"])
        if not receipt.get("ready"):
            # The server says why; the website shows the same shot with what it still needs.
            reason = receipt.get("blocked") or ", ".join(
                str((item.get("plan") or {}).get("status"))
                for item in receipt.get("items", [])
                if (item.get("plan") or {}).get("status") not in (None, "ready")
            )
            self.api.send_message(
                chat_id,
                self.t(
                    state,
                    "blocked",
                    reason=TEXT[state.get("language", "zh")].get(
                        "why_" + str(reason), str(reason or receipt.get("status"))
                    )[:300],
                ),
                [[(self.t(state, "open_web"), "url:" + self.web_link(state))]],
            )
            return
        state["quote"] = {
            "id": receipt["id"],
            "plans": [item["plan_id"] for item in receipt["items"]],
            "shots": shot_ids,
        }
        credits = credits_text(receipt)
        text = (
            self.t(state, "quote", credits=credits)
            if credits
            else self.t(state, "quote_unknown")
        )
        self.api.send_message(
            chat_id,
            text,
            [
                [
                    (self.t(state, "confirm"), "b:" + receipt["id"]),
                    (self.t(state, "cancel"), "x"),
                ]
            ],
        )

    def submit(self, chat_id, state, preflight_id):
        quote = state.get("quote") or {}
        if quote.get("id") != preflight_id:
            return
        # One key per confirmed quote: a repeated tap never submits twice.
        self.client(chat_id).submit(
            state["story"], quote["plans"], "tg-batch-" + preflight_id
        )
        state.setdefault("watch", {})
        for shot in quote["shots"]:
            state["watch"][shot] = self.clock()
        state.pop("quote", None)
        self.api.send_message(chat_id, self.t(state, "started"))

    def use_take(self, chat_id, state, value):
        shot_id, artifact_id = value.split("|", 1)
        client = self.client(chat_id)
        view = client.chat(state["story"])
        try:
            client.choose(state["story"], shot_id, artifact_id, view["version"])
        except StudioError as error:
            if error.status != 422 or "segment" not in error.code:
                raise
            # A shot with kept parts (#512) clears them in the same edit, like the website.
            client.choose(
                state["story"],
                shot_id,
                artifact_id,
                view["version"],
                clear_segments=True,
            )
        self.api.send_message(chat_id, self.t(state, "used"))

    # ---------- finished takes ----------
    def deliver(self):
        """Send newly finished takes for shots this chat is waiting on."""
        for chat_id in self.store.chats():
            state = self.store.get(chat_id)
            if not state.get("watch") or not state.get("story"):
                continue
            client = self.client(chat_id)
            if not client:
                continue
            try:
                view = client.chat(state["story"])
                order = {
                    s["id"]: i for i, s in enumerate(view["cards"].get("shots", []), 1)
                }
                for shot_id in list(state["watch"]):
                    rows = client.versions(state["story"], shot_id).get("versions", [])
                    sent = set(state.setdefault("sent", {}).get(shot_id, []))
                    for row in rows:
                        if row.get("kind") != "video" or row["id"] in sent:
                            continue
                        if row.get("status") not in (
                            None,
                            "succeeded",
                            "done",
                            "ready",
                        ):
                            continue
                        path = self.media / f"{row['id']}.mp4"
                        client.download(row["id"], path)
                        self.api.send_video(
                            chat_id,
                            path,
                            self.t(state, "take", n=order.get(shot_id, "?")),
                            [[(self.t(state, "use"), f"u:{shot_id}|{row['id']}")]],
                        )
                        path.unlink(missing_ok=True)
                        sent.add(row["id"])
                    state["sent"][shot_id] = sorted(sent)
                    # Stop watching once something arrived or after two hours.
                    if sent or self.clock() - state["watch"][shot_id] > 7200:
                        state["watch"].pop(shot_id)
            except StudioError as error:
                log.warning("deliver failed for a chat: %s", error.code)
            finally:
                self.store.put(chat_id, state)
