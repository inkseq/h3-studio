"""Langfuse tracing credential transport over SSM (h3-studio#770).

One SecureString parameter per deployment target holds the validated JSON contract
from studio_platform.langfuse_config. The host installs it into the app's data
volume (`install`), outside the release helpers. No key creation or rotation.
"""

import json

import boto3
from botocore.config import Config

from studio_platform.langfuse_config import ORIGINS, PROFILE, unique, validate

ACCOUNT = "829135631045"
REGION = "ap-southeast-1"
TARGETS = ("staging", "production")


def name(target):
    if target not in TARGETS:
        raise ValueError("langfuse_target_invalid")
    return "/sixnine/" + target + "/langfuse"


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
        raise ValueError("langfuse_aws_account_invalid")
    return session.client(
        "ssm", endpoint_url="https://ssm.ap-southeast-1.amazonaws.com", config=config
    )


def payload(base_url, public_key, secret_key):
    value = {
        "profile": PROFILE,
        "base_url": base_url.rstrip("/"),
        "public_key": public_key,
        "secret_key": secret_key,
    }
    validate(value)
    return value


def read(api, target):
    parameter = name(target)
    response = api.get_parameters(Names=[parameter], WithDecryption=True)
    rows = response.get("Parameters", [])
    if not rows:
        return None
    row = rows[0]
    if (
        len(rows) != 1
        or row.get("Name") != parameter
        or row.get("Type") != "SecureString"
        or type(row.get("Version")) is not int
    ):
        raise ValueError("langfuse_parameter_invalid")
    validate(json.loads(row["Value"], object_pairs_hook=unique))
    return row


def import_existing(api, loader, target):
    """Create the parameter once from the central profile; same value is a no-op."""
    cfg = loader("langfuse", profile=PROFILE)
    if (cfg.base_url or "").rstrip("/") not in ORIGINS:
        raise ValueError("langfuse_profile_invalid")
    value = json.dumps(
        payload(
            cfg.base_url, cfg.env["LANGFUSE_PUBLIC_KEY"], cfg.env["LANGFUSE_SECRET_KEY"]
        ),
        separators=(",", ":"),
        sort_keys=True,
    )
    row = read(api, target)
    if row is not None and row["Value"] != value:
        raise ValueError("langfuse_existing_parameter_differs")
    if row is None:
        api.put_parameter(
            Name=name(target),
            Value=value,
            Type="SecureString",
            KeyId="alias/aws/ssm",
            Tier="Standard",
            Overwrite=False,
        )
    row = read(api, target)
    if row is None or row["Value"] != value:
        raise ValueError("langfuse_import_readback_mismatch")
    return {k: row[k] for k in ("Name", "Type", "Version")}


def install(api, *, target, data_root="/srv/sixnine/platform-data", app_gid=10001):
    """Write the app's tracing credential into its data volume, root:app 0440.

    The app (uid/gid 10001) can read it but not replace it: the directory is
    root-owned 0750. Same bytes are a no-op; nothing is printed but metadata.
    """
    import os
    from pathlib import Path

    row = read(api, target)
    if row is None:
        raise ValueError("langfuse_parameter_missing")
    directory = Path(data_root) / "runtime-secrets"
    directory.mkdir(mode=0o750, exist_ok=True)
    os.chown(directory, 0, app_gid)
    os.chmod(directory, 0o750)
    path = directory / "langfuse.json"
    raw = row["Value"].encode()
    if path.exists() and path.read_bytes() == raw:
        return {"path": str(path), "changed": False, "version": row["Version"]}
    temporary = directory / ".langfuse.json.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    os.chown(temporary, 0, app_gid)
    os.chmod(temporary, 0o440)
    os.replace(temporary, path)
    return {"path": str(path), "changed": True, "version": row["Version"]}
