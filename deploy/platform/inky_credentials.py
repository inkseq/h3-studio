"""Inky SSM transport. No key creation, rotation or provider calls.

Staging hydrates per-service parameters into a release secret. Production keeps one
parameter and installs it into the data volume, as Enhance and EveryFrame do.
"""

import json

import boto3
from botocore.config import Config

from studio_platform.inky_config import PROFILES, validate

ACCOUNT = "829135631045"
REGION = "ap-southeast-1"
NAMES = {service: "/sixnine/staging/inky/" + service for service in PROFILES}


def client():
    session = boto3.Session(region_name=REGION)
    config = Config(
        connect_timeout=5,
        read_timeout=20,
        retries={"mode": "standard", "total_max_attempts": 1},
    )
    identity = session.client(
        "sts", endpoint_url="https://sts.ap-southeast-1.amazonaws.com", config=config
    ).get_caller_identity()
    if identity.get("Account") != ACCOUNT:
        raise ValueError("inky_aws_account_invalid")
    return session.client(
        "ssm", endpoint_url="https://ssm.ap-southeast-1.amazonaws.com", config=config
    )


def payload(keys):
    value = {
        "enabled": True,
        **{
            service: {"profile": profile, "base_url": origin, "api_key": keys[service]}
            for service, (profile, origin) in PROFILES.items()
        },
    }
    validate(value)
    return value


PRODUCTION = "/inkseq/production/inky"


def read_production(api):
    rows = api.get_parameters(Names=[PRODUCTION], WithDecryption=True).get(
        "Parameters", []
    )
    if not rows:
        return None
    row = rows[0]
    if (
        len(rows) != 1
        or row.get("Name") != PRODUCTION
        or row.get("Type") != "SecureString"
        or type(row.get("Version")) is not int
    ):
        raise ValueError("inky_parameter_invalid")
    validate(json.loads(row["Value"]))
    return row


def import_production(api, loader):
    """Create or update the parameter from the central profiles; never print it."""
    keys = {}
    for service, (profile, origin) in PROFILES.items():
        cfg = loader(service, profile=profile)
        configured = cfg.base_url or (origin if service == "openai" else None)
        if configured != origin:
            raise ValueError("inky_profile_invalid")
        keys[service] = cfg.api_key
    value = json.dumps(payload(keys), separators=(",", ":"), sort_keys=True)
    row = read_production(api)
    if row is None or row["Value"] != value:
        api.put_parameter(
            Name=PRODUCTION,
            Value=value,
            Type="SecureString",
            KeyId="alias/aws/ssm",
            Tier="Standard",
            Overwrite=row is not None,
        )
    row = read_production(api)
    if row is None or row["Value"] != value:
        raise ValueError("inky_import_readback_mismatch")
    return {k: row[k] for k in ("Name", "Type", "Version")}


def install_production(api, *, data_root="/srv/sixnine/platform-data", app_gid=10001):
    """Write runtime-secrets/inky.json, root:app 0440 under a root 0750 directory."""
    import os
    from pathlib import Path

    row = read_production(api)
    if row is None:
        raise ValueError("inky_parameters_missing")
    directory = Path(data_root) / "runtime-secrets"
    directory.mkdir(mode=0o750, exist_ok=True)
    os.chown(directory, 0, app_gid)
    os.chmod(directory, 0o750)
    path = directory / "inky.json"
    raw = row["Value"].encode()
    if path.exists() and path.read_bytes() == raw:
        return {"path": str(path), "changed": False, "version": row["Version"]}
    temporary = directory / ".tmp-inky.json"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    os.chown(temporary, 0, app_gid)
    os.chmod(temporary, 0o440)
    os.replace(temporary, path)
    return {"path": str(path), "changed": True, "version": row["Version"]}


def read(api):
    response = api.get_parameters(Names=sorted(NAMES.values()), WithDecryption=True)
    rows = {}
    for row in response.get("Parameters", []):
        name = row.get("Name")
        if (
            name not in NAMES.values()
            or name in rows
            or row.get("Type") != "SecureString"
            or type(row.get("Version")) is not int
            or row["Version"] < 1
        ):
            raise ValueError("inky_parameter_invalid")
        rows[name] = row
    if set(rows) | set(response.get("InvalidParameters", [])) != set(NAMES.values()):
        raise ValueError("inky_parameter_invalid")
    return rows


def metadata(rows):
    return [
        {k: row[k] for k in ("Name", "Type", "Version")}
        for _, row in sorted(rows.items())
    ]


def import_existing(api, loader):
    """Create only absent parameters; reconcile same values, never overwrite.

    If a response is lost, stop. The next explicit invocation first reads the
    originals and compares in memory; it cannot rotate or repeat an existing put.
    No secret-drop files, argv keys or environment injection are accepted.
    """
    keys = {}
    for service, (profile, origin) in PROFILES.items():
        cfg = loader(service, profile=profile)
        configured = cfg.base_url or (origin if service == "openai" else None)
        if configured != origin:
            raise ValueError("inky_profile_invalid")
        keys[service] = cfg.api_key
    payload(keys)  # Validate every source before the first possible write.
    rows = read(api)
    for service, name in NAMES.items():
        if name in rows and rows[name].get("Value") != keys[service]:
            raise ValueError("inky_existing_parameter_differs")
    for service, name in NAMES.items():
        if name not in rows:
            api.put_parameter(
                Name=name,
                Value=keys[service],
                Type="SecureString",
                KeyId="alias/aws/ssm",
                Tier="Standard",
                Overwrite=False,
            )
    rows = read(api)
    if set(rows) != set(NAMES.values()) or any(
        rows[name].get("Value") != keys[service] for service, name in NAMES.items()
    ):
        raise ValueError("inky_import_readback_mismatch")
    return metadata(rows)


def hydrate(api, write_runtime, *, target):
    if target != "staging":
        raise ValueError("inky_staging_only")
    rows = read(api)
    if set(rows) != set(NAMES.values()):
        raise ValueError("inky_parameters_missing")
    value = payload({service: rows[name]["Value"] for service, name in NAMES.items()})
    write_runtime("inky", json.dumps(value, separators=(",", ":"), sort_keys=True))
    return metadata(rows)
