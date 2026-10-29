#!/usr/bin/env python3
"""Host timer half of the lost-host GPU stop (#816); runs as root every minute.

publish: copy the controller's stop list to an SSM parameter the off-host
stopper reads. Skips an unchanged list unless the last copy is 5 minutes old,
so updated_at keeps proving the host was alive.
sync-vast-key: one-shot copy of the dstack server's Vast key into the
stopper's secret, without printing it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

REGION = "ap-southeast-1"
SOURCE = Path("/srv/sixnine/dstack-runtime/host-loss-stoppable.json")
STATE = Path("/var/lib/sixnine-host-loss/last.json")
DSTACK_CONFIG = Path("/run/sixnine-secrets/dstack-server-config.yml")
REFRESH_SECONDS = 300


def parameter(environment):
    return f"/sixnine/{environment}/host-loss/stoppable"


def secret(environment):
    return f"/sixnine/{environment}/vast-host-loss"


def valid(value):
    return (
        isinstance(value, dict)
        and value.get("version") == 1
        and type(value.get("updated_at")) in (int, float)
        and isinstance(value.get("instances"), list)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("instance_id"), str)
            and item["instance_id"].isdigit()
            for item in value["instances"]
        )
    )


def publish(environment, ssm, *, now=None):
    now = time.time() if now is None else now
    value = json.loads(SOURCE.read_text())
    if not valid(value) or now - value["updated_at"] > 600:
        return (
            "stale_or_invalid"  # Never refresh a list the controller stopped writing.
        )
    body = json.dumps(value, sort_keys=True)
    try:
        last = json.loads(STATE.read_text())
    except (OSError, ValueError):
        last = {}
    instances = json.dumps(value["instances"], sort_keys=True)
    if last.get("instances") == instances and now - last.get("at", 0) < REFRESH_SECONDS:
        return "unchanged"
    ssm.put_parameter(
        Name=parameter(environment), Value=body, Type="String", Overwrite=True
    )
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"instances": instances, "at": now}))
    return "published"


def vast_key(text):
    """The vastai backend api_key from dstack's server config.yml."""
    import yaml

    config = yaml.safe_load(text) or {}
    keys = {
        backend["creds"]["api_key"]
        for project in config.get("projects", [])
        for backend in project.get("backends", [])
        if backend.get("type") == "vastai"
        and isinstance(backend.get("creds"), dict)
        and isinstance(backend["creds"].get("api_key"), str)
    }
    if len(keys) != 1:
        raise SystemExit("vast_key_not_unique")
    return keys.pop()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("publish", "sync-vast-key"))
    parser.add_argument(
        "--environment", choices=("staging", "production"), required=True
    )
    args = parser.parse_args(argv)
    import boto3

    if args.command == "publish":
        print(publish(args.environment, boto3.client("ssm", region_name=REGION)))
        return 0
    boto3.client("secretsmanager", region_name=REGION).put_secret_value(
        SecretId=secret(args.environment),
        SecretString=json.dumps({"api_key": vast_key(DSTACK_CONFIG.read_text())}),
    )
    print("synced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
