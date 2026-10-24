"""Run the Telegram bot on the core story flow: python -m telegram_bot --home <dir>

<dir>/config.json (no secrets):
  {"registry_profile": "telegram-bot--...", "origin": "https://staging.inkseq.com",
   "allowed_user_ids": [...]}   # optional allowlist while testing
The bot token is read in memory from the central AI-Registry and never printed. Each
linked person's connection token stays in the OS credential store (Agent Connect helper).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys

from .flow import COMMANDS, Flow, Store
from .linking import Linker
from .telegram_api import ORIGIN, TelegramAPI


def load_token(profile):
    root = Path(
        os.environ.get("AI_REGISTRY_ROOT", r"C:\Users\danmo\Desktop\AI-Registry")
    )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from api_registry import load_api

    config = load_api("telegram-bot", profile=profile)
    if config.base_url != ORIGIN or not config.api_key:
        raise SystemExit("registry profile endpoint or token mismatch")
    return config.api_key


class Allowlisted:
    """While testing, only allowlisted Telegram users reach the flow."""

    def __init__(self, flow, allowed):
        self.flow, self.allowed = flow, set(allowed)

    def handle(self, update):
        sender = (update.get("message") or update.get("callback_query") or {}).get(
            "from"
        ) or {}
        if self.allowed and sender.get("id") not in self.allowed:
            return
        self.flow.handle(update)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="telegram_bot")
    parser.add_argument("--home", default=".local/telegram-bot-core")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines carry the URL
    home = Path(args.home)
    config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    origin = config["origin"]
    api = TelegramAPI(load_token(config["registry_profile"]))
    me = api.get_me()
    api.set_commands(COMMANDS)
    linker = Linker(home / "accounts", [origin])
    flow = Flow(api, linker, Store(home / "state"), home / "media", origin=origin)
    gate = Allowlisted(flow, [int(x) for x in config.get("allowed_user_ids", [])])
    logging.info("running as @%s on %s", me.get("username"), origin)
    _poll(api, gate, flow)


def _poll(api, gate, flow):
    import time

    offset, last = None, 0.0
    while True:
        try:
            for update in api.get_updates(offset):
                offset = update["update_id"] + 1
                try:
                    gate.handle(update)
                except Exception:
                    logging.exception("update failed")
        except Exception:
            logging.exception("polling failed")
            time.sleep(5)
        if time.monotonic() - last > 30:
            last = time.monotonic()
            flow.deliver()


if __name__ == "__main__":
    main()
