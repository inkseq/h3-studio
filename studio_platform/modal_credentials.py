"""Existing Modal tokens in a protected CPU-only file, never a GPU launch spec."""

import json
import os
import stat

KEYS = {'MODAL_TOKEN_ID', 'MODAL_TOKEN_SECRET'}
FILENAME = 'modal-credentials.json'


def validate(value):
    if (
        type(value) is not dict
        or set(value) != KEYS
        or any(
            not isinstance(v, str)
            or not 1 <= len(v) <= 4096
            or not v.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in v)
            for v in value.values()
        )
    ):
        raise ValueError('modal_existing_credentials_invalid')
    return value


def unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('modal_existing_credentials_invalid')
        value[key] = item
    return value


def protected_file(info, *, posix=None):
    posix = os.name == 'posix' if posix is None else posix
    return bool(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and 0 < info.st_size <= 16384
        and (
            not posix
            or (
                info.st_uid == 0
                and info.st_gid == 10001
                and stat.S_IMODE(info.st_mode) == 0o440
            )
        )
    )


def read(path):
    """The host owns the inode; the controller has only group read access."""
    from .dstack_factory import _path

    path = _path(path)
    if path.name != FILENAME:
        raise ValueError('modal_credential_path_invalid')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if not protected_file(info):
            raise ValueError('modal_credential_file_unprotected')
        raw = os.read(fd, 16385)
        after = os.fstat(fd)
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or len(raw) != info.st_size:
            raise ValueError('modal_credential_file_changed')
        return validate(json.loads(raw, object_pairs_hook=unique_pairs))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('modal_existing_credentials_invalid') from None
    finally:
        os.close(fd)
