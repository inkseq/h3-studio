"""Explicit staging adoption of existing central Inky keys; never read a drop file.

Run only after the final Tier2 PR review and owner credential-adoption authority.
This prepares SSM parameters; it does not enable the evaluator or install IAM.
"""

import argparse
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy.platform.inky_credentials import client, import_existing
from studio_platform.central_registry import load_api


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(
            2, "Invalid arguments; use --help. Never pass credentials as arguments.\n"
        )


def main():
    parser = Parser(description=__doc__)
    parser.add_argument("--apply-staging", action="store_true")
    args = parser.parse_args()
    if not args.apply_staging:
        parser.error("Explicit adoption required")
    logging.disable(logging.CRITICAL)
    try:
        receipt = import_existing(client(), load_api)
        print(json.dumps({"target": "staging", "parameters": receipt}))
        return 0
    except Exception:
        print(
            "inky_import_unconfirmed: reconcile original parameters before any retry; details suppressed",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
