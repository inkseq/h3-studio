"""Closed Langfuse credential contract (h3-studio#770); tracing is off without it.

The deployment hydrates this protected file from one SSM parameter per target.
"""

from dataclasses import dataclass, field

PROFILE = "langfuse--inkseq"
ORIGINS = {"https://us.cloud.langfuse.com", "https://cloud.langfuse.com"}


class LangfuseConfigError(ValueError):
    def __init__(self):
        super().__init__("langfuse_runtime_config_invalid")


@dataclass(frozen=True)
class Credential:
    base_url: str
    public_key: str
    secret_key: str = field(repr=False)


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LangfuseConfigError()
        result[key] = value
    return result


def _key(value, prefix):
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and 16 <= len(value) <= 256
        and value.isascii()
        and not any(c.isspace() or ord(c) < 32 for c in value)
    )


def validate(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"profile", "base_url", "public_key", "secret_key"}
        or value["profile"] != PROFILE
        or value["base_url"] not in ORIGINS
        or not _key(value["public_key"], "pk-lf-")
        or not _key(value["secret_key"], "sk-lf-")
    ):
        raise LangfuseConfigError()
    return Credential(value["base_url"], value["public_key"], value["secret_key"])


def runtime_config(filename):
    from .protected_config import read_protected_json

    return validate(read_protected_json(filename, LangfuseConfigError, pairs=unique))
