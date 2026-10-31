"""Drain finite domain calls, not durable workflow sleeps, on executor exit."""

import asyncio
import threading


class DomainTurns:
    def __init__(self):
        self._condition = threading.Condition()
        self._closing = False
        self._active = 0

    def _invoke(self, function, args):
        # Check inside the worker: queued to_thread calls must not start after
        # drain returned. Cancellation leaves DBOS PENDING for normal recovery,
        # rather than recording a fabricated domain result or workflow failure.
        with self._condition:
            if self._closing:
                raise asyncio.CancelledError()
            self._active += 1
        try:
            return function(*args)
        finally:
            # Async task cancellation does not stop its underlying thread.
            # Only the actual domain call's exit releases this obligation.
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    async def call(self, function, *args):
        return await asyncio.to_thread(self._invoke, function, args)

    def drain(self):
        with self._condition:
            self._closing = True
            while self._active:
                self._condition.wait()
