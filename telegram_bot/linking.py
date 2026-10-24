"""Link a Telegram user to their own Inkseq account (#382, options 1+2+3).

Reuses the published Agent Connect helper (skills/inkseq/scripts/connect.py):
the bot generates the PAT and verifier locally, saves them in the OS secure
store *before* exchanging the owner's one-time code, verifies the returned
authorization against the fingerprint, and keeps only non-secret metadata in
its own state. The raw key is read into memory only to call the API.

Accepted inputs: the instruction block the website's Connect AI page shows
(code + connection metadata JSON), or later a `/start l_<code>` deep link once
the backend exposes the grant preview described on #382.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import tempfile
import os

CODE_RE = re.compile(r"sxc_[A-Za-z0-9_-]{43}")
CONNECTION_RE = re.compile(r"connection-[0-9a-f]{32}")
FINGERPRINT_RE = re.compile(r"\b[0-9a-f]{64}\b")
HELPER = (
    Path(__file__).resolve().parents[1] / "skills" / "inkseq" / "scripts" / "connect.py"
)


def load_helper():
    spec = importlib.util.spec_from_file_location("inkseq_connect_helper", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contains_code(text):
    return bool(CODE_RE.search(text or ""))


def parse_instruction(text):
    """Code + connection metadata from what the Connect AI page shows.

    Returns None when anything needed is missing; never echoes the code.
    """
    code = CODE_RE.search(text or "")
    if not code:
        return None
    metadata = {}
    start = text.find("{")
    if start != -1:
        depth = 0
        for end in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            if depth == 0:
                try:
                    metadata = json.loads(text[start : end + 1])
                except ValueError:
                    metadata = {}
                break
    authorization = metadata.get("authorization") or {}
    connection_id = metadata.get("connection_id") or authorization.get("connection_id")
    if not connection_id:
        found = CONNECTION_RE.search(text)
        connection_id = found.group(0) if found else None
    fingerprint = metadata.get("authorization_fingerprint")
    if not fingerprint:
        found = FINGERPRINT_RE.search(text)
        fingerprint = found.group(0) if found else None
    fields = {
        "code": code.group(0),
        "connection_id": connection_id,
        "fingerprint": fingerprint,
        "owner": authorization.get("owner"),
        "tenant": authorization.get("tenant"),
        "origin": authorization.get("origin"),
        "profile_id": authorization.get("profile_id", "creator-full"),
        "profile_version": authorization.get("profile_version", 1),
    }
    if not all(
        fields[k] for k in ("connection_id", "fingerprint", "owner", "tenant", "origin")
    ):
        return None
    return fields


class LinkError(Exception):
    """Static, user-safe reason codes only."""


class Linker:
    """Per-Telegram-user account links; secrets stay in the OS secure store."""

    def __init__(self, folder, allowed_origins, *, helper=None, store=None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.allowed_origins = set(allowed_origins)
        self.helper = helper or load_helper()
        self.store = store or self.helper.SecureStore()

    def _path(self):
        return self.folder / "links.json"

    def _links(self):
        path = self._path()
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _save(self, links):
        fd, tmp = tempfile.mkstemp(dir=self.folder, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(links, handle, indent=1)
        os.replace(tmp, self._path())

    def get(self, chat_id):
        """Public link metadata for a chat, or None."""
        return self._links().get(str(int(chat_id)))

    def link(self, chat_id, fields):
        if fields["origin"] not in self.allowed_origins:
            raise LinkError("origin_not_allowed")
        client = self.helper.AgentClient(fields["origin"], store=self.store)
        try:
            result = client.connect(
                code=fields["code"], connection_id=fields["connection_id"],
                owner=fields["owner"], tenant=fields["tenant"],
                fingerprint=fields["fingerprint"], profile_id=fields["profile_id"],
                profile_version=fields["profile_version"],
            )  # fmt: skip
        except self.helper.HelperError as error:
            raise LinkError(str(error)) from None
        links = self._links()
        links[str(int(chat_id))] = {
            "origin": result["origin"], "connection_id": result["connection_id"],
            "owner": result["owner"], "expires_at": result.get("expires_at"),
        }  # fmt: skip
        self._save(links)
        return links[str(int(chat_id))]

    def fields_from_code(self, code, *, http=None):
        """Deep-link payload → exchange fields via the grant preview (#382).

        Returns None while the backend does not offer the preview yet, so the
        bot can fall back to the pasted instruction.
        """
        if not CODE_RE.fullmatch(code or ""):
            raise LinkError("invalid_code")
        import httpx  # the helper itself is standard-library only

        for origin in sorted(self.allowed_origins):
            try:
                response = (http or httpx).get(
                    origin + "/v1/agent-connect/grant-preview",
                    params={"code": code}, timeout=15, follow_redirects=False,
                )  # fmt: skip
            except Exception:
                continue
            if response.status_code in (404, 405):
                return None  # not deployed yet
            if response.status_code != 200:
                raise LinkError({410: "connection_consumed", 403: "connection_expired"}
                                .get(response.status_code, "preview_failed"))  # fmt: skip
            preview = response.json()
            authorization = preview.get("authorization") or {}
            text = code + "\n" + json.dumps({
                "connection_id": preview.get("connection_id"),
                "authorization_fingerprint": preview.get("authorization_fingerprint"),
                "authorization": authorization,
            })  # fmt: skip
            return parse_instruction(text)
        return None

    def unlink(self, chat_id):
        links = self._links()
        removed = links.pop(str(int(chat_id)), None)
        self._save(links)
        return removed

    def token(self, chat_id):
        """The chat's PAT, in memory only, or None when not (or no longer) linked."""
        link = self.get(chat_id)
        if not link:
            return None
        state = self.store.load(link["origin"], link["connection_id"])
        if not state or state.get("status") != "connected":
            return None
        return state.get("api_key")
