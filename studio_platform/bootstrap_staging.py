"""One pollable, upload-only operation; no provider, database or model execution.

The caller durably records the exact preparation identity before polling. The
OS-held lock outlives individual controller ticks, so another controller cannot
upload concurrently. A lost process may resume verified partial bytes, but this
helper never calls remote setup/start or infers an inference outcome.
"""

from __future__ import annotations

import threading

from .slot_lock import slot_lock


class UploadCancelled(Exception):
    pass


class PollableUpload:
    def __init__(self):
        self._thread = None
        self._done = threading.Event()
        self._cancelled = threading.Event()
        self._mutex = threading.Lock()
        self._state = "staging"
        self._progress = {}
        self._key = None

    def pending(self):
        return self._thread is not None and (
            self._thread.is_alive() or not self._done.is_set()
        )

    def stopped_for(self, key):
        """Positive proof from this owned operation, never reconstructed from files."""
        if self._key != key or not self._done.is_set() or self.pending():
            return None
        return {**self.snapshot(), "cancel_requested": self._cancelled.is_set()}

    def cancel(self):
        self._cancelled.set()

    def snapshot(self):
        with self._mutex:
            return {
                "state": self._state,
                "phase": "staging_dependencies",
                **self._progress,
            }

    def _observe(self, transferred, total):
        if (
            type(transferred) is not int
            or type(total) is not int
            or not 0 <= transferred <= total
        ):
            return
        with self._mutex:
            self._progress = {"transferred_bytes": transferred, "total_bytes": total}

    def poll(self, directory, key, upload):
        if self._key is not None and key != self._key:
            raise ValueError("bootstrap_staging_identity_changed")
        if self._thread is not None:
            return self.snapshot()
        if self._done.is_set():
            return self.snapshot()
        lock = slot_lock(directory, "staging-" + key)
        if not lock.__enter__():
            lock.__exit__(None, None, None)
            return {"state": "staging_locked", "phase": "staging_dependencies"}
        self._key = key
        if self._cancelled.is_set():
            lock.__exit__(None, None, None)
            self._state = "staging_cancelled"
            self._done.set()
            return self.snapshot()

        def run():
            state = "staged"
            try:
                if self._cancelled.is_set():
                    raise UploadCancelled
                upload(progress=self._observe, should_stop=self._cancelled.is_set)
                if self._cancelled.is_set():
                    raise UploadCancelled
            except UploadCancelled:
                state = "staging_cancelled"
            except Exception:
                # SSH/library messages may contain endpoints or private paths.
                state = "staging_failed"
            finally:
                try:
                    lock.__exit__(None, None, None)
                finally:
                    with self._mutex:
                        self._state = state
                    self._done.set()

        self._thread = threading.Thread(
            target=run, name="bootstrap-upload", daemon=True
        )
        try:
            self._thread.start()
        except Exception:
            lock.__exit__(None, None, None)
            self._state = "staging_failed"
            self._done.set()
        return self.snapshot()
