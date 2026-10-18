"""Dependency-free validation shared by the app and protected host hydration."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat

ORIGIN = "https://generativelanguage.googleapis.com"
PROFILE = "gemini--user-supplied"


class GoogleTitleConfigError(ValueError):
    code = "title_config_invalid"

    def __init__(self):
        super().__init__("title_config_invalid")


@dataclass(frozen=True)
class GoogleTitleConfig:
    base_url: str
    api_key: str = field(repr=False)


def validate_config(value):
    if (
        isinstance(value, dict)
        and set(value) == {"enabled"}
        and value["enabled"] is False
    ):
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"enabled", "service", "profile", "base_url", "api_key"}
        or value.get("enabled") is not True
        or value.get("service") != "gemini"
        or value.get("profile") != PROFILE
        or value.get("base_url") != ORIGIN
        or not isinstance(value.get("api_key"), str)
        or not 20 <= len(value["api_key"]) <= 512
        or any(c.isspace() or ord(c) < 32 for c in value["api_key"])
    ):
        raise GoogleTitleConfigError()
    return GoogleTitleConfig(base_url=ORIGIN, api_key=value["api_key"])


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GoogleTitleConfigError()
        result[key] = value
    return result


def runtime_config(filename):
    path = Path(filename)
    if not path.is_absolute():
        raise GoogleTitleConfigError()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > 4096
                or (os.name != "nt" and (info.st_uid != 0 or info.st_mode & 0o037))
            ):
                raise GoogleTitleConfigError()
            raw = handle.read(4097)
        if len(raw) > 4096:
            raise GoogleTitleConfigError()
        return validate_config(json.loads(raw, object_pairs_hook=unique_pairs))
    except GoogleTitleConfigError:
        raise
    except Exception:
        raise GoogleTitleConfigError() from None
