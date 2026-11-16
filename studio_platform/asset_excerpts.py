"""One cut per voice excerpt, and a wait for a processing slot instead of a refusal (#1246).

Preflight plans shots in parallel, and every shot of a character asks for the same 3 s
voice excerpt. A fresh cut took ~12 s on staging (2026-11-07), and each parallel cut held
one of the owner's two processing slots: right after the chat drew a story's pictures (whose
storage holds slots too) the check failed with "capacity_unavailable" (2026-11-16, logged
as AssetQuotaExceeded). Here one worker cuts an excerpt while the others wait and reuse it,
and a cut that finds every slot busy waits briefly; nothing is reserved before a slot is
free, so a retry starts clean.
"""

from contextlib import contextmanager
import threading
import time
import uuid

from .storage_asset_journal import AssetQuotaExceeded

# Seconds to wait for a free processing slot before trying again; None gives up.
WAITS = (1, 2, 4, 8, None)
_LOCKS = {}
_GUARD = threading.Lock()


@contextmanager
def _excerpt_lock(key):
    """One cut at a time per excerpt in this process; the lock is dropped when unused."""
    with _GUARD:
        entry = _LOCKS.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _GUARD:
            entry[1] -= 1
            if not entry[1]:
                _LOCKS.pop(key, None)


def derive_shared(service, owner, asset_id, start, end):
    """The finished excerpt if one exists, else one cut of it, shared by concurrent callers."""
    existing = service._existing_derivation(owner, asset_id, start, end)
    if existing is not None:
        return existing
    with _excerpt_lock((owner, asset_id, start, end)):
        existing = service._existing_derivation(owner, asset_id, start, end)
        if existing is not None:
            return existing
        ident = uuid.uuid4().hex
        for wait in WAITS:
            try:
                with service._operation(ident):
                    return service._derive(ident, owner, asset_id, start, end)
            except AssetQuotaExceeded:
                if wait is None:
                    raise
                time.sleep(wait)
