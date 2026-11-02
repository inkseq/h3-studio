"""Private backup file validation and bounded local serialization."""

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from .backup_schema import MIB, BackupError
from .storage import _check_ancestors, _no_links


def _absolute(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise BackupError("backup_paths_must_be_explicit_absolute")
    _check_ancestors(path)
    return path


def _regular(path, *, max_bytes=None):
    path = _absolute(path)
    info = _no_links(path)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (max_bytes is not None and info.st_size > max_bytes)
    ):
        raise BackupError("backup_file_type_or_size_invalid")
    return path


def _write_json(path, value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode("utf-8")
    if len(encoded) > 16 * MIB:
        raise BackupError("backup_manifest_too_large")
    with path.open("xb") as handle:
        if os.name != "nt":
            os.fchmod(handle.fileno(), 0o600)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _digest(path):
    digest = hashlib.sha256()
    with _regular(path).open("rb") as source:
        for block in iter(lambda: source.read(MIB), b""):
            digest.update(block)
    return digest.hexdigest()


def _open_sqlite_readonly(path):
    db = sqlite3.connect(
        _regular(path, max_bytes=256 * MIB).as_uri() + "?mode=ro", uri=True, timeout=5
    )
    db.execute("PRAGMA trusted_schema=OFF")
    db.execute("PRAGMA query_only=ON")
    return db
