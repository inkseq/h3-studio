"""Closed runtime credential contract; no imports of local vaults on servers.

The deployment operator supplies a protected file through the existing secret
hydration lane. This module neither provisions it nor changes rollout settings.
"""

from dataclasses import dataclass, field

PROFILES = {
    "typesafe": ("typesafe--avatar-user-20261008", "https://api.typesafe.ai/v1"),
    "openai": ("openai--inkseq", "https://api.openai.com/v1"),
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
    from .protected_config import read_protected_json

    return validate(read_protected_json(filename, InkyConfigError, pairs=unique))
