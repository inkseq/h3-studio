"""Per-reason counts of refused claims and deferred submissions (#1140 item 4).

Claims and pre-submission checks run inside the controller's DBOS executor and
used to leave no trace when they refused. Each refusal is counted here by its
exact reason; the controller tick reports and resets the counts, so its log
line says why queued work is not moving. Process-local, never persisted.
"""

import threading
from collections import Counter

_lock = threading.Lock()
_counts = Counter()


def count(kind, reason):
    key = "%s:%s" % (kind, reason or "none")
    with _lock:
        _counts[key[:120]] += 1


def drain():
    """The counts since the last call, then start again from zero."""
    with _lock:
        value = dict(_counts)
        _counts.clear()
    return value


def submission_open(control, job):
    """``control.submission_allowed``, counting and logging the reason it is not."""
    import logging

    try:
        if control.submission_allowed(job) is True:
            return True
    except Exception as error:
        count("defer", "check_failed_" + type(error).__name__)
        return False
    reason = "window_closed"
    try:
        if callable(getattr(type(control), "submission_block_reason", None)):
            reason = control.submission_block_reason(job) or reason
    except Exception as error:
        reason = "check_failed_" + type(error).__name__
    # The deferral itself still says worker_window_closed; keep the why.
    count("defer", reason)
    logging.getLogger("studio_platform.worker").info(
        "worker_submission_deferred job=%s reason=%s", job["id"][:8], reason
    )
    return False
