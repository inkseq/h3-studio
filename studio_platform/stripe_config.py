"""Protected, test-only Stripe configuration; never load ambient credentials."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat


@dataclass(frozen=True, repr=False)
class StripeConfig:
    account_id: str
    publishable_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    webhook_secret: str | None = field(default=None, repr=False)


def validate_credentials(value):
    fields = {
        "version",
        "account_id",
        "publishable_key",
        "secret_key",
        "webhook_secret",
    }
    if (
        type(value) is not dict
        or set(value) != fields
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["account_id"], str)
        or not re.fullmatch(r"acct_[A-Za-z0-9]{6,80}", value["account_id"])
        or not isinstance(value["publishable_key"], str)
        or not re.fullmatch(r"pk_test_[A-Za-z0-9]{20,240}", value["publishable_key"])
        or not isinstance(value["secret_key"], str)
        or not re.fullmatch(r"sk_test_[A-Za-z0-9]{20,240}", value["secret_key"])
        or value["webhook_secret"] is not None
        and (
            not isinstance(value["webhook_secret"], str)
            or not re.fullmatch(r"whsec_[A-Za-z0-9]{20,240}", value["webhook_secret"])
        )
    ):
        raise ValueError("stripe_configuration_invalid")
    return StripeConfig(**{key: value[key] for key in fields - {"version"}})


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("stripe_configuration_invalid")
        value[key] = item
    return value


def load_credentials(path):
    if path is None:
        return None
    try:
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError()
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or getattr(before, "st_file_attributes", 0) & 0x400
        ):
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            actual = os.fstat(fd)
            if (
                not stat.S_ISREG(actual.st_mode)
                or actual.st_nlink != 1
                or actual.st_size > 4096
                or (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino)
                or os.name != "nt"
                and (actual.st_uid not in {0, os.geteuid()} or actual.st_mode & 0o027)
            ):
                raise ValueError()
            raw = os.read(fd, 4097)
            if len(raw) != actual.st_size:
                raise ValueError()
        finally:
            os.close(fd)
        return validate_credentials(json.loads(raw, object_pairs_hook=_unique))
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ValueError("stripe_configuration_invalid") from None
