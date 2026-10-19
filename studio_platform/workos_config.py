"""Small shared WorkOS credential schema; no SDK, environment or network access."""

import json
import os
from pathlib import Path
import re
import stat


def validate_credentials(value):
    """Validate the closed runtime shape without reflecting any supplied value."""
    if (
        type(value) is not dict
        or set(value) != {"client_id", "api_key"}
        or not isinstance(value["client_id"], str)
        or not re.fullmatch(r"client_[A-Za-z0-9]{12,80}", value["client_id"])
        or not isinstance(value["api_key"], str)
        or not re.fullmatch(r"sk_[A-Za-z0-9_-]{20,240}", value["api_key"])
    ):
        raise ValueError("workos_configuration_invalid")
    return value


def unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("workos_configuration_invalid")
        value[key] = item
    return value


def load_credentials(path):
    """Load an explicit private mount, or leave the optional integration unset."""
    if path is None:
        return None
    try:
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError()
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or getattr(before, 'st_file_attributes', 0) & 0x400
        ):
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            actual = os.fstat(fd)
            if (
                not stat.S_ISREG(actual.st_mode)
                or actual.st_nlink != 1
                or actual.st_size > 4096
                or (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino)
                or os.name != 'nt'
                and (actual.st_uid not in {0, os.geteuid()} or actual.st_mode & 0o027)
            ):
                raise ValueError()
            raw = os.read(fd, 4097)
            if len(raw) != actual.st_size:
                raise ValueError()
        finally:
            os.close(fd)
        return validate_credentials(json.loads(raw, object_pairs_hook=unique_pairs))
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ValueError('workos_configuration_invalid') from None
