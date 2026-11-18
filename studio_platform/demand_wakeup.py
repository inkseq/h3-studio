"""Commit-time wake hints for the original business ledger, never task authority.

PostgreSQL LISTEN uses a dedicated autocommit connection. Notifications carry a
fixed nonsecret payload; callers always re-read durable rows after waking.
SQLite remains a local verification backend with a blocking timeout fallback.
"""

from __future__ import annotations

import faulthandler
import select
import socket
from contextlib import contextmanager
import time

from sqlalchemy import text

CHANNEL = "sixnine_business_demand_v1"
PAYLOAD = "changed"
IDLE_SAFETY_SECONDS = 900
# A pass this long gets every thread's stack written to stderr, once. Passes
# of 56-127 s stalled the pass and all six hosted slot turns together
# (staging 2026-11-18, #1252); the stacks say what they were all waiting on.
STALL_DUMP_SECONDS = 30
TRANSACTION_FLAG = "sixnine_demand_notified"


def notify_demand(connection):
    """Deliver only on commit; rollback discards the hint with its business rows."""
    if connection.dialect.name != "postgresql" or connection.info.get(TRANSACTION_FLAG):
        return
    connection.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": CHANNEL, "payload": PAYLOAD},
    )
    connection.info[TRANSACTION_FLAG] = True


class DemandWakeup:
    def __init__(self, repo, stop, *, selector=select.select):
        self.repo, self.stop, self.selector = repo, stop, selector
        self.connection = self.driver = None
        self.reader, self.writer = socket.socketpair()
        self.reader.setblocking(False)
        self.writer.setblocking(False)

    def subscribe(self):
        """Call before scanning demand, including after a connection failure."""
        if self.repo.engine.dialect.name != "postgresql":
            return False
        try:
            connection = self.repo.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            )
            self.connection = connection
            connection.exec_driver_sql("LISTEN " + CHANNEL)
            self.driver = connection.connection.driver_connection
            return True
        except Exception:
            self._disconnect()
            return False

    def interrupt(self):
        """Signal shutdown without a polling thread or a database mutation."""
        try:
            self.writer.send(b"x")
        except (BlockingIOError, OSError):
            pass

    def _disconnect(self):
        if self.connection is not None:
            try:
                self.connection.invalidate()
            finally:
                self.connection.close()
        self.connection = self.driver = None

    def _notifications(self):
        return bool(list(self.driver.notifies(timeout=0, stop_after=100)))

    def wait(self, seconds):
        if self.stop.is_set():
            return "stopped"
        seconds = max(0, seconds)
        if self.repo.engine.dialect.name != "postgresql":
            self.stop.wait(seconds)
            return "timeout"
        if self.driver is None:
            # A failed listener is not a valid idle subscription. Reconnect and
            # re-scan instead of relying on notifications lost while offline.
            if self.stop.wait(min(seconds, 5)):
                return "stopped"
            return "reconnected" if self.subscribe() else "unavailable"
        try:
            if self._notifications():
                return "notified"
            ready, _, _ = self.selector([self.driver, self.reader], [], [], seconds)
            if self.reader in ready:
                try:
                    self.reader.recv(4096)
                except BlockingIOError:
                    pass
            if self.stop.is_set():
                return "stopped"
            if self.driver in ready:
                self._notifications()
                return "notified"
            return "timeout"
        except Exception:
            self._disconnect()
            return "disconnected"

    def close(self):
        try:
            if self.connection is not None:
                try:
                    self.connection.exec_driver_sql("UNLISTEN " + CHANNEL)
                except Exception:
                    self.connection.invalidate()
                self.connection.close()
        finally:
            self.connection = self.driver = None
            self.reader.close()
            self.writer.close()


@contextmanager
def _stall_dump():
    """Arm the dump for one pass; diagnostics must never stop the loop."""
    try:
        faulthandler.dump_traceback_later(STALL_DUMP_SECONDS, exit=False)
    except Exception:  # e.g. a stderr without a file descriptor
        yield
        return
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


def run_demand_loop(
    stop,
    wakeup,
    has_demand,
    work,
    *,
    active_seconds,
    safety=None,
    heartbeat=None,
    clock=time.monotonic,
    report=lambda value: None,
):
    """One existing actor: fast while obligated, blocked while genuinely idle."""
    from .phase_timing import PhaseTimer

    wakeup.subscribe()  # Commit before subscription is caught by the first scan.
    next_safety = clock()
    while not stop.is_set():
        # One line per pass: hosted turns share this process, so a long pass
        # delays their first claim (#1252).
        with (
            _stall_dump(),
            PhaseTimer("controller_pass_timing", slow_ms=5000) as timer,
        ):
            demand = has_demand()
            timer.lap("has_demand")
            if demand:
                report(work())
                timer.lap("work")
            elif safety is not None and clock() >= next_safety:
                report(safety())
                next_safety = clock() + IDLE_SAFETY_SECONDS
                timer.lap("safety")
            if stop.is_set():
                break
            # Re-read after work and before wait. A commit in this gap also
            # leaves a notification buffered on the already-subscribed connection.
            active = has_demand()
            timer.lap("recheck")
            seconds = (
                (active_seconds() if callable(active_seconds) else active_seconds)
                if active
                else max(0, next_safety - clock())
                if safety
                else IDLE_SAFETY_SECONDS
            )
            timer.lap("cadence")
            if heartbeat is not None:
                # The loop has finished its scan/work. No timer can mask a stall.
                heartbeat(active)
                seconds = min(seconds, 60)
                timer.lap("heartbeat")
        wakeup.wait(seconds)
