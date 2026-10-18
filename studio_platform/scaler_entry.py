"""Select a reviewed controller from a bounded operator file; default is disabled."""

import argparse
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=Path)
    args, _ = parser.parse_known_args(argv)
    try:
        mode = None
        if args.config is not None:
            if args.config.is_symlink() or not args.config.is_absolute():
                raise ValueError
            with args.config.open("rb") as source:
                raw = source.read(65537)
            if len(raw) > 65536:
                raise ValueError
            mode = json.loads(raw).get("service_mode")
        if mode == "on-demand":
            from .on_demand_scaler import main as run
        elif mode is None:
            from .production_scaler import main as run
        else:
            raise ValueError
        return run(argv)
    except Exception:
        print(
            json.dumps(
                {
                    "phase": "controller_configuration_invalid",
                    "provider_calls_enabled": False,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
