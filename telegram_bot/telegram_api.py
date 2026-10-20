"""Minimal Telegram Bot API client (long polling). The token never leaves memory.

Errors never include request URLs, because Telegram puts the token in the path.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

ORIGIN = "https://api.telegram.org"


class TelegramError(Exception):
    def __init__(self, method, description):
        self.method, self.description = method, description
        super().__init__(f"{method}: {description}")


def keyboard(rows):
    """rows: [[(label, callback_data), ...], ...]"""
    return json.dumps(
        {
            "inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in rows
            ]
        },
        ensure_ascii=False,
    )


class TelegramAPI:
    def __init__(self, token, *, transport=None, poll_timeout=50):
        if not token or "/" in token:
            raise ValueError("invalid bot token")
        self._token = token
        self.poll_timeout = poll_timeout
        self._client = httpx.Client(
            base_url=ORIGIN,
            transport=transport,
            timeout=httpx.Timeout(poll_timeout + 20, connect=15),
            follow_redirects=False,
        )

    def __repr__(self):
        return "TelegramAPI(<token hidden>)"

    def call(self, method, data=None, files=None):
        try:
            response = self._client.post(
                f"/bot{self._token}/{method}", data=data or {}, files=files
            )
            body = response.json()
        except httpx.HTTPError as error:
            raise TelegramError(method, type(error).__name__) from None
        except ValueError:
            raise TelegramError(method, "invalid response") from None
        if not body.get("ok"):
            raise TelegramError(method, str(body.get("description", "error"))[:200])
        return body["result"]

    def get_me(self):
        return self.call("getMe")

    def get_updates(self, offset=None, timeout=None):
        data = {"timeout": self.poll_timeout if timeout is None else timeout,
                "allowed_updates": json.dumps(["message", "callback_query"])}  # fmt: skip
        if offset is not None:
            data["offset"] = offset
        return self.call("getUpdates", data)

    def send_message(self, chat_id, text, buttons=None):
        data = {"chat_id": chat_id, "text": text[:4000]}
        if buttons:
            data["reply_markup"] = keyboard(buttons)
        return self.call("sendMessage", data)

    def edit_message_text(self, chat_id, message_id, text, buttons=None):
        data = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]}
        if buttons:
            data["reply_markup"] = keyboard(buttons)
        return self.call("editMessageText", data)

    def send_chat_action(self, chat_id, action="typing"):
        return self.call("sendChatAction", {"chat_id": chat_id, "action": action})

    def set_commands(self, commands):
        """commands: [(name, description)] shown in the Telegram menu."""
        payload = [{"command": c, "description": d} for c, d in commands]
        return self.call("setMyCommands", {"commands": json.dumps(payload, ensure_ascii=False)})

    def _send_file(self, method, field, chat_id, path, caption, buttons):
        data = {"chat_id": chat_id, "caption": caption[:1000]}
        if buttons:
            data["reply_markup"] = keyboard(buttons)
        with open(path, "rb") as handle:
            return self.call(method, data, files={field: (Path(path).name, handle)})

    def send_photo(self, chat_id, path, caption="", buttons=None):
        return self._send_file("sendPhoto", "photo", chat_id, path, caption, buttons)

    def send_video(self, chat_id, path, caption="", buttons=None):
        return self._send_file("sendVideo", "video", chat_id, path, caption, buttons)

    def answer_callback(self, callback_id, text=None):
        data = {"callback_query_id": callback_id}
        if text:
            data["text"] = text[:190]
        return self.call("answerCallbackQuery", data)

    def download(self, file_id, dest):
        info = self.call("getFile", {"file_id": file_id})
        file_path = info.get("file_path", "")
        if not file_path or ".." in file_path:
            raise TelegramError("getFile", "missing file path")
        try:
            response = self._client.get(f"/file/bot{self._token}/{file_path}")
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TelegramError("download", type(error).__name__) from None
        Path(dest).write_bytes(response.content)
        return str(dest)
