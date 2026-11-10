"""Per-reason counts of refused claims and deferred submissions (#1140 item 4).

Claims and pre-submission checks run inside the controller's DBOS executor and
used to leave no trace when they refused. Each refusal is counted here by its
exact reason; the controller tick reports and resets the counts, so its log
line says why queued work is not moving. Counts that name a worker are also
kept per worker; the capacity timeline drains those into each machine's
persisted refusal summary (capacity_timeline.py). Process-local and bounded.
"""

import re
import threading
from collections import Counter

_lock = threading.Lock()
_counts = Counter()
_by_worker = {}
# A process nobody drains (no controller) must not grow without bound.
MAX_WORKERS = 256


_STATIC_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")


def static_error_code(error):
    """A short static code from our own error types for a log line, else "-".

    DstackError, BudgetExceeded and Conflict carry static codes only. Anything
    else, or any argument that is not a plain snake_case code, may hold
    provider text and is not logged (#1140: a stuck node logged no reason).
    """
    from .dstack_request import DstackError
    from .repository import BudgetExceeded, Conflict

    if not isinstance(error, (DstackError, BudgetExceeded, Conflict)) or not error.args:
        return "-"
    code = error.args[0]
    if isinstance(code, str) and _STATIC_CODE.fullmatch(code):
        return code
    return "-"


def count(kind, reason, worker_id=None):
    key = ("%s:%s" % (kind, reason or "none"))[:120]
    with _lock:
        _counts[key] += 1
        if isinstance(worker_id, str) and worker_id:
            counts = _by_worker.get(worker_id)
            if counts is None and len(_by_worker) < MAX_WORKERS:
                counts = _by_worker[worker_id] = Counter()
            if counts is not None:
                counts[key] += 1


def drain():
    """The counts since the last call, then start again from zero."""
    with _lock:
        value = dict(_counts)
        _counts.clear()
    return value


def drain_workers():
    """Per-worker counts since the last call, then start again from zero."""
    with _lock:
        value = {worker: dict(counts) for worker, counts in _by_worker.items()}
        _by_worker.clear()
    return value


def submission_open(control, job):
    """``control.submission_allowed``, counting and logging the reason it is not."""
    import logging

    worker_id = job.get("lease_worker_id") if isinstance(job, dict) else None
    try:
        if control.submission_allowed(job) is True:
            return True
    except Exception as error:
        count("defer", "check_failed_" + type(error).__name__, worker_id)
        return False
    reason = "window_closed"
    try:
        if callable(getattr(type(control), "submission_block_reason", None)):
            reason = control.submission_block_reason(job) or reason
    except Exception as error:
        reason = "check_failed_" + type(error).__name__
    # The deferral itself still says worker_window_closed; keep the why.
    count("defer", reason, worker_id)
    logging.getLogger("studio_platform.worker").info(
        "worker_submission_deferred job=%s reason=%s", job["id"][:8], reason
    )
    return False
