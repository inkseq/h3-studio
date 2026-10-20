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
from .feedback import FeedbackLog
from .demo_adapter import DemoAdapter
from .telegram_api import ORIGIN, TelegramAPI


def load_registry(service, profile):
    root = Path(os.environ.get("AI_REGISTRY_ROOT", r"C:\Users\danmo\Desktop\AI-Registry"))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from api_registry import load_api

    return load_api(service, profile=profile)


def load_token(profile):
    config = load_registry("telegram-bot", profile)
    if config.base_url != ORIGIN or not config.api_key:
        raise SystemExit("registry profile endpoint or token mismatch")
    return config.api_key


def make_adapter(config, home):
    """demo (default), llm (Gemini plans, local demo film) or staging (QA story)."""
    if config.get("adapter", "demo") == "demo":
        return DemoAdapter(home / "work")
    if config["adapter"] == "llm":
        from .llm_adapter import ORIGIN as GEMINI, GeminiClient, LlmAdapter

        llm = config["llm"]
        key = load_registry("gemini", llm["profile"])
        if key.base_url != GEMINI or not key.api_key:
            raise SystemExit("gemini registry profile endpoint or key mismatch")
        client = GeminiClient(key.api_key, llm["model"], usage_log=home / "llm-usage.jsonl")
        return LlmAdapter(home / "work", client)
    if config["adapter"] not in ("staging", "llm+staging"):
        raise SystemExit("adapter must be demo, llm, staging or llm+staging")
    from .staging_adapter import STAGING, LlmStagingAdapter, StagingAdapter, StudioClient

    staging = config["staging"]
    accounts = json.loads(Path(staging["qa_accounts"]).read_text(encoding="utf-8"))
    if accounts.get("base_url") != STAGING:
        raise SystemExit("staging adapter refuses a non-staging account file")
    account = next(a for a in accounts["accounts"] if a["username"] == staging["account"])
    client = StudioClient(STAGING, account["username"], account["password"])
    if config["adapter"] == "staging":
        return StagingAdapter(home / "work", client)
    from .llm_adapter import GeminiClient

    llm = config["llm"]
    key = load_registry("gemini", llm["profile"])
    gemini = GeminiClient(key.api_key, llm["model"], usage_log=home / "llm-usage.jsonl")
    return LlmStagingAdapter(home / "work", client, gemini)


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
    logging.info("running as @%s for %d allowlisted user(s), %s adapter",
                 me.get("username"), len(allowed), config.get("adapter", "demo"))  # fmt: skip
    bot = Bot(api, make_adapter(config, home), Store(home / "state"), allowed,
              home / "media", feedback=FeedbackLog(home / "feedback.jsonl"))  # fmt: skip
    bot.poll_forever()


if __name__ == "__main__":
    main()
