"""One log line of named phase durations for a slow request path (#1252)."""

from contextvars import ContextVar
import logging
import time

_LOG = logging.getLogger("studio_platform.timing")
_CURRENT = ContextVar("phase_timer", default=None)


class PhaseTimer:
    """Context manager: lap(name) adds the time since the previous lap.

    Inner code calls the module-level lap() so no signature changes; outside
    a timer it does nothing. One line is logged on exit.
    """

    def __init__(self, event, slow_ms=2000):
        self.event, self.slow_ms, self.ms = event, slow_ms, {}

    def __enter__(self):
        self.started = self.last = time.perf_counter()
        self._token = _CURRENT.set(self)
        return self

    def lap(self, name):
        now = time.perf_counter()
        self.ms[name] = self.ms.get(name, 0) + (now - self.last) * 1000
        self.last = now

    def __exit__(self, *_exc):
        _CURRENT.reset(self._token)
        total = (time.perf_counter() - self.started) * 1000
        _LOG.log(
            logging.WARNING if total >= self.slow_ms else logging.INFO,
            "%s total_ms=%d %s",
            self.event,
            total,
            " ".join(f"{name}_ms={int(value)}" for name, value in self.ms.items()),
        )
        return False


def lap(name):
    timer = _CURRENT.get()
    if timer is not None:
        timer.lap(name)
