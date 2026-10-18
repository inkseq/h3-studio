"""Local OS-held locks for an asset's receive/prepare/publish/release operation.

This is defense against live local writers, not proof that a prior host or an
older version has stopped. Never delete lock files while writers may exist.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat

from .storage import IntegrityError, _check_ancestors


@contextmanager
def asset_operation_lock(directory, asset_id):
    if not isinstance(asset_id, str) or not re.fullmatch(r"[0-9a-f]{32}", asset_id):
        raise IntegrityError("素材操作引用无效")
    root = Path(directory)
    _check_ancestors(root)
    path = root / (asset_id + ".lock")
    # O_NOFOLLOW on Linux, lstat/fstat checks on both systems. The directory is
    # private and operator-owned; no user-supplied lock root or symlink target.
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise IntegrityError("素材操作锁类型无效")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    with os.fdopen(fd, "r+b", buffering=0) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise IntegrityError("素材操作锁类型无效")
        try:
            if os.name == "nt":
                import msvcrt

                if info.st_size == 0:
                    handle.write(b"0")
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    pass
            yield acquired
        finally:
            if acquired:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
