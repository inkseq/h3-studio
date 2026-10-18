"""Finite controller worker: scoped admission, durable drain, serial collection.

No provider operations or import-time IO. All GPU slots of one CPU controller
must use the SAME protected collection_lock_dir, including after restart.
"""

from contextlib import contextmanager
import math
from pathlib import Path
import signal
import stat
import threading
import time

from .worker import WorkerRunner, _slot_lock


@contextmanager
def collection_slot(directory, heartbeat, *, poll_interval_s=1):
    """One CPU collection at a time across processes; waiting keeps leases live.

    No lease or durable state is cleared on timeout/drain. The OS releases this
    lock when its owning process exits; an unknown GPU attempt stays in SQL.
    """
    directory = Path(directory)
    if (
        not directory.is_absolute()
        or not callable(heartbeat)
        or not math.isfinite(poll_interval_s)
        or not 0.01 <= poll_interval_s <= 30
    ):
        raise ValueError('invalid_collection_lock_configuration')
    # This is operator-owned control state, never a user-provided media path.
    for parent in (directory, *directory.parents):
        if parent.is_symlink():
            raise ValueError('collection_lock_directory_must_not_be_link')
    directory.mkdir(parents=True, exist_ok=True)
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise ValueError('collection_lock_directory_invalid')
    while True:
        heartbeat()
        with _slot_lock(directory, 'production-cpu-collection-v1') as acquired:
            if acquired:
                heartbeat()
                yield
                return
        time.sleep(poll_interval_s)


class DrainSafeRunner(WorkerRunner):
    """Drain prevents new work; already bound attempts still reconcile/collect.

    job_filter is an optional SQL predicate used before candidate selection.
    job_allowed MUST additionally check the full immutable job, under its row
    lock before claim. New scope/deadline checks never reject an existing bound
    attempt's collection. WorkerControl still checks its original model/pool,
    device ownership and provider deadline for every new claim/submission.
    """

    def __init__(
        self,
        *args,
        stop_new,
        job_allowed,
        collection_lock_dir,
        job_filter=None,
        **kwargs,
    ):
        if not callable(stop_new) or not callable(job_allowed):
            raise ValueError('finite_worker_callbacks_required')
        directory = Path(collection_lock_dir)
        if not directory.is_absolute():
            raise ValueError('absolute_shared_collection_lock_required')
        self._stop_new = threading.Event()
        self._stop_new_callback, self._job_allowed_callback = stop_new, job_allowed
        self._job_filter, self.collection_lock_dir = job_filter, directory
        self._worker_id = None
        super().__init__(*args, **kwargs)
        if self.control is None:
            raise ValueError('finite_worker_requires_durable_control')

    def drain(self):
        # Do not set WorkerRunner._drain: that flag also stops recovery turns.
        self._stop_new.set()

    def stopped(self):
        try:
            if self._stop_new.is_set():
                return True
            stopped = self._stop_new_callback()
            external = False if self.stop_requested is None else self.stop_requested()
            # An unavailable/non-boolean control response cannot authorize new
            # work. Existing bound attempts keep their recovery path below.
            return (
                type(stopped) is not bool
                or type(external) is not bool
                or stopped
                or external
            )
        except Exception:
            return True

    def _check_external_stop(self):
        if self.stopped() and self._worker_id:
            self.control.drain(self._worker_id)

    def _allowed_new_job(self, job):
        try:
            return not self.stopped() and self._job_allowed_callback(job) is True
        except Exception:
            return False

    def _submission_allowed(self, job):
        return self._allowed_new_job(job) and super()._submission_allowed(job)

    def _claim(self, worker_id, pool, *, purpose):
        if purpose != 'generate':
            # The original worker's current_job_id restricts this to its own
            # prior attempt; never strand it because a new deadline expired.
            return super()._claim(worker_id, pool, purpose=purpose)
        self._check_external_stop()
        return self.control.claim(
            worker_id,
            pool,
            lease_seconds=900,
            purpose=purpose,
            job_filter=self._job_filter,
            job_allowed=self._allowed_new_job,
        )

    def run_once(self, worker_id, pool):
        self._worker_id = worker_id
        return super().run_once(worker_id, pool)

    def _collect(self, job, lease, tag, task_id, heartbeat):
        with collection_slot(self.collection_lock_dir, heartbeat):
            return super()._collect(job, lease, tag, task_id, heartbeat)

    def run_forever(self, worker_id, pool, *, poll_interval_s=1):
        if not math.isfinite(poll_interval_s) or not 0.01 <= poll_interval_s <= 60:
            raise ValueError('invalid_poll_interval')
        self._worker_id = worker_id
        prior = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                prior[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda *_: self.drain())
        try:
            while True:
                self._check_external_stop()
                if (
                    self.stopped()
                    and self.control.get(worker_id)['current_job_id'] is None
                ):
                    break
                self.run_once(worker_id, pool)
                time.sleep(poll_interval_s)
        finally:
            try:
                self.control.drain(worker_id)
            finally:
                for signum, handler in prior.items():
                    signal.signal(signum, handler)
