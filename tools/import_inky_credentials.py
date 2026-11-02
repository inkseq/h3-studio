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

from deploy.platform.inky_credentials import (
    client,
    import_existing,
    import_production,
    install_production,
)
from studio_platform.central_registry import load_api


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(
            2, "Invalid arguments; use --help. Never pass credentials as arguments.\n"
        )


def main():
    parser = Parser(description=__doc__)
    parser.add_argument("--apply-staging", action="store_true")
    parser.add_argument("--apply-production", action="store_true")
    parser.add_argument("--install-production", action="store_true")
    args = parser.parse_args()
    if args.apply_staging + args.apply_production + args.install_production != 1:
        parser.error("Explicit adoption required")
    logging.disable(logging.CRITICAL)
    try:
        if args.apply_production or args.install_production:
            receipt = (
                import_production(client(), load_api)
                if args.apply_production
                else install_production(client())
            )
            print(json.dumps({"target": "production", "result": receipt}))
            return 0
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
