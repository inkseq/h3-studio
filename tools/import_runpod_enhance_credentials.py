"""Put the central RunPod profile into one target's enhance SSM parameter (#795).

The endpoint id and billed rate are not secrets and are passed as arguments; the
key comes only from the central profile. Values are never printed.
"""

import argparse
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy.platform.runpod_enhance_credentials import TARGETS, client, import_existing
from studio_platform.central_registry import load_api


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(
            2, "Invalid arguments; use --help. Never pass credentials as arguments.\n"
        )


def main():
    parser = Parser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--rate-microusd-per-s", type=int, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        receipt = import_existing(
            client(),
            load_api,
            args.target,
            endpoint_id=args.endpoint,
            rate_microusd_per_s=args.rate_microusd_per_s,
        )
        print(json.dumps({"target": args.target, "parameter": receipt}))
        return 0
    except Exception as error:
        print(
            "runpod_enhance_import_unconfirmed: "
            + (
                str(error)
                if str(error).startswith("runpod_enhance_")
                else "details suppressed"
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
