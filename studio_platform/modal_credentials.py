"""Existing Modal tokens in a protected CPU-only file, never a GPU launch spec."""

import os
import stat

KEYS = {'MODAL_TOKEN_ID', 'MODAL_TOKEN_SECRET'}
FILENAME = 'modal-credentials.json'


class CredentialFileError(ValueError):
    def __init__(self):
        super().__init__('modal_credential_file_unprotected')


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
    from .protected_config import read_protected_json

    path = _path(path)
    if path.name != FILENAME:
        raise ValueError('modal_credential_path_invalid')
    return validate(
        read_protected_json(
            path,
            CredentialFileError,
            pairs=unique_pairs,
            limit=16384,
            check_file=protected_file,
        )
    )
