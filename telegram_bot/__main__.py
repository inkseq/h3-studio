"""Run the demo bot locally: python -m telegram_bot --home <dir>

<dir>/config.json holds {"registry_profile": ..., "allowed_user_ids": [...]}.
The token is read in memory from the central AI-Registry and never printed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys

from .bot import COMMANDS, Bot, Store
from .demo_adapter import DemoAdapter
from .telegram_api import ORIGIN, TelegramAPI


def load_token(profile):
    root = Path(os.environ.get("AI_REGISTRY_ROOT", r"C:\Users\danmo\Desktop\AI-Registry"))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from api_registry import load_api

    config = load_api("telegram-bot", profile=profile)
    if config.base_url != ORIGIN or not config.api_key:
        raise SystemExit("registry profile endpoint or token mismatch")
    return config.api_key


def main(argv=None):
    parser = argparse.ArgumentParser(prog="telegram_bot")
    parser.add_argument("--home", default=".local/telegram-bot")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines carry the URL
    home = Path(args.home)
    config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    allowed = [int(x) for x in config.get("allowed_user_ids", [])]
    if not allowed:
        raise SystemExit("allowed_user_ids is empty; refusing to run")
    api = TelegramAPI(load_token(config["registry_profile"]))
    me = api.get_me()
    api.set_commands(COMMANDS)
    logging.info("running as @%s for %d allowlisted user(s), demo adapter",
                 me.get("username"), len(allowed))  # fmt: skip
    bot = Bot(api, DemoAdapter(home / "work"), Store(home / "state"), allowed,
              home / "media")  # fmt: skip
    bot.poll_forever()


if __name__ == "__main__":
    main()
