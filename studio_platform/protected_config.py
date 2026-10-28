"""One reader for root-owned runtime credential files mounted as Compose secrets."""

import json
import os
from pathlib import Path
import stat


def read_protected_json(filename, error, *, pairs=None, limit=8192):
    """The file's JSON, or `error()` for any path, ownership, mode, size or parse problem."""
    try:
        path = Path(filename)
        if not path.is_absolute() or ".." in path.parts:
            raise error()
        original = path.lstat()
        if not stat.S_ISREG(original.st_mode):
            raise error()
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > limit
                or (original.st_dev, original.st_ino) != (info.st_dev, info.st_ino)
                or (
                    os.name != "nt"
                    and (
                        info.st_uid != 0
                        or stat.S_IMODE(info.st_mode) not in {0o400, 0o440}
                    )
                )
            ):
                raise error()
            raw = handle.read(limit + 1)
        if len(raw) != info.st_size:
            raise error()
        return json.loads(raw, object_pairs_hook=pairs)
    except error:
        raise
    except Exception:
        raise error() from None
