"""Closed runtime credential contract; no imports of local vaults on servers.

The deployment operator supplies a protected file through the existing secret
hydration lane. This module neither provisions it nor changes rollout settings.
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat

PROFILES = {
    "typesafe": ("typesafe--avatar-user-20261008", "https://api.typesafe.ai/v1"),
    "openrouter": ("openrouter--inkseq-chat", "https://openrouter.ai/api/v1"),
}


class InkyConfigError(ValueError):
    def __init__(self):
        super().__init__("inky_runtime_config_invalid")


@dataclass(frozen=True)
class Credential:
    base_url: str
    api_key: str = field(repr=False)


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InkyConfigError()
        result[key] = value
    return result


def validate(value):
    if (
        isinstance(value, dict)
        and set(value) == {"enabled"}
        and value["enabled"] is False
    ):
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"enabled", *PROFILES}
        or value["enabled"] is not True
    ):
        raise InkyConfigError()
    result = {}
    for service, (profile, origin) in PROFILES.items():
        config = value[service]
        if (
            not isinstance(config, dict)
            or set(config) != {"profile", "base_url", "api_key"}
            or config["profile"] != profile
            or config["base_url"] != origin
            or not isinstance(config["api_key"], str)
            or not 16 <= len(config["api_key"]) <= 1024
            or any(c.isspace() or ord(c) < 32 for c in config["api_key"])
        ):
            raise InkyConfigError()
        result[service] = Credential(origin, config["api_key"])
    return result


def runtime_config(filename):
    try:
        path = Path(filename)
        if not path.is_absolute() or ".." in path.parts:
            raise InkyConfigError()
        original = path.lstat()
        if not stat.S_ISREG(original.st_mode):
            raise InkyConfigError()
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > 8192
                or (original.st_dev, original.st_ino) != (info.st_dev, info.st_ino)
                or (
                    os.name != "nt"
                    and (
                        info.st_uid != 0
                        or stat.S_IMODE(info.st_mode) not in {0o400, 0o440}
                    )
                )
            ):
                raise InkyConfigError()
            raw = handle.read(8193)
        if len(raw) != info.st_size:
            raise InkyConfigError()
        return validate(json.loads(raw, object_pairs_hook=unique))
    except InkyConfigError:
        raise
    except Exception:
        raise InkyConfigError() from None
