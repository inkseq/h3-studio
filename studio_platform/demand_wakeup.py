"""Commit-time wake hints for the original business ledger, never task authority.

PostgreSQL LISTEN uses a dedicated autocommit connection. Notifications carry a
fixed nonsecret payload; callers always re-read durable rows after waking.
SQLite remains a local verification backend with a blocking timeout fallback.
"""
from __future__ import annotations

import select
import socket
import time

from sqlalchemy import text

CHANNEL = "sixnine_business_demand_v1"
PAYLOAD = "changed"
IDLE_SAFETY_SECONDS = 900
TRANSACTION_FLAG = "sixnine_demand_notified"


def notify_demand(connection):
    """Deliver only on commit; rollback discards the hint with its business rows."""
    if connection.dialect.name != "postgresql" or connection.info.get(TRANSACTION_FLAG):
        return
    connection.execute(text("SELECT pg_notify(:channel, :payload)"),
                       {"channel": CHANNEL, "payload": PAYLOAD})
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
            connection = self.repo.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
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


def run_demand_loop(stop, wakeup, has_demand, work, *, active_seconds, safety=None,
                    clock=time.monotonic, report=lambda value: None):
    """One existing actor: fast while obligated, blocked while genuinely idle."""
    wakeup.subscribe()  # Commit before subscription is caught by the first scan.
    next_safety = clock()
    while not stop.is_set():
        if has_demand():
            report(work())
        elif safety is not None and clock() >= next_safety:
            report(safety())
            next_safety = clock() + IDLE_SAFETY_SECONDS
        if stop.is_set():
            break
        # Re-read after work and before wait. A commit in this gap also leaves
        # a notification buffered on the already-subscribed connection.
        active = has_demand()
        seconds = active_seconds if active else max(0, next_safety - clock()) if safety else IDLE_SAFETY_SECONDS
        wakeup.wait(seconds)
