#!/usr/bin/env python3
"""Host timer half of the lost-host GPU stop (#816); runs as root every minute.

publish: copy the controller's stop list to an SSM parameter the off-host
stopper reads. Skips an unchanged list unless the last copy is 5 minutes old,
so updated_at keeps proving the host was alive. Accepts list versions 1 and 2.
sync-vast-key, sync-runpod-key, sync-lium-key: one-shot copy of one provider's
key from the dstack server config into that provider's stopper secret, without
printing it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

REGION = "ap-southeast-1"
SOURCE = Path("/srv/sixnine/dstack-runtime/host-loss-stoppable.json")
STATE = Path("/var/lib/sixnine-host-loss/last.json")
DSTACK_CONFIG = Path("/run/sixnine-secrets/dstack-server-config.yml")
REFRESH_SECONDS = 300
# Instance id forms, as in studio_platform/host_loss.py (this file runs alone).
FORMS = {
    "vast": re.compile(r"[0-9]{1,20}"),
    "runpod": re.compile(r"[A-Za-z0-9]{1,64}"),
    "lium": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}"),
}
# Provider -> dstack server config backend type.
BACKEND_TYPES = {"vast": "vastai", "runpod": "runpod", "lium": "lium"}


def parameter(environment):
    return f"/sixnine/{environment}/host-loss/stoppable"


def secret(environment, provider="vast"):
    if provider == "vast":
        return f"/sixnine/{environment}/vast-host-loss"  # Existing name; kept.
    return f"/inkseq/{environment}/{provider}-host-loss"


def _entry_valid(item, version):
    if not isinstance(item, dict):
        return False
    provider = item.get("provider") if version == 2 else "vast"
    instance_id = item.get("instance_id")
    return (
        provider in FORMS
        and isinstance(instance_id, str)
        and FORMS[provider].fullmatch(instance_id) is not None
    )


def valid(value):
    return (
        isinstance(value, dict)
        and value.get("version") in (1, 2)
        and type(value.get("updated_at")) in (int, float)
        and isinstance(value.get("instances"), list)
        and all(_entry_valid(item, value["version"]) for item in value["instances"])
    )


def publish(environment, ssm, *, now=None):
    now = time.time() if now is None else now
    try:
        value = json.loads(SOURCE.read_text())
    except (OSError, ValueError):
        return "missing"  # Controller not writing yet (or mid-deploy).
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


def provider_key(text, provider="vast"):
    """One provider's backend api_key from dstack's server config.yml."""
    backend_type = BACKEND_TYPES[provider]
    try:
        # The rendered config is JSON-compatible YAML (migration_secrets_aws).
        config = json.loads(text)
    except ValueError:
        import yaml

        config = yaml.safe_load(text)
    config = config or {}
    keys = {
        backend["creds"]["api_key"]
        for project in config.get("projects", [])
        for backend in project.get("backends", [])
        if backend.get("type") == backend_type
        and isinstance(backend.get("creds"), dict)
        and isinstance(backend["creds"].get("api_key"), str)
        and backend["creds"]["api_key"]
    }
    if len(keys) != 1:
        raise SystemExit(f"{provider}_key_not_unique")
    return keys.pop()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("publish", *(f"sync-{name}-key" for name in BACKEND_TYPES)),
    )
    parser.add_argument(
        "--environment", choices=("staging", "production"), required=True
    )
    args = parser.parse_args(argv)
    import boto3

    if args.command == "publish":
        print(publish(args.environment, boto3.client("ssm", region_name=REGION)))
        return 0
    provider = args.command.removeprefix("sync-").removesuffix("-key")
    key = provider_key(DSTACK_CONFIG.read_text(), provider)
    boto3.client("secretsmanager", region_name=REGION).put_secret_value(
        SecretId=secret(args.environment, provider),
        SecretString=json.dumps({"api_key": key}),
    )
    print("synced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
