"""Put the central Langfuse profile into one target's SSM parameter (h3-studio#770).

Never reads drop files or takes keys as arguments; values are never printed.
"""

import argparse
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy.platform.langfuse_credentials import TARGETS, client, import_existing
from studio_platform.central_registry import load_api


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(
            2, "Invalid arguments; use --help. Never pass credentials as arguments.\n"
        )


def main():
    parser = Parser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        receipt = import_existing(client(), load_api, args.target)
        print(json.dumps({"target": args.target, "parameter": receipt}))
        return 0
    except Exception as error:
        print(
            "langfuse_import_unconfirmed: "
            + (str(error) if str(error).startswith("langfuse_") else "details suppressed"),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
