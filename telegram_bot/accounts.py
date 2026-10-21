"""Route each chat to an adapter bound to its own Inkseq account (#382).

A linked chat gets a Gemini + website adapter whose website client carries
that owner's PAT, so stories land in their Works and use their credits within
the connection's daily cap. An unlinked chat gets the default adapter (plans
in chat only, nothing saved to any account).
"""

from __future__ import annotations

from .staging_adapter import LlmStagingAdapter, StudioClient


class AccountRouter:
    def __init__(self, linker, default, gemini, workdir, *, client_factory=None):
        self.linker = linker
        self.default = default
        self.gemini = gemini
        self.workdir = workdir
        self.client_factory = client_factory or (
            lambda origin, token: StudioClient(origin, token=token)
        )
        self._adapters = {}  # chat id -> (connection id, adapter)
        self._on_saved = None

    @property
    def on_saved(self):
        return self._on_saved

    @on_saved.setter
    def on_saved(self, callback):
        self._on_saved = callback
        for _connection, adapter in self._adapters.values():
            adapter.on_saved = callback

    def for_chat(self, chat_id):
        link = self.linker.get(chat_id)
        if not link:
            return self.default
        cached = self._adapters.get(chat_id)
        if cached and cached[0] == link["connection_id"]:
            return cached[1]
        token = self.linker.token(chat_id)
        if not token:  # revoked locally or the secure store lost it
            return self.default
        client = self.client_factory(link["origin"], token)
        adapter = LlmStagingAdapter(self.workdir / str(chat_id), client, self.gemini,
                                    on_saved=self._on_saved)  # fmt: skip
        self._adapters[chat_id] = (link["connection_id"], adapter)
        return adapter

    def forget(self, chat_id):
        self._adapters.pop(chat_id, None)
