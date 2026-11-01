"""Put the owner's EveryFrame key into one target's SSM parameter (#918).

The key comes only from the central registry profile; it is never printed or
passed as an argument. --install runs on the host and writes the app's runtime
secret from the parameter.
"""

import argparse
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy.platform.everyframe_credentials import (  # noqa: E402
    TARGETS,
    client,
    import_existing,
    install,
)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(
            2, "Invalid arguments; use --help. Never pass credentials as arguments.\n"
        )


def main():
    parser = Parser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        if args.install:
            receipt = install(client(), target=args.target)
        else:
            from studio_platform.central_registry import load_api

            receipt = import_existing(client(), load_api, args.target)
        print(json.dumps({"target": args.target, "result": receipt}))
        return 0
    except Exception as error:
        print(
            "everyframe_credentials_unconfirmed: "
            + (
                str(error)
                if str(error).startswith("everyframe_")
                else "details suppressed"
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
