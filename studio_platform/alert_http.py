"""Bounded aggregate HTTP counters; no request content or identity retained."""
import json
import threading
import time
import uuid

PATH = "/internal/alert-counters"
WINDOW_SECONDS = 300


class HTTPAlertCounters:
    def __init__(self, *, clock=time.time):
        self.clock = clock
        self.epoch = str(uuid.uuid4())
        self.buckets = {}
        self.lock = threading.Lock()

    def _prune(self, now):
        for second in list(self.buckets):
            if not now-WINDOW_SECONDS < second <= now:
                self.buckets.pop(second)

    def record(self, failed):
        now = int(self.clock())
        with self.lock:
            self._prune(now)
            counts = self.buckets.setdefault(now, [0, 0])
            counts[0] += 1
            counts[1] += int(failed)

    def snapshot(self):
        now = int(self.clock())
        with self.lock:
            self._prune(now)
            return {"epoch": self.epoch, "window_seconds": WINDOW_SECONDS,
                "requests": sum(value[0] for value in self.buckets.values()),
                "server_errors": sum(value[1] for value in self.buckets.values())}


class AlertCountersMiddleware:
    def __init__(self, app, *, counters=None):
        self.app, self.counters = app, counters if counters is not None else HTTPAlertCounters()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path")
        if path == PATH:
            peer = scope.get("client")
            # Never accept proxy assertions. This also refuses proxy middleware
            # rewriting scope.client from any forwarded header to loopback.
            forwarded = any(name.lower() in {b"forwarded", b"x-forwarded-for", b"x-real-ip"}
                for name, _ in scope.get("headers", []))
            local = isinstance(peer, (list, tuple)) and len(peer) == 2 and peer[0] in {"127.0.0.1", "::1"}
            status = 403 if not local or forwarded else 405 if scope.get("method") != "GET" else 200
            value = self.counters.snapshot() if status == 200 else {"code": "alert_counters_unavailable"}
            body = json.dumps(value, separators=(",", ":")).encode("ascii")
            await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode("ascii"))]})
            return await send({"type": "http.response.body", "body": body})
        if path == "/healthz":
            return await self.app(scope, receive, send)
        status, exception = None, False
        async def counted_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
        try:
            return await self.app(scope, receive, counted_send)
        except Exception:
            exception = True
            raise
        finally:
            self.counters.record(exception or status is None or status >= 500)
