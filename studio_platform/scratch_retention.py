"""Bounded post-commit R2 scratch removal, never object-store/history cleanup.

Callers hold their original operation/slot lock and persist the exact manifest
before deletion. A crash can leave files missing but quota conservatively high;
the same receipt reconciles that once, without deleting unrelated files.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

from .storage import IntegrityError, _check_ancestors, _no_links, _part, _sync_directory

POLICY = "remote_verified_v1"


def fingerprint(path, max_bytes):
    path = Path(path)
    _check_ancestors(path.parent)
    before = _no_links(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise IntegrityError("r2_scratch_not_regular")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise IntegrityError("r2_scratch_changed")
        count, digest = 0, hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            count += len(chunk)
            if count > max_bytes:
                raise IntegrityError("r2_scratch_exceeds_limit")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_nlink)
    if identity(before) != identity(after) or count != after.st_size:
        raise IntegrityError("r2_scratch_changed")
    return dict(size_bytes=count, sha256=digest.hexdigest(), device=after.st_dev,
                inode=after.st_ino, mtime_ns=after.st_mtime_ns)


def manifest(directory, paths, max_bytes):
    directory = Path(directory)
    _check_ancestors(directory)
    result = {}
    for path in map(Path, paths):
        if path.parent != directory or path.name in result:
            raise IntegrityError("r2_scratch_path_mismatch")
        _part(path.name)
        result[path.name] = fingerprint(path, max_bytes)
    return result


def remove_manifest(directory, files, max_bytes):
    directory = Path(directory)
    _check_ancestors(directory)
    if not directory.exists():
        return 0
    # Validate the whole bounded set first. Unexpected entries are never swept.
    if {path.name for path in directory.iterdir()} - set(files):
        raise IntegrityError("r2_scratch_unexpected_file")
    present = []
    for name, expected in files.items():
        _part(name)
        path = directory / name
        if _no_links(path, missing_ok=True) is None:
            continue
        if fingerprint(path, max_bytes) != expected:
            raise IntegrityError("r2_scratch_manifest_mismatch")
        present.append(path)
    for path in present:
        # Private server-owned directories are already fenced by the caller.
        if fingerprint(path, max_bytes) != files[path.name]:
            raise IntegrityError("r2_scratch_changed")
        path.unlink()
        _sync_directory(directory)
    return 0
