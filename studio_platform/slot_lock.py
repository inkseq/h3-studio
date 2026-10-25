"""OS-held endpoint/supervisor exclusion, independent of inference runners.

Callers must share the same existing directory. No directory creation, GPU
operation or distributed-lease substitute is implied by acquiring this lock.
"""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path


@contextmanager
def slot_lock(directory, key):
    """OS-held local lock; all runners for an endpoint must share this directory."""
    path = Path(directory) / (hashlib.sha256(key.encode()).hexdigest() + ".slot")
    with path.open("a+b") as handle:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"0")
                    handle.flush()
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
