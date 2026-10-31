"""Cold-start backstop credential transport over SSM (#918), like runpod_enhance.

One SecureString per deployment target holds the `everyframe.json` contract of
studio_platform.everyframe_backend. The vendor is not yet a central registry
service, so the import reads the owner's local drop file in memory; the host
installs the parameter into the app's data volume. Values are never printed.
"""

import json

from deploy.platform.langfuse_credentials import client  # noqa: F401 -- same account guard
from studio_platform.everyframe_backend import FILENAME, validate_config

TARGETS = ("staging", "production")
VARIABLE = "EVERYFRAME_API_KEY"


def name(target):
    if target not in TARGETS:
        raise ValueError("everyframe_target_invalid")
    return "/inkseq/" + target + "/everyframe"


def _validate(raw):
    """The exact contract the app reads."""
    try:
        validate_config(json.loads(raw))
    except ValueError:
        raise ValueError("everyframe_value_invalid") from None


def read(api, target):
    parameter = name(target)
    rows = api.get_parameters(Names=[parameter], WithDecryption=True).get(
        "Parameters", []
    )
    if not rows:
        return None
    row = rows[0]
    if (
        len(rows) != 1
        or row.get("Name") != parameter
        or row.get("Type") != "SecureString"
        or type(row.get("Version")) is not int
    ):
        raise ValueError("everyframe_parameter_invalid")
    _validate(row["Value"])
    return row


def key_from_env_file(path):
    from pathlib import Path

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith(VARIABLE + "="):
            value = line.split("=", 1)[1].strip().strip('"')
            if value:
                return value
    raise ValueError("everyframe_drop_file_missing_key")


def import_existing(api, target, *, env_file):
    """Create or update the parameter from the drop file; never print it."""
    value = json.dumps(
        {"api_key": key_from_env_file(env_file)}, separators=(",", ":")
    )
    _validate(value)
    row = read(api, target)
    if row is None or row["Value"] != value:
        api.put_parameter(
            Name=name(target),
            Value=value,
            Type="SecureString",
            KeyId="alias/aws/ssm",
            Tier="Standard",
            Overwrite=row is not None,
        )
    row = read(api, target)
    if row is None or row["Value"] != value:
        raise ValueError("everyframe_import_readback_mismatch")
    return {k: row[k] for k in ("Name", "Type", "Version")}


def install(api, *, target, data_root="/srv/sixnine/platform-data", app_gid=10001):
    """Write runtime-secrets/everyframe.json, root:app 0440 under a root 0750 dir."""
    import os
    from pathlib import Path

    row = read(api, target)
    if row is None:
        raise ValueError("everyframe_parameter_missing")
    directory = Path(data_root) / "runtime-secrets"
    directory.mkdir(mode=0o750, exist_ok=True)
    os.chown(directory, 0, app_gid)
    os.chmod(directory, 0o750)
    path = directory / FILENAME
    raw = row["Value"].encode()
    if path.exists() and path.read_bytes() == raw:
        return {"path": str(path), "changed": False, "version": row["Version"]}
    temporary = directory / (".tmp-" + FILENAME)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    os.chown(temporary, 0, app_gid)
    os.chmod(temporary, 0o440)
    os.replace(temporary, path)
    return {"path": str(path), "changed": True, "version": row["Version"]}
